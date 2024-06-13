import os
import aiohttp, aiofiles, asyncio
from utils.extra import get_filename
from utils.logger import Logger
from pathlib import Path
from utils.uploader import start_file_uploader

logger = Logger(__name__)

DOWNLOAD_PROGRESS = {}
STOP_DOWNLOAD = []


async def download_file(url, id, path, filename):
    global DOWNLOAD_PROGRESS, STOP_DOWNLOAD

    cache_dir = Path("./cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Downloading file from {url}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                total_size = int(response.headers["Content-Length"])
                ext = filename.lower().split(".")[-1]
                file_location = cache_dir / f"{id}.{ext}"

                size_downloaded = 0

                async with aiofiles.open(file_location, "wb") as f:
                    while True:
                        if id in STOP_DOWNLOAD:
                            logger.info(f"Stopping download {id}")
                            try:
                                await f.close()
                                os.remove(file_location)
                            except:
                                pass
                            return

                        chunk = await response.content.read(1024)
                        if not chunk:
                            break
                        size_downloaded += len(chunk)
                        DOWNLOAD_PROGRESS[id] = (
                            "running",
                            size_downloaded,
                            total_size,
                        )
                        await f.write(chunk)

                DOWNLOAD_PROGRESS[id] = ("completed", total_size, total_size)
                logger.info(f"File downloaded to {file_location}")

                asyncio.create_task(
                    start_file_uploader(file_location, id, path, filename, total_size)
                )
    except Exception as e:
        DOWNLOAD_PROGRESS[id] = ("error", 0, 0)
        logger.error(f"Failed to download file: {url} {e}")


async def get_file_info_from_url(url):
    logger.info(f"Getting file info from {url}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            logger.info(str(response.headers))
            filename = get_filename(response.headers, url)

            try:
                size = int(response.headers["Content-Length"])
                if size == 0:
                    raise Exception("File size is 0")
            except:
                raise Exception(
                    "Failed to get file size, Content-Length Headers Not Found"
                )

            logger.info(f"Got file info from url: {filename} ({size} bytes)")
            return {"file_size": size, "file_name": filename}
