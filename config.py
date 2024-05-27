# Password will be used to access the website
ADMIN_PASSWORD = "admin_password"

# Telegram API_ID and API_HASH
API_ID = 12345678
API_HASH = "api_hash"

# Floodwait retry delay
SLEEP_THRESHOLD = 60  # 1 minute

# To use .session files or not
USE_SESSION_FILE = True  # False if you want to use in-memory sessions

# List of bot tokens which will be used to upload/download files from Telegram
BOT_TOKENS = ["bot_token"]
# You can add multiple bot tokens like this: BOT_TOKENS = ["bot_token1", "bot_token2", "bot_token3", "bot_token4", "bot_token5"]

# Maximum file size that can be uploaded to Telegram is 2GB
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# Chat id of the storage channel where the files will be stored
STORAGE_CHANNEL = -1001234567890

# Message id of a file stored in the storage channel which will be used to store the database backups
DATABASE_BACKUP_MSG_ID = 123

# Database backup interval in seconds. After every interval, the database will be backed up to the storage channel
DATABASE_BACKUP_TIME = 60  # 1 minute
