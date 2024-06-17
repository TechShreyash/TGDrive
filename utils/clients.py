import asyncio, config
from pathlib import Path
from pyrogram import Client
from utils.directoryHandler import backup_drive_data, loadDriveData
from utils.logger import Logger

logger = Logger(__name__)

multi_clients = {}
premium_clients = {}
work_loads = {}
premium_work_loads = {}
main_bot = None


async def initialize_clients():
    global multi_clients, work_loads, premium_clients, premium_work_loads
    logger.info("Initializing Clients")

    session_cache_path = Path(f"./cache")
    session_cache_path.parent.mkdir(parents=True, exist_ok=True)

    all_tokens = dict((i, t) for i, t in enumerate(config.BOT_TOKENS, start=1))
    all_sessions = dict(
        (i, s) for i, s in enumerate(config.STRING_SESSIONS, start=len(all_tokens) + 1)
    )

    async def start_client(client_id, token, type):
        try:
            logger.info(f"Starting - {type.title()} Client {client_id}")

            if type == "bot":
                client = await Client(
                    name=str(client_id),
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    bot_token=token,
                    sleep_threshold=config.SLEEP_THRESHOLD,
                    workdir=session_cache_path,
                    no_updates=True,
                ).start()
                await client.send_message(
                    config.STORAGE_CHANNEL,
                    f"Started - {type.title()} Client {client_id}",
                )
                multi_clients[client_id] = client
                work_loads[client_id] = 0
            elif type == "user":
                client = await Client(
                    name=str(client_id),
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    session_string=token,
                    sleep_threshold=config.SLEEP_THRESHOLD,
                    workdir=session_cache_path,
                    no_updates=True,
                ).start()
                await client.send_message(
                    config.STORAGE_CHANNEL,
                    f"Started - {type.title()} Client {client_id}",
                )
                premium_clients[client_id] = client
                premium_work_loads[client_id] = 0

            logger.info(f"Started - {type.title()} Client {client_id}")
        except Exception as e:
            logger.error(
                f"Failed To Start {type.title()} Client - {client_id} Error: {e}"
            )

    await asyncio.gather(
        *(
            [
                start_client(client_id, client, "bot")
                for client_id, client in all_tokens.items()
            ]
            + [
                start_client(client_id, client, "user")
                for client_id, client in all_sessions.items()
            ]
        )
    )
    if len(multi_clients) == 0:
        logger.error("No Clients Were Initialized")
        exit(1)
    if len(premium_clients) == 0:
        logger.info("No Premium Clients Were Initialized")

    logger.info("Clients Initialized")

    # Load the drive data
    await loadDriveData()

    # Start the backup drive data task
    asyncio.create_task(backup_drive_data())


def get_client(premium_required=False) -> Client:
    global multi_clients, work_loads, premium_clients, premium_work_loads

    if premium_required:
        index = min(premium_work_loads, key=premium_work_loads.get)
        premium_work_loads[index] += 1
        return premium_clients[index]

    index = min(work_loads, key=work_loads.get)
    work_loads[index] += 1
    return multi_clients[index]
