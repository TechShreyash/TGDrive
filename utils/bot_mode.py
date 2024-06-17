import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import config
from utils.logger import Logger
from pathlib import Path

logger = Logger(__name__)

START_CMD = """🚀 **Welcome To TG Drive's Bot Mode**

You can use this bot to upload files to your TG Drive website directly instead of doing it from website.

🗄 **Commands:**
/set_folder - Set folder for file uploads
/current_folder - Check current folder

📤 **How To Upload Files:** Send a file to this bot and it will be uploaded to your TG Drive website. You can also set a folder for file uploads using /set_folder command.

Read more about [TG Drive's Bot Mode](https://github.com/TechShreyash/TGDrive#tg-drives-bot-mode)
"""

SET_FOLDER_PATH_CACHE = {}  # Cache to store folder path for each folder id
DRIVE_DATA = None
BOT_MODE = None

session_cache_path = Path(f"./cache")
session_cache_path.parent.mkdir(parents=True, exist_ok=True)

main_bot = Client(
    name="main_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.MAIN_BOT_TOKEN,
    sleep_threshold=config.SLEEP_THRESHOLD,
    workdir=session_cache_path,
)


@main_bot.on_message(
    filters.command(["start", "help"])
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def start_handler(client: Client, message: Message):
    await message.reply_text(START_CMD)


@main_bot.on_message(
    filters.command("set_folder")
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def set_folder_handler(client: Client, message: Message):
    global SET_FOLDER_PATH_CACHE, DRIVE_DATA

    while True:
        try:
            folder_name = await message.ask(
                "Send the folder name where you want to upload files\n\n/cancel to cancel",
                timeout=60,
                filters=filters.text,
            )
        except asyncio.TimeoutError:
            await message.reply_text("Timeout\n\nUse /set_folder to set folder again")
            return

        if folder_name.text.lower() == "/cancel":
            await message.reply_text("Cancelled")
            return

        folder_name = folder_name.text.strip()
        search_result = DRIVE_DATA.search_file_folder(folder_name)

        # Get folders from search result
        folders = {}
        for item in search_result.values():
            if item.type == "folder":
                folders[item.id] = item

        if len(folders) == 0:
            await message.reply_text(f"No Folder found with name {folder_name}")
        else:
            break

    buttons = []
    folder_cache = {}
    folder_cache_id = len(SET_FOLDER_PATH_CACHE) + 1

    for folder in search_result.values():
        path = folder.path.strip("/")
        folder_path = "/" + ("/" + path + "/" + folder.id).strip("/")
        folder_cache[folder.id] = (folder_path, folder.name)
        buttons.append(
            [
                InlineKeyboardButton(
                    folder.name,
                    callback_data=f"set_folder_{folder_cache_id}_{folder.id}",
                )
            ]
        )
    SET_FOLDER_PATH_CACHE[folder_cache_id] = folder_cache

    await message.reply_text(
        "Select the folder where you want to upload files",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@main_bot.on_callback_query(
    filters.user(config.TELEGRAM_ADMIN_IDS) & filters.regex(r"set_folder_")
)
async def set_folder_callback(client: Client, callback_query: Message):
    global SET_FOLDER_PATH_CACHE, BOT_MODE

    folder_cache_id, folder_id = callback_query.data.split("_")[2:]

    folder_path_cache = SET_FOLDER_PATH_CACHE.get(int(folder_cache_id))
    if folder_path_cache is None:
        await callback_query.answer("Request Expired, Send /set_folder again")
        await callback_query.message.delete()
        return

    folder_path, name = folder_path_cache.get(folder_id)
    del SET_FOLDER_PATH_CACHE[int(folder_cache_id)]
    BOT_MODE.set_folder(folder_path, name)

    await callback_query.answer(f"Folder Set Successfully To : {name}")
    await callback_query.message.edit(
        f"Folder Set Successfully To : {name}\n\nNow you can send / forward files to me and it will be uploaded to this folder."
    )


@main_bot.on_message(
    filters.command("current_folder")
    & filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS),
)
async def current_folder_handler(client: Client, message: Message):
    global BOT_MODE

    await message.reply_text(f"Current Folder: {BOT_MODE.current_folder_name}")


# Handling when any file is sent to the bot
@main_bot.on_message(
    filters.private
    & filters.user(config.TELEGRAM_ADMIN_IDS)
    & (
        filters.document
        | filters.video
        | filters.audio
        | filters.photo
        | filters.sticker
    )
)
async def file_handler(client: Client, message: Message):
    global BOT_MODE, DRIVE_DATA

    copied_message = await message.copy(config.STORAGE_CHANNEL)
    file = (
        copied_message.document
        or copied_message.video
        or copied_message.audio
        or copied_message.photo
        or copied_message.sticker
    )

    DRIVE_DATA.new_file(
        BOT_MODE.current_folder,
        file.file_name,
        copied_message.id,
        file.file_size,
    )

    await message.reply_text(
        f"""✅ File Uploaded Successfully To Your TG Drive Website
                             
**File Name:** {file.file_name}
**Folder:** {BOT_MODE.current_folder_name}
"""
    )


async def start_bot_mode(d, b):
    global DRIVE_DATA, BOT_MODE
    DRIVE_DATA = d
    BOT_MODE = b

    logger.info("Starting Main Bot")
    await main_bot.start()

    await main_bot.send_message(
        config.STORAGE_CHANNEL, "Main Bot Started -> TG Drive's Bot Mode Enabled"
    )
    logger.info("Main Bot Started")
    logger.info("TG Drive's Bot Mode Enabled")
