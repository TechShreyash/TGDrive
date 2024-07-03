import os
import aiohttp, asyncio
from utils.extra import get_filename
from utils.logger import Logger
from pathlib import Path
from utils.uploader import start_file_uploader
from techzdl import TechZDL

logger = Logger(__name__)

DOWNLOAD_PROGRESS = {}
STOP_DOWNLOAD = []

cache_dir = Path("./cache")
cache_dir.mkdir(parents=True, exist_ok=True)


async def download_progress_callback(status, current, total, id):
    global DOWNLOAD_PROGRESS

    DOWNLOAD_PROGRESS[id] = (
        status,
        current,
        total,
    )


async def download_file(url, id, path, filename, singleThreaded):
    global DOWNLOAD_PROGRESS, STOP_DOWNLOAD

    logger.info(f"Downloading file from {url}")

    try:
        downloader = TechZDL(
            url,
            output_dir=cache_dir,
            debug=False,
            progress_callback=download_progress_callback,
            progress_args=(id,),
            max_retries=5,
            single_threaded=singleThreaded,
        )
        await downloader.start(in_background=True)

        await asyncio.sleep(5)

        while downloader.is_running:
            if id in STOP_DOWNLOAD:
                logger.info(f"Stopping download {id}")
                await downloader.stop()
                return
            await asyncio.sleep(1)

        if downloader.download_success is False:
            raise downloader.download_error

        DOWNLOAD_PROGRESS[id] = (
            "completed",
            downloader.total_size,
            downloader.total_size,
        )

        logger.info(f"File downloaded to {downloader.output_path}")

        asyncio.create_task(
            start_file_uploader(
                downloader.output_path, id, path, filename, downloader.total_size
            )
        )
    except Exception as e:
        DOWNLOAD_PROGRESS[id] = ("error", 0, 0)
        logger.error(f"Failed to download file: {url} {e}")


async def get_file_info_from_url(url):
    downloader = TechZDL(
        url,
        output_dir=cache_dir,
        debug=False,
        progress_callback=download_progress_callback,
        progress_args=(id,),
        max_retries=5,
    )
    file_info = await downloader.get_file_info()
    return {"file_size": file_info["total_size"], "file_name": file_info["filename"]}
