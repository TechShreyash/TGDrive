"""Mirror confirmed encrypted MongoDB archives without downloading originals.

The portable channel catalog remains the automatic recovery point. This extra
archive retains the independently consistent full MongoDB snapshot for advanced
recovery, including app settings and records not replayed during channel failover.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import stat
from pathlib import Path

from pymongo.errors import PyMongoError

from tgdrive.recovery.mongo_backup import MAGIC, MAX_EXPANDED
from tgdrive.settings.managed_settings import SettingsProblem
from tgdrive.storage.mongo import DatabaseUnavailable


def _check_pending_key(service, configured, key):
    """A resumed immutable ciphertext must match the key placed in the capsule.

    Pending snapshots have no key fingerprint. Authenticate their staged
    ciphertext in bounded blocks before allowing a resumed publication;
    do not expand, rewrite or discard the pending database archive.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    backend = getattr(service, "backend", None)
    if backend is None:
        return
    pending = backend.db.backups.find_one(
        {"status": "pending"}, sort=[("created_at", 1)]
    )
    if pending is None:
        return
    if pending.get("format") == "tgdrive-backup-v2":
        return  # MongoProtection completes this file under its own unified key.
    path = Path(pending["local_path"])
    if not path.exists():
        return  # Existing MongoProtection will mark this interrupted safely.
    root = configured.resolved_cache_dir / "mongo-backups"
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise SettingsProblem("The staged MongoDB safety copy path is invalid.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or not len(MAGIC) + 28 <= info.st_size <= MAX_EXPANDED
        ):
            raise ValueError
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            if source.read(len(MAGIC)) != MAGIC:
                raise ValueError
            nonce = source.read(12)
            source.seek(-16, os.SEEK_END)
            tag = source.read(16)
            decoder = Cipher(
                algorithms.AES(bytes.fromhex(key)), modes.GCM(nonce, tag)
            ).decryptor()
            decoder.authenticate_additional_data(MAGIC)
            source.seek(len(MAGIC) + 12)
            remaining = info.st_size - len(MAGIC) - 28
            while remaining:
                block = source.read(min(1024**2, remaining))
                if not block:
                    raise ValueError
                decoder.update(block)
                remaining -= len(block)
            decoder.finalize()
    except Exception:
        raise SettingsProblem(
            "The pending MongoDB archive does not authenticate with the current "
            "recovery key. Preserve it and restore its original key before "
            "retrying; no existing archive was changed.",
            code="REPLICA_MONGODB_KEY_MISMATCH",
            status=409,
        ) from None
    finally:
        os.close(descriptor)


