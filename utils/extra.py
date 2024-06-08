from pathlib import Path
from datetime import datetime, timezone
from config import WEBSITE_URL
import asyncio, aiohttp
from utils.logger import Logger

logger = Logger(__name__)


def convert_class_to_dict(data, isObject, showtrash=False):
    if isObject == True:
        data = data.__dict__.copy()
    new_data = {"contents": {}}

    for key in data["contents"]:
        if data["contents"][key].trash == showtrash:
            if data["contents"][key].type == "folder":
                folder = data["contents"][key]
                new_data["contents"][key] = {
                    "name": folder.name,
                    "type": folder.type,
                    "id": folder.id,
                    "path": folder.path,
                    "upload_date": folder.upload_date,
                }
            else:
                file = data["contents"][key]
                new_data["contents"][key] = {
                    "name": file.name,
                    "type": file.type,
                    "size": file.size,
                    "id": file.id,
                    "path": file.path,
                    "upload_date": file.upload_date,
                }
    return new_data


def get_current_utc_time():
    return datetime.now(timezone.utc).strftime("Date - %Y-%m-%d | Time - %H:%M:%S")


async def auto_ping_website():
    if WEBSITE_URL is not None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(WEBSITE_URL) as response:
                        if response.status == 200:
                            logger.info(f"Pinged website at {get_current_utc_time()}")
                        else:
                            logger.warning(f"Failed to ping website: {response.status}")
                except Exception as e:
                    logger.warning(f"Failed to ping website: {e}")

                await asyncio.sleep(60)  # Ping website every minute


def reset_cache_dir():
    cache_dir = Path("./cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    for file_path in cache_dir.iterdir():
        if file_path.is_file() and file_path.suffix not in [
            ".session",
            ".session-journal",
            ".data",
        ]:
            try:
                file_path.unlink()
            except:
                pass


import re
import urllib.parse


def parse_content_disposition(content_disposition):
    # Split the content disposition into parts
    parts = content_disposition.split(";")

    # Initialize filename variable
    filename = None

    # Loop through parts to find the filename
    for part in parts:
        part = part.strip()
        if part.startswith("filename="):
            # If filename is found
            filename = part.split("=", 1)[1]
        elif part.startswith("filename*="):
            # If filename* is found
            match = re.match(r"filename\*=(\S*)''(.*)", part)
            if match:
                encoding, value = match.groups()
                try:
                    filename = urllib.parse.unquote(value, encoding=encoding)
                except ValueError:
                    # Handle invalid encoding
                    pass

    if filename is None:
        raise Exception("Failed to get filename")
    return filename
