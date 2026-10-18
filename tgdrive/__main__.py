"""Local maintenance commands. Never send account credentials to a hosted service."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path

from argon2 import PasswordHasher

from tgdrive.backup import TelegramBackupService
from tgdrive.config import Settings
from tgdrive.store import DriveStore
from tgdrive.telegram_clients import build_client_pool


async def restore_telegram(
    settings: Settings, message_id: int, destination: Path
) -> None:
    pool = build_client_pool(settings)
    try:
        await pool.start()
        restored = await TelegramBackupService(settings, pool).restore_from_telegram(
            message_id, destination=destination
        )
        restored.close()
    finally:
        await pool.stop(drain_timeout=10)


async def recover_telegram(settings: Settings, destination: Path | None = None) -> None:
    target = destination or settings.resolved_data_dir / "drive.sqlite3"
    if target.exists():
        raise FileExistsError(target)
    pool = build_client_pool(settings)
    try:
        await pool.start()
        restored = await TelegramBackupService(settings, pool).recover_latest(
            destination=target
        )
        try:
            print(
                f"Recovered catalog revision {restored.revision} from the verified "
                "Telegram recovery pin. Existing share links were revoked."
            )
        finally:
            restored.close()
    finally:
        await pool.stop(drain_timeout=10)


async def login(settings: Settings) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(), settings.api_id, settings.api_hash.get_secret_value()
    )
    try:
        await client.start(
            phone=lambda: input("Phone number: "),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
        me = await client.get_me()
        if me.bot or not me.premium:
            raise RuntimeError("This account is not a Premium user")
        directory = settings.resolved_data_dir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        output = directory / "premium-session.txt"
        output.write_text(client.session.save() + "\n")
        output.chmod(0o600)
        print("Saved the authorized session to DATA_DIR/premium-session.txt (0600).")
        print(
            "Copy its contents into STRING_SESSIONS locally; "
            "do not share this account credential."
        )
    finally:
        await client.disconnect()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="python -m tgdrive", description="TGDrive local maintenance"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "init",
        help="Create a new drive in the configured database; refuses existing state",
    )
    sub.add_parser(
        "password-hash", help="Generate an Argon2id owner password hash interactively"
    )
    sub.add_parser("login", help="Authorize a Premium Telegram account locally")
    mongo_restore = sub.add_parser(
        "restore-mongodb",
        help="Restore an encrypted complete backup into an empty MongoDB database",
    )
    mongo_restore.add_argument("--file", required=True, type=Path)
    recover = sub.add_parser(
        "recover", help="Recover the latest pinned checkpoint into an absent database"
    )
    recover.add_argument(
        "--destination",
        type=Path,
        help="New database path; defaults to DATA_DIR/drive.sqlite3",
    )
    restore = sub.add_parser("restore", help="Restore a checkpoint into a new database")
    source = restore.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--message-id", type=int)
    restore.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "password-hash":
        password = getpass.getpass("New owner password (at least 12 characters): ")
        if len(password) < 12 or password != getpass.getpass("Confirm password: "):
            parser.error("Passwords must match and contain at least 12 characters")
        print(PasswordHasher().hash(password))
        return
    if args.command == "restore-mongodb":
        from types import SimpleNamespace

        from dotenv import dotenv_values
        from pydantic import SecretStr

        from tgdrive.mongo import MongoBackend
        from tgdrive.mongo_backup import backup_key, restore_snapshot

        values = {**dotenv_values(".env"), **os.environ}
        uri = values.get("MONGODB_URI")
        if not uri:
            parser.error("Configure MONGODB_URI first")
        backend = MongoBackend(uri, values.get("MONGODB_DATABASE", "tgdrive"))
        key = backup_key(
            SimpleNamespace(
                mongodb_backup_key=SecretStr(values.get("MONGODB_BACKUP_KEY", ""))
            )
        )
        backend.acquire()
        try:
            restore_snapshot(backend, args.file, key)
        finally:
            backend.release()
            backend.client.close()
        print("Restored the complete MongoDB backup; browser sessions were revoked.")
        return
    settings = Settings()
    if args.command == "init":
        from tgdrive.mongo import for_settings, save_bootstrap

        backend = for_settings(settings)
        if backend is not None:
            from tgdrive.managed_settings import (
                ManagedDocument,
                initial_clients,
                save_document,
            )
            from tgdrive.mongo_store import MongoStore

            backend.acquire()
            try:
                store = MongoStore.create(backend)
                store.bind_storage_channel(settings.storage_channel)
                save_bootstrap(settings, backend)
                save_document(
                    settings.resolved_data_dir,
                    ManagedDocument(
                        revision=1,
                        api_id=settings.api_id,
                        api_hash=settings.api_hash,
                        clients=initial_clients(settings),
                    ),
                    0,
                )
                store.close()
            finally:
                backend.release()
            print("Created a new MongoDB drive. Existing state was not overwritten.")
        else:
            settings.resolved_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            store = DriveStore.create(settings.resolved_data_dir / "drive.sqlite3")
            store.close()
            print("Created the local SQLite drive. Existing state was not overwritten.")
    elif args.command == "restore":
        if args.file is not None:
            service = TelegramBackupService(settings, build_client_pool(settings))
            restored = service.restore_from_file(
                args.file, destination=args.destination
            )
            restored.close()
        else:
            asyncio.run(restore_telegram(settings, args.message_id, args.destination))
        print(
            "Restored checkpoint into the new destination. "
            "Review it before switching DATA_DIR."
        )
    elif args.command == "recover":
        asyncio.run(recover_telegram(settings, args.destination))
    elif args.command == "login":
        asyncio.run(login(settings))


if __name__ == "__main__":
    main()
