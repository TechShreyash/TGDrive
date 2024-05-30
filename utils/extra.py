from datetime import datetime, timezone
from config import WEBSITE_URL
import aiohttp, asyncio
from utils.logger import Logger

logger = Logger(__name__)


def convert_class_to_dict(data, showtrash=False):
    if showtrash == False:
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