async def mirror_mongodb(
    replica,
    transport,
    *,
    source_channel: int,
    destination_channel: int,
    revision: int | None = None,
    settings_fingerprint: str | None = None,
):
    if replica.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
        return None
    backend = getattr(replica.runtime, "mongo", None)
    if backend is None:
        return None
    configured = replica.settings
    secret = getattr(configured, "mongodb_backup_key", None)
    if secret is None:
        raise SettingsProblem(
            "Full MongoDB safety copies require MONGODB_BACKUP_KEY. Preserve the "
            "existing database, configure its recovery key, save an independent "
            "copy and restart before retrying channel backup.",
            code="REPLICA_MONGODB_KEY_REQUIRED",
            status=409,
        )
    key = secret.get_secret_value()
    if re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
        raise SettingsProblem("The MongoDB recovery key is invalid.")
    if (
        source_channel != configured.storage_channel
        or source_channel == destination_channel
        or type(destination_channel) is not int
        or not -(1 << 63) <= destination_channel < 0
    ):
        raise SettingsProblem("MongoDB safety copy channels do not match this drive.")
    store = replica.runtime.store
    required = store.revision if revision is None else revision
    if type(required) is not int or not 0 < required <= store.revision:
        raise SettingsProblem("The requested MongoDB backup revision is invalid.")
    if settings_fingerprint is not None and (
        not isinstance(settings_fingerprint, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", settings_fingerprint) is None
    ):
        raise SettingsProblem("The saved settings fingerprint is invalid.")
    fingerprint = hashlib.sha256(bytes.fromhex(key)).hexdigest()
    cache_key = (
        f"mongo-snapshot:{store.drive_id}:{source_channel}:{destination_channel}:"
        f"{required}:{fingerprint}"
    )
    if settings_fingerprint is not None:
        # Policy/connection edits do not necessarily advance catalog revision.
        # Retain their captured identity through retries, but require a fresh
        # full database snapshot for the next changed settings generation.
        cache_key += ":settings:" + settings_fingerprint.lower()
    archive = replica.journal.get(cache_key)
    if archive is None:
        service = replica.runtime.backup
        # Use the existing service and its publication lock. Never introduce a
        # second snapshot writer or temporarily replace its configured key.
        for _ in range(2):
            await asyncio.to_thread(_check_pending_key, service, configured, key)
            try:
                await service.snapshot_now(store, legacy=True)
            except asyncio.CancelledError:
                raise
            except PyMongoError, DatabaseUnavailable:
                # Keep transient outages distinct from ownership/permission failures.
                raise
            except Exception as error:
                from tgdrive.replication.replica_transport import ReplicaTransportError

                delay = getattr(error, "retry_after", None) or getattr(
                    error, "seconds", None
                )
                if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
                    raise ReplicaTransportError(
                        "Telegram requested a wait before the full database "
                        "backup resumes.",
                        "REPLICA_FLOOD_WAIT",
                        retry_after=max(1, delay),
                        retryable=True,
                    ) from None
                if (
                    isinstance(error, (TimeoutError, OSError))
                    or getattr(error, "timeout", False) is True
                ):
                    raise ReplicaTransportError(
                        "The full database safety copy could not be confirmed. "
                        "It will retry when connectivity returns.",
                        "REPLICA_MONGODB_UNAVAILABLE",
                        retryable=True,
                    ) from None
                raise SettingsProblem(
                    "The full MongoDB safety copy failed. Check database access, "
                    "the recovery key, temporary storage and Telegram permissions, "
                    "then retry. Existing confirmed backups are retained.",
                    code="REPLICA_MONGODB_SNAPSHOT_FAILED",
                    status=503,
                ) from None
            rows = await asyncio.to_thread(service.history, limit=100)
            latest = next(
                (
                    row
                    for row in rows
                    if row.get("status") == "confirmed"
                    and row.get("channel_id") == source_channel
                    and row.get("format") == "mongodb-encrypted-v1"
                    and type(row.get("revision")) is int
                    and row["revision"] >= required
                ),
                None,
            )
            if latest is not None:
                break
        else:
            raise SettingsProblem(
                "A current full MongoDB safety copy has not been confirmed. "
                "Retry after the database snapshot finishes.",
                code="REPLICA_MONGODB_SNAPSHOT_PENDING",
                status=503,
            )
        identity = latest.get("id")
        message_id = latest.get("message_id")
        digest = latest.get("sha256")
        if (
            not isinstance(identity, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity) is None
            or type(message_id) is not int
            or not 0 < message_id < 2**31
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SettingsProblem("The confirmed MongoDB snapshot record is invalid.")
        async with replica.runtime.pool.lease() as client:
            message = await client.get_messages(source_channel, message_ids=message_id)
        document = getattr(message, "document", None)
        size = getattr(document, "file_size", None)
        if (
            type(size) is not int
            or not 0 < size <= MAX_EXPANDED
            or getattr(document, "file_name", None) != "tgdrive-" + identity + ".tgdb"
        ):
            raise SettingsProblem(
                "The full MongoDB archive is missing or differs from its "
                "confirmed record. Preserve existing backups and retry.",
                code="REPLICA_MONGODB_ARCHIVE_MISSING",
                status=409,
            )
        archive = {
            "format": "mongodb-encrypted-v1",
            "id": identity,
            "revision": latest["revision"],
            "source_channel": source_channel,
            "source_message_id": message_id,
            "replica_channel": destination_channel,
            "size": size,
            "sha256": digest,
            "created_at": latest.get("created_at"),
        }
        # Persist exactly which confirmed source ciphertext will be copied.
        replica.journal.set(cache_key, archive)
    if replica.journal.get("status", {}).get("phase") in {"paused", "disabled"}:
        return None
    copy_key = (
        f"mongo-copy:{store.drive_id}:{source_channel}:{destination_channel}:"
        f"{archive['source_message_id']}:{archive['size']}:{archive['sha256']}"
    )
    operation_id, receipt = replica.journal.intent(copy_key, archive["size"])
    if receipt is None:
        receipt = await transport.copy(
            source_channel,
            archive["source_message_id"],
            destination_channel,
            random_id=operation_id,
            expected_size=archive["size"],
        )
        replica.journal.confirm(copy_key, receipt)
    else:
        await transport.verify(destination_channel, receipt, archive["size"])
    result = archive | {"replica_message_id": receipt}
    from tgdrive.replication.replica_settings import validate_recovery_settings

    validate_recovery_settings({"mongodb_archive": result, "mongodb_backup_key": key})
    return result
