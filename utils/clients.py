import asyncio, config
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
                workdir="./cache",
                no_updates=True,
                in_memory=not config.USE_SESSION_FILE,
            ).start()
            logger.info(f"Started - Client {client_id}")
            work_loads[client_id] = 0
            return client_id, client
        except Exception as e:
            logger.error(f"Failed To Start Client - {client_id} Error: {e}")

    clients = await asyncio.gather(
        *[start_client(i, token) for i, token in all_tokens.items()]
    )
    multi_clients.update(dict(clients))

    logger.info("Clients Initialized")

def get_client() -> Client:
    global multi_clients, work_loads

    index = min(work_loads, key=work_loads.get)
    work_loads[index] += 1
    return multi_clients[index]


