"""Opt-in local UI showcase; never imported by the production application.

TGDRIVE_UI_SHOWCASE=1 adds synthetic content only to the offline browser fixture.
Sample metrics are explicitly labelled. File/cache/share/trash actions use their
real stores; illustrative historical resource values never enter production.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import time
import zipfile
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.responses import JSONResponse

from tgdrive.media.media_reader import BLOCK
from tgdrive.transfers.tasks import TransferStatus
from tgdrive.web.share_links import token_factory


async def seed(runtime, settings):
    folder = runtime.store.create_folder("/", "Demo workspace")
    destination = "/" + folder["id"]
    cache = runtime.preloads.cache
    for index, (name, blocks) in enumerate(
        [
            ("Project reference.bin", (0, 1, 2)),
            ("Design assets.bin", (0, 2)),
            ("Interview recording.bin", (0,)),
            ("Product demo.bin", (1, 2)),
        ],
        400,
    ):
        size = 3 * BLOCK
        runtime.payloads[index] = b"x" * size
        item = runtime.store.add_file(destination, name, index, size)
        file_path = destination + "/" + item["id"]
        if index in (400, 401):
            runtime.store.create_share(
                file_path,
                label="Project review" if index == 400 else "Design handoff",
                expires_at=(
                    datetime.now(UTC) + timedelta(days=7 if index == 400 else 30)
                ).isoformat(),
                token_factory=token_factory(settings),
            )
        cache.register_file(
            channel_id=settings.storage_channel, message_id=index, size=size, name=name
        )
        for block in blocks:
            key = (
                settings.storage_channel,
                index,
                "document",
                index + 100,
                size,
                block,
            )
            await cache.get(key, BLOCK, lambda: asyncio.sleep(0, result=b"x" * BLOCK))
    await cache.flush()
    runtime.payloads[490] = b"Demo item: restore this from Trash."
    discarded = runtime.store.add_file(
        destination, "Old proposal.txt", 490, len(runtime.payloads[490])
    )
    runtime.store.set_trashed(destination + "/" + discarded["id"], True)
    registry = runtime.transfers.registry
    names = [
        "Product walkthrough.mp4",
        "Design assets.zip",
        "Research notes.pdf",
        "Project backup.zip",
        "Team photos.zip",
        "Release checklist.txt",
    ]
    now = time.time()
    for index in range(64):
        state = [
            TransferStatus.COMPLETED,
            TransferStatus.COMPLETED,
            TransferStatus.ERROR,
            TransferStatus.CANCELLED,
        ][index % 4]
        size = (index % 8 + 1) * 12 * 1024**2
        job = await registry.create("completed", total=size)
        job.created_at = now - 900 - index * 420
        job.context.update(
            kind="upload",
            source_kind="remote" if index % 3 == 0 else "upload",
            name=f"{index + 1:02d} - {names[index % len(names)]}",
            destination=destination,
        )
        job.context["showcase"] = True
        if state == TransferStatus.COMPLETED:
            name = job.context["name"]
            if name.endswith(".mp4"):
                content = (
                    Path(__file__).resolve().parents[1]
                    / "fixtures/movtext-subtitles.mp4"
                ).read_bytes()
            elif name.endswith(".pdf"):
                content = runtime.payloads[113]
            elif name.endswith(".zip"):
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr("Read me.txt", "TGDrive local demo archive.")
                content = output.getvalue()
            else:
                content = b"TGDrive local demo checklist.\n"
            identity = 600 + index
            runtime.payloads[identity] = content
            size = len(content)
            runtime.store.add_file(
                destination, name, identity, size, operation_id=job.id
            )
        job.finished_at = job.created_at + 65
        (
            await registry.update(
                job.id,
                total=size,
                status=state,
                phase=state.value,
                current=size if state == TransferStatus.COMPLETED else size // 3,
                error="Demo: Telegram connection interrupted. Retry is available."
                if state == TransferStatus.ERROR
                else None,
            )
        )
    demo_jobs = []
    pending = deque()
    for index, phase in enumerate(["uploading", "downloading", "queued", "queued"]):
        job = await registry.create(phase, total=(index + 1) * 256 * 1024**2)
        job.context.update(
            kind="upload",
            source_kind="remote" if phase == "downloading" else "upload",
            name=names[index],
            destination=destination,
            showcase_active=True,
        )
        demo_jobs.append(job.id)
        if phase == "queued":
            pending.append(job.id)
        (
            await registry.update(
                job.id,
                status=TransferStatus.QUEUED
                if phase == "queued"
                else TransferStatus.RUNNING,
                current=0 if phase == "queued" else job.total // (index + 3),
                phase=phase,
            )
        )
    runtime.showcase = True

    async def animate_transfers():
        tick = 0
        while True:
            await asyncio.sleep(2)
            tick += 1
            (await advance_transfers(registry, demo_jobs, pending, tick))

    runtime.showcase_task = asyncio.create_task(animate_transfers())


async def advance_transfers(registry, identities, pending, tick):
    """Cycle the four illustrative rows without accumulating fake history.

    Each completed stage advances or gives its slot to the oldest queued sample.
    Only explicitly seeded rows are touched; actual uploads and cancelled samples
    retain the normal registry lifecycle. API speed/ETA use the registry's ordinary
    measured-byte samples, rather than invented response fields.
    """
    jobs = {job.id: job for job in registry.list() if job.id in identities}
    for index, identity in enumerate(identities):
        job = jobs.get(identity)
        if (
            job is None
            or not job.context.get("showcase_active")
            or job.status != TransferStatus.RUNNING
            or job.task is not None
        ):
            continue
        if job.phase == "finalizing":
            (
                await registry.update(
                    job.id, status=TransferStatus.QUEUED, phase="queued", current=0
                )
            )
            pending.append(job.id)
            continue
        step = int((1.7 + 0.6 * math.sin(tick / 4 + index)) * 2 * 1024**2)
        current = min(job.total, job.current + step)
        if current == job.total and job.phase == "downloading":
            (await registry.update(job.id, phase="uploading", current=0))
        elif current == job.total:
            (await registry.update(job.id, phase="finalizing", current=current))
        else:
            (await registry.update(job.id, current=current))
    slots = 2 - sum(job.status == TransferStatus.RUNNING for job in jobs.values())
    while pending and slots > 0:
        job = jobs.get(pending.popleft())
        if job is None or job.status != TransferStatus.QUEUED:
            continue
        (
            await registry.update(
                job.id,
                status=TransferStatus.RUNNING,
                phase="downloading"
                if job.context.get("source_kind") == "remote"
                else "uploading",
                current=0,
            )
        )
        slots -= 1


def samples(start, end, count=100):
    result = []
    for i in range(count):
        at = start + (end - start) * i / max(1, count - 1)
        wave = 0.5 + 0.5 * math.sin(i / 7)
        result.append(
            dict(
                at=at,
                cpu_percent=12 + wave * 36,
                cpu_scope="cgroup",
                memory_bytes=int((1.3 + wave * 0.7) * 1024**3),
                memory_limit_bytes=4 * 1024**3,
                received_bytes_per_second=int((1 + wave * 6) * 1024**2),
                sent_bytes_per_second=int((0.5 + (1 - wave) * 3) * 1024**2),
                network_interface="demo0",
            )
        )
    return result


def install(app, runtime):
    @app.middleware("http")
    async def showcase(request, call_next):
        response = await call_next(request)
        if not getattr(runtime, "showcase", False) or response.status_code != 200:
            return response
        path = request.url.path
        if path not in {
            "/api/config",
            "/api/v1/performance",
            "/api/v1/performance/history",
        }:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        value = json.loads(body)
        now = time.time()
        if path == "/api/config":
            value["demo_data"] = True
        elif path.endswith("/history"):
            start, end = (
                float(request.query_params["start"]),
                float(request.query_params["end"]),
            )
            value.update(
                samples=samples(start, end),
                earliest_at=now - 31 * 86400,
                tracked_since=now - 31 * 86400,
                available=True,
                totals=dict(
                    network_received_bytes=154 * 1024**3,
                    network_sent_bytes=92 * 1024**3,
                    http_upload_bytes=36 * 1024**3,
                    http_download_bytes=58 * 1024**3,
                    telegram_upload_bytes=31 * 1024**3,
                    telegram_download_bytes=40 * 1024**3,
                    http_requests=8421,
                    http_errors=8,
                ),
            )
        else:
            rows = samples(now - 297, now)
            value["server"].update(
                history=rows,
                cpu=dict(
                    percent=rows[-1]["cpu_percent"], scope="cgroup", capacity_cores=4
                ),
            )
            value["server"]["memory"].update(
                cgroup_current=rows[-1]["memory_bytes"], cgroup_limit=4 * 1024**3
            )
            value["server"]["network"].update(
                interface="demo0",
                available=True,
                received_bytes=154 * 1024**3,
                sent_bytes=92 * 1024**3,
                received_bytes_per_second=rows[-1]["received_bytes_per_second"],
                sent_bytes_per_second=rows[-1]["sent_bytes_per_second"],
            )
            value["telegram"].update(
                active=3,
                queued=2,
                accounts=[
                    dict(
                        name="Demo storage bot 1",
                        kind="bot",
                        uploads=1,
                        downloads=1,
                        combined_limit=3,
                        cooldown_seconds=0,
                        requests=dict(
                            active=4,
                            queued=1,
                            configured_limit=8,
                            effective_limit=8,
                            configured_parts_per_second=48,
                            effective_parts_per_second=32,
                            floods=0,
                            failed=0,
                            completed=840,
                            bytes=121 * 1024**2,
                            upload_bytes=85 * 1024**2,
                            download_bytes=36 * 1024**2,
                        ),
                    ),
                    dict(
                        name="Demo storage bot 2",
                        kind="bot",
                        uploads=1,
                        downloads=0,
                        combined_limit=3,
                        cooldown_seconds=12,
                        requests=dict(
                            active=0,
                            queued=1,
                            configured_limit=8,
                            effective_limit=4,
                            configured_parts_per_second=48,
                            effective_parts_per_second=16,
                            floods=1,
                            failed=0,
                            completed=310,
                            bytes=54 * 1024**2,
                            upload_bytes=34 * 1024**2,
                            download_bytes=20 * 1024**2,
                        ),
                    ),
                ],
                queue=[
                    dict(
                        request_id="demo-queue-1",
                        position=1,
                        operation="download",
                        priority="demand",
                        waiting_seconds=4,
                        estimated_wait_seconds=8,
                    ),
                    dict(
                        request_id="demo-queue-2",
                        position=2,
                        operation="download",
                        priority="preload",
                        waiting_seconds=12,
                        estimated_wait_seconds=20,
                    ),
                ],
                history=[
                    dict(at=r["at"], active=1 + i % 4, queued=i % 3)
                    for i, r in enumerate(rows)
                ],
            )
            value["history_scope"] = (
                "Illustrative demo activity; not measurements of this laptop."
            )
        headers = {
            key: val
            for key, val in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        }
        return JSONResponse(value, headers=headers)
