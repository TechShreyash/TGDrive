from utils.clients import get_client
from pyrogram import Client
from pyrogram.types import Message
from config import STORAGE_CHANNEL
import os
from utils.logger import Logger

logger = Logger("Uploader")
PROGRESS_CACHE = {}
STOP_TRANSMISSION = []


async def progress_callback(current, total, id, client: Client):
    global PROGRESS_CACHE, STOP_TRANSMISSION

    PROGRESS_CACHE[id] = ("running", current, total)
    if id in STOP_TRANSMISSION:
        client.stop_transmission()


async def start_file_uploader(file_path, id, directory_path, filename):
    global PROGRESS_CACHE
    from utils.directoryHandler import DRIVE_DATA

    logger.info(f"Uploading file {file_path} {id}")
    client: Client = get_client(for_upload=True)
    PROGRESS_CACHE[id] = ("running", 0, 0)

    message: Message = await client.send_document(
        STORAGE_CHANNEL,
        file_path,
        progress=progress_callback,
        progress_args=(id, client),
    )
    size = message.document.file_size

    DRIVE_DATA.new_file(directory_path, filename, message.id, size)
    PROGRESS_CACHE[id] = ("completed", size, size)

    os.remove(file_path)
    logger.info(f"Uploaded file {file_path} {id}")
