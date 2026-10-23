from __future__ import annotations

import asyncio

from test_transfers import make_manager

from tgdrive.tasks import TransferStatus
from tgdrive.uploads import ResumableUploads


async def test_receiving_more_files_does_not_wait_for_telegram_or_the_whole_batch(
    tmp_path,
):
    manager, store, client, pool = await make_manager(tmp_path)
    uploads = manager.uploads = ResumableUploads(manager.settings, manager)
    client.wait_during_upload = True
    waiting = uploads.create("still-on-device.bin", "/", 3)
    jobs = []
    try:
        first = uploads.create("first.bin", "/", 3)
        await uploads.append(first["id"], 0, b"one")
        jobs.append(await uploads.complete(first["id"]))
        # Telegram starts while another batch member has not sent any bytes.
        await asyncio.wait_for(client.started_upload.wait(), 3)

        async def receive_remaining():
            for name, content in (("second.bin", b"two"), ("third.bin", b"end")):
                item = uploads.create(name, "/", len(content))
                await uploads.append(item["id"], 0, content)
                jobs.append(await uploads.complete(item["id"]))

        # One Telegram upload holds the only client lease. Device receipts and
        # their durable queue acknowledgements must still finish independently.
        await asyncio.wait_for(receive_remaining(), 3)
        assert len(jobs) == 3
        assert all(job.task is not None and not job.task.done() for job in jobs)
        assert all(uploads.get(job.id)["state"] == "queued" for job in jobs)
        assert uploads.get(waiting["id"])["offset"] == 0
        assert store.list_directory("/") == {}

        # The request that received each file has returned. Only registry-owned
        # tasks remain, and those finish without further browser activity.
        client.release_upload.set()
        await asyncio.wait_for(asyncio.gather(*(job.task for job in jobs)), 3)
        assert all(job.status is TransferStatus.COMPLETED for job in jobs)
        assert {item["name"] for item in store.list_directory("/").values()} == {
            "first.bin",
            "second.bin",
            "third.bin",
        }
        assert all(uploads.get(job.id)["state"] == "finished" for job in jobs)
        assert uploads.get(waiting["id"])["state"] == "receiving"
    finally:
        client.release_upload.set()
        await manager.close()
        await pool.stop()
        uploads.close()
        store.close()
