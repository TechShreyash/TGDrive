import asyncio, config
from pathlib import Path
from pyrogram import Client
from utils.logger import Logger

logger = Logger("clients")

multi_clients = {}
work_loads = {}


async def initialize_clients():
    global multi_clients, work_loads
    logger.info("Initializing Clients")

    all_tokens = dict((i, t) for i, t in enumerate(config.BOT_TOKENS, start=1))

    async def start_client(client_id, token):
        try:
            logger.info(f"Starting - Client {client_id}")

            client = await Client(
                name=str(client_id),
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                bot_token=token,
                sleep_threshold=config.SLEEP_THRESHOLD,
                workdir=Path("./cache"),
                no_updates=True,
                in_memory=not config.USE_SESSION_FILE,
            ).start()
            logger.info(f"Started - Client {client_id}")
            work_loads[client_id] = 0
            return client_id, client
        except Exception as e:
            logger.error(f"Failed To Start Client - {client_id} Error: {e}")
            return client_id, None

    clients = await asyncio.gather(
        *[start_client(client_id, client) for client_id, client in all_tokens.items()]
    )
    for client_id, client in clients:
        if client is not None:
            multi_clients[client_id] = client

    if len(multi_clients) == 0:
        logger.error("No Clients Were Initialized")
        exit(1)

    logger.info("Clients Initialized")


def get_client() -> Client:
    global multi_clients, work_loads

    index = min(work_loads, key=work_loads.get)
    work_loads[index] += 1
    return multi_clients[index]
