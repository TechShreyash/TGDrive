"""Byte admission through real transfer adapters, independent of provider network."""

import asyncio
import time

import pytest

from tests.media.test_media_scheduler import wait_until
from tests.replication.test_replica_transport import BACKUP, started
from tests.transfers.test_upload_pipeline import PART, Native, client
from tgdrive.transfers.bandwidth import FileBandwidth
from tgdrive.transfers.file_window import FileWindow


@pytest.mark.asyncio
async def test_accounts_share_upload_admission_and_charge_outer_part_retries(tmp_path):
    bandwidth = FileBandwidth()
    await bandwidth.configure(2 * 1024**2, None)
    native_a, native_b = Native(), Native()
    native_a.fail_part = 0
    first, second = client(native_a), client(native_b)
    first.name, second.name = "storage-bot", "premium-user"
    first.bind_bandwidth(bandwidth)
    second.bind_bandwidth(bandwidth)
    payload = b"a" * (PART + 5)
    path = tmp_path / "parts.bin"
    path.write_bytes(payload)

    async def upload(account):
        with FileWindow(path, 0, len(payload)) as source:
            return await account._upload_parts(
                source, length=len(payload), file_name=path.name, progress=None
            )

    started_at = time.monotonic()
    await asyncio.gather(upload(first), upload(second))
    assert native_a.calls[0] == 2 and native_b.calls[0] == 1
    assert bandwidth.upload.admitted_bytes == 2 * len(payload) + PART
    assert (
        time.monotonic() - started_at
        >= bandwidth.upload.admitted_bytes / (2 * 1024**2) * 0.8
    )
    assert (
        b"".join(native_a.parts[index] for index in sorted(native_a.parts)) == payload
    )
    assert (
        b"".join(native_b.parts[index] for index in sorted(native_b.parts)) == payload
    )
    assert not native_a.published and not native_b.published


@pytest.mark.asyncio
async def test_cancel_during_upload_admission_never_dispatches_or_publishes(tmp_path):
    bandwidth = FileBandwidth()
    await bandwidth.configure(1024, None)
    native = Native()
    account = client(native)
    account.bind_bandwidth(bandwidth)
    path = tmp_path / "cancel.bin"
    path.write_bytes(b"x" * (2 * PART))
    task = asyncio.create_task(
        account.send_staged(
            1, path, file_name=path.name, random_id=123, generate_thumbnail=False
        )
    )
    await wait_until(lambda: bandwidth.upload.snapshot()["waiting"] > 0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not native.calls and not native.published
    assert bandwidth.upload.snapshot()["waiting"] == 0
    assert account._media_scheduler.active == 0


@pytest.mark.asyncio
async def test_independent_backup_bundle_counts_one_upload_and_its_verification_read(
    tmp_path,
):
    bandwidth = FileBandwidth()
    await bandwidth.configure(2048, 2048)
    transport, native = await started(bandwidth=bandwidth)
    payload = b"synthetic encrypted recovery capsule"
    path = tmp_path / "backup.tgdb"
    path.write_bytes(payload)
    try:
        await transport.publish(
            BACKUP, path, random_id=42, caption="synthetic", pin=True
        )
        assert native.body == payload
        assert bandwidth.upload.admitted_bytes == len(payload)
        assert bandwidth.download.admitted_bytes == len(payload)
        assert (
            bandwidth.upload.snapshot()["waiting"]
            == bandwidth.download.snapshot()["waiting"]
            == 0
        )
    finally:
        await transport.close()
