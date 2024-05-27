import logging, asyncio

logging.basicConfig(
    filename="logs.txt", filemode="w", format="%(name)s - %(levelname)s - %(message)s"
)

LOG_UPDATES = []


class ListHandler(logging.Handler):
    def emit(self, record):
        global LOG_UPDATES
        # Append each log message to the list
        log_message = self.format(record)
        LOG_UPDATES.append(log_message)


class Logger:
    def __init__(self, name, level=logging.DEBUG):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.formatter = logging.Formatter("%(name)s - %(levelname)s - %(message)s")

        # StreamHandler for console output
        self.stream_handler = logging.StreamHandler()
        self.stream_handler.setFormatter(self.formatter)
        self.logger.addHandler(self.stream_handler)

        # Custom ListHandler to capture log messages in a list
        # self.list_handler = ListHandler()
        # self.list_handler.setFormatter(self.formatter)
        # self.logger.addHandler(self.list_handler)

    def debug(self, message):
        self.logger.debug(message)

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)


# For future use, for showing logs in a channel
async def log_updater(logger_bot):
    global LOG_UPDATES
    while True:
        if len(LOG_UPDATES) == 0:
            await asyncio.sleep(1)
            continue

        message = LOG_UPDATES.pop(0)

        try:
            await logger_bot.send_message(
                "LOGGER_CHANNEL", message, disable_web_page_preview=True
            )
        except Exception as e:
            pass
