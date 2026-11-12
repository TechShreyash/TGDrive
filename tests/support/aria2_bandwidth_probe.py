"""Exercise one owned aria2 engine's caps without torrent sources or peer traffic."""

import asyncio
import json
import tempfile
from pathlib import Path

from tgdrive.transfers.bandwidth import FileBandwidth
from tgdrive.transfers.torrent import _Aria2


async def main():
    budget = FileBandwidth()
    await budget.configure(1024 * 1024, 2 * 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix="tgdrive-aria2-cap-probe-") as directory:
        async with _Aria2(Path(directory), bandwidth=budget) as engine:
            first = await engine.call("getGlobalOption")
            assert first["max-overall-upload-limit"] == str(512 * 1024)
            assert first["max-overall-download-limit"] == str(1024 * 1024)
            assert budget.upload.rate == 512 * 1024
            assert budget.download.rate == 1024 * 1024
            await budget.configure(2048, 4096)
            changed = await engine.call("getGlobalOption")
            assert changed["max-overall-upload-limit"] == "1024"
            assert changed["max-overall-download-limit"] == "2048"
            assert budget.upload.rate == 1024 and budget.download.rate == 2048
            process = engine.process
            assert process is not None
            identity = process.pid
        assert process.returncode is not None
        assert budget.snapshot()["native_engines"] == 0
        assert budget.upload.rate == 2048 and budget.download.rate == 4096
    return {
        "scope": (
            "Owned isolated aria2 RPC/configuration only; "
            "no addUri/addTorrent or peer payload"
        ),
        "engine_pid": identity,
        "initial_native_upload_bytes_per_second": 512 * 1024,
        "initial_native_download_bytes_per_second": 1024 * 1024,
        "changed_native_upload_bytes_per_second": 1024,
        "changed_native_download_bytes_per_second": 2048,
        "stopped": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), sort_keys=True))
