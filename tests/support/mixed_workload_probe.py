"""Run cold and warm TGDrive workloads against test MongoDB and a Telegram double."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import resource
import tempfile
import time
from collections import Counter, defaultdict
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import Any
from unittest.mock import AsyncMock

from httpx2 import ASGITransport, AsyncClient
from pydantic import SecretStr
from pymongo import monitoring
from telethon import utils

from tests.media.test_pooled_media import account
from tests.media.test_telegram_cache import message
from tests.storage.test_mongodb import isolated_backend
from tests.support.app_fixtures import FakeRuntime, make_settings
from tests.support.stream_memory_probe import cgroup_value
from tests.transfers.test_multipart_transfers import Account, Ledger
from tgdrive.core.security import SESSION_COOKIE
from tgdrive.media.media_policy import MediaCachePolicy
from tgdrive.media.media_reader import MediaReadCache
from tgdrive.media.media_scheduler import MediaScheduler
from tgdrive.media.preloading import PreloadManager
from tgdrive.storage.mongo_store import MongoStore
from tgdrive.telegram.client_pool import ClientPool
from tgdrive.telegram.clients import TelegramMessage
from tgdrive.transfers.manager import TransferManager
from tgdrive.transfers.remote import RemoteClient
from tgdrive.transfers.tasks import TransferRegistry, TransferStatus
from tgdrive.transfers.uploads import ResumableUploads
from tgdrive.web.app import create_app

MIB = 1024 * 1024
PROFILES = {"small": (10_000, 1_000, 3), "busy": (100_000, 100_000, 16)}
REQUEST_ROUTE = ContextVar("probe_request_route", default="background")


class MongoCommands(monitoring.CommandListener):
    """Count commands against the disposable database without reading their payloads."""

    def __init__(self):
        self.database = ""
        self._commands: Counter[tuple[str, str]] = Counter()
        self._lock = Lock()

    def started(self, event):
        if event.database_name == self.database:
            with self._lock:
                self._commands[REQUEST_ROUTE.get(), event.command_name] += 1

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass

    def snapshot(self):
        with self._lock:
            return self._commands.copy()


def seed(store, registry, nodes, jobs):
    for cursor in range(0, nodes, 1000):
        store.db.nodes.insert_many(
            [
                store._node(
                    f"fixture-{i:06}",
                    "root",
                    f"Fixture {i:06}.txt",
                    "file",
                    message_id=200_000 + i,
                    size=1,
                    starred=True,
                )
                for i in range(cursor, min(nodes, cursor + 1000))
            ]
        )
    store.db.catalog.update_one(
        {"_id": "catalog"}, {"$inc": {"files": nodes, "total_bytes": nodes}}
    )
    for cursor in range(0, jobs, 1000):
        rows = []
        for i in range(cursor, min(jobs, cursor + 1000)):
            identity = f"archive-{i:06}"
            summary = {
                "id": identity,
                "created_at": i,
                "status": "completed",
                "kind": "upload",
                "name": f"Finished file {i:06}.txt",
            }
            rows.append(
                {
                    "_id": identity,
                    "archived": True,
                    "created_at": i,
                    "status": "completed",
                    "summary": summary,
                }
            )
        store.db.jobs.insert_many(rows)
    index = registry._history_index
    assert index is not None
    index.ensure_indexes()
    while not index.rebuild_step():
        pass


def distribution(samples):
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": round(ordered[math.ceil(0.50 * len(ordered)) - 1], 2),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 2),
    }


async def phase(app, manager, session, examples, preload, seconds, label):
    latency = defaultdict(list)
    delivered = 0
    completed = []
    peak_staged = 0
    started = time.monotonic()
    deadline = started + seconds

    async def measured_app(scope, receive, send):
        before = time.monotonic()

        async def measured_send(event):
            nonlocal before
            if (
                before
                and scope["path"] == "/file"
                and (event["type"] == "http.response.body" and event.get("body"))
            ):
                latency["first_bytes"].append(1000 * (time.monotonic() - before))
                before = 0
            await send(event)

        path = scope["path"]
        if path.startswith("/api/v1/uploads/"):
            path = "/api/v1/uploads/{id}" + (
                "/complete" if path.endswith("/complete") else ""
            )
        route = REQUEST_ROUTE.set(f"{scope['method']} {path}")
        try:
            await app(scope, receive, measured_send)
        finally:
            REQUEST_ROUTE.reset(route)

    async with (
        AsyncClient(
            transport=ASGITransport(app=measured_app),
            base_url="https://probe.invalid",
            headers={"Cookie": f"{SESSION_COOKIE}={session}"},
        ) as owner,
        AsyncClient(
            transport=ASGITransport(app=measured_app), base_url="https://probe.invalid"
        ) as guest,
    ):

        async def viewer(i, path, token, expected):
            nonlocal delivered
            client = owner if i == 0 else guest
            options = (
                {"path": path, "preview": "true"}
                if i == 0
                else {"path": path, "auth": token, "download": "true"}
            )
            while time.monotonic() < deadline:
                before = time.monotonic()
                response = await client.get(
                    "/file",
                    params=options,
                    headers={"Range": f"bytes=0-{MIB // 2 - 1}"},
                )
                assert response.status_code == 206, (label, i, response.status_code)
                assert hashlib.sha256(response.content).hexdigest() == expected
                latency["range"].append(1000 * (time.monotonic() - before))
                delivered += len(response.content)
                await asyncio.sleep(0.05)

        async def observer():
            while time.monotonic() < deadline:
                for key, path in (
                    ("catalog", "/api/v1/starred?limit=100"),
                    ("history", "/api/v1/transfers/history?page=1&limit=20"),
                    ("health", "/health/ready"),
                ):
                    before = time.monotonic()
                    response = await owner.get(path)
                    assert response.status_code == 200, (
                        label,
                        key,
                        response.status_code,
                    )
                    latency[key].append(1000 * (time.monotonic() - before))
                before = time.monotonic()
                queue = manager.pool.performance_snapshot()["queue"]
                latency["sampled_queue_wait"].extend(
                    1000 * item["waiting_seconds"] for item in queue
                )
                await asyncio.sleep(0.20)
                latency["event_loop_lag"].append(
                    1000 * max(0, time.monotonic() - before - 0.20)
                )

        def staged_size(upload_id):
            return manager.uploads.path(upload_id).stat().st_size

        async def upload_and_preload():
            nonlocal peak_staged
            content = (label.encode() * MIB)[:MIB]
            checksum = hashlib.sha256(content).hexdigest()
            prepared = await owner.post("/api/v1/preloads", json={"path": preload})
            assert prepared.status_code == 202, prepared.text
            preload_id = prepared.json()["id"]
            while not completed or time.monotonic() < deadline:
                before = time.monotonic()
                response = await owner.post(
                    "/api/v1/uploads",
                    json={
                        "name": f"{label}-{len(completed)}.bin",
                        "parent_id": "/",
                        "total_size": len(content),
                    },
                )
                assert response.status_code == 201, response.text
                upload_id = response.json()["id"]
                target = f"/api/v1/uploads/{upload_id}"
                received = await owner.patch(
                    target,
                    content=content,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Upload-Offset": "0",
                    },
                )
                assert received.status_code == 204, received.text
                assert int(received.headers["Upload-Offset"]) == len(content)
                staged = await asyncio.to_thread(staged_size, upload_id)
                assert staged == len(content)
                peak_staged = max(peak_staged, staged)
                queued = await owner.post(target + "/complete")
                assert queued.status_code == 202, queued.text
                job = manager.registry.get(queued.json()["id"])
                assert job.task is not None
                await asyncio.wait_for(asyncio.shield(job.task), 25)
                assert job.status is TransferStatus.COMPLETED, job.as_dict()
                item = await asyncio.to_thread(
                    manager.store.transfer_location, job.id, completed=True
                )
                completed.append(("/" + item["item_id"], checksum))
                latency["upload"].append(1000 * (time.monotonic() - before))
                delay = min(0.5, deadline - time.monotonic())
                if delay > 0:
                    await asyncio.sleep(delay)
            return preload_id

        async with asyncio.timeout(seconds + 40), asyncio.TaskGroup() as group:
            group.create_task(observer())
            upload_task = group.create_task(upload_and_preload())
            for i, (path, token, expected) in enumerate(examples):
                group.create_task(viewer(i, path, token, expected))
        elapsed = time.monotonic() - started
        preload_id = upload_task.result()
        async with asyncio.timeout(25):
            while True:
                response = await owner.get("/api/v1/preloads")
                assert response.status_code == 200, response.text
                job = next(
                    row for row in response.json()["jobs"] if row["id"] == preload_id
                )
                if job["state"] == "ready":
                    assert job["complete"] and job["ready_bytes"] == MIB, job
                    break
                assert job["state"] in {"queued", "preparing"}, job
                await asyncio.sleep(0.05)
    assert completed and delivered
    return {
        "verified_uploads": len(completed),
        "preload_ready_bytes": MIB,
        "peak_sampled_staged_bytes": peak_staged,
        "delivered_mib_per_second": round(delivered / elapsed / MIB, 2),
        "latency": {key: distribution(ms) for key, ms in latency.items() if ms},
    }, completed


async def run(profile, directory, seconds, commands):
    nodes, jobs, readers = PROFILES[profile]
    settings = make_settings(directory)
    with isolated_backend(settings.resolved_data_dir) as backend:
        commands.database = backend.db.name
        settings = settings.model_copy(
            update={
                "mongodb_uri": SecretStr(os.environ["TGDRIVE_TEST_MONGODB_URI"]),
                "mongodb_database": backend.db.name,
                "min_free_bytes": 0,
            }
        )
        store = MongoStore.create(backend, storage_channel=settings.storage_channel)
        registry = TransferRegistry(mongo=backend.db.jobs, mongo_backend=backend)
        seed(store, registry, nodes, jobs)
        payloads, examples = {}, []
        for i in range(readers + 2):
            identity = i + 1
            content = bytes([identity]) * MIB
            payloads[identity] = content
            suffix = "mp4" if i == 0 else "bin"
            item = store.add_file("/", f"sample-{i}.{suffix}", identity, len(content))
            path = "/" + item["id"]
            token = store.create_share(path)
            examples.append(
                (path, token, hashlib.sha256(content[: MIB // 2]).hexdigest())
            )

        ledger = Ledger()
        ledger.rpc_delay = 0.1
        clients = []
        for i in range(2):
            name = f"probe-bot-{i}"
            sender = Account(name, ledger)
            transport: Any = account(name, payloads[1])
            transport._media_scheduler = MediaScheduler(limit=8, rate=48)
            transport.native.payloads = payloads

            async def lookup(channel, *, message_ids, transport=transport):
                content = payloads.get(message_ids)
                if content is None:
                    return None
                channel_id, _ = utils.resolve_id(channel)
                native = message(
                    message_ids,
                    len(content),
                    message_id=message_ids,
                    channel_id=channel_id,
                )
                return TelegramMessage(transport, native)

            transport.get_messages = AsyncMock(side_effect=lookup)

            async def send_staged(
                channel,
                path,
                *,
                preview_name=None,
                thumbnail=None,
                generate_thumbnail=True,
                sender=sender,
                **options,
            ):
                return await sender.send_staged(channel, path, **options)

            transport.send_staged = send_staged
            clients.append(transport)

        pool = ClientPool(clients, media_per_bot=4)
        await pool.start()
        runtime: Any = FakeRuntime(store)
        runtime.mongo = backend
        runtime.pool = pool
        runtime.ready = True
        manager = TransferManager(
            settings=settings,
            pool=pool,
            store=store,
            registry=registry,
            remote=RemoteClient(),
        )
        manager.uploads = ResumableUploads(settings, manager)
        runtime.transfers = manager
        policy = MediaCachePolicy(
            memory_max_bytes=32 * MIB, disk_max_bytes=64 * MIB, min_free_bytes=0
        )
        pool._media_read_cache = MediaReadCache(directory / "stream", policy)
        runtime.preloads = PreloadManager(pool, policy=policy)
        result, uploaded = {}, []
        try:
            app = create_app(settings, runtime=runtime)
            session = await asyncio.to_thread(app.state.sessions.create)

            def native_reads():
                return sum(sum(client.native.requests.values()) for client in clients)

            for i, label in enumerate(("cold", "warm")):
                reads_before = native_reads()
                commands_before = commands.snapshot()
                report, completed = await phase(
                    app,
                    manager,
                    session,
                    examples[:readers],
                    examples[readers + i][0],
                    seconds,
                    label,
                )
                report["origin_payload_bytes"] = (native_reads() - reads_before) * (
                    MIB // 2
                )
                counts = commands.snapshot() - commands_before
                by_command = Counter()
                by_route = defaultdict(dict)
                for (route, command), count in counts.items():
                    by_command[command] += count
                    by_route[route][command] = count
                report["mongo_commands"] = dict(by_command)
                report["mongo_commands_by_route"] = dict(by_route)
                result[label] = report
                uploaded.extend(completed)
        finally:
            await runtime.preloads.close()
            await manager.close()
            await pool.stop()
            store.close()
        reopened = MongoStore(backend)
        try:
            assert all(
                reopened.get_file(path)["sha256"] == digest for path, digest in uploaded
            )
        finally:
            reopened.close()
        return {
            "provider": "synthetic Telegram with real MongoDB",
            "catalog_nodes": nodes,
            "archived_jobs": jobs,
            "concurrent_readers": readers,
            "uploads_after_reopen": len(uploaded),
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "memory_max": cgroup_value("memory.max"),
            "memory_peak": cgroup_value("memory.peak"),
            "memory_events": cgroup_value("memory.events"),
            "results": result,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("--seconds", type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.seconds <= 120:
        parser.error("seconds must be greater than zero and at most 120")
    if not os.environ.get("TGDRIVE_TEST_MONGODB_URI"):
        parser.error("Set TGDRIVE_TEST_MONGODB_URI to the disposable replica set")
    commands = MongoCommands()
    monitoring.register(commands)
    with tempfile.TemporaryDirectory(prefix="tgdrive-mixed-probe-") as directory:
        report = asyncio.run(run(args.profile, Path(directory), args.seconds, commands))
    print(json.dumps(report, sort_keys=True))
