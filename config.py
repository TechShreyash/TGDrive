from dotenv import load_dotenv
import os

# Load environment variables from the .env file (if present)
load_dotenv()

# Password used to access the website's admin panel
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

# Telegram API_ID and API_HASH obtained from https://my.telegram.org/auth
API_ID = int(os.getenv("API_ID"))  # Your Telegram API ID
API_HASH = os.getenv("API_HASH")  # Your Telegram API Hash

# Time delay in seconds before retrying after a Telegram API floodwait error
SLEEP_THRESHOLD = int(os.getenv("SLEEP_THRESHOLD", 60))  # 1 minute

# Choose whether to use .session files for session persistence or in-memory sessions
USE_SESSION_FILE = bool(
    os.getenv("USE_SESSION_FILE", True)
)  # Set to False to use in-memory sessions

# List of Telegram bot tokens used for file upload/download operations
BOT_TOKENS = os.getenv("BOT_TOKENS", "").strip(", ").split(",")
BOT_TOKENS = [token.strip() for token in BOT_TOKENS]


# Maximum file size (in GBs) allowed for uploading to Telegram
MAX_FILE_SIZE = float(os.getenv("MAX_FILE_SIZE", 1.98)) * 1024 * 1024 * 1024

# Chat ID of the Telegram storage channel where files will be stored
STORAGE_CHANNEL = int(os.getenv("STORAGE_CHANNEL"))  # Your storage channel's chat ID

# Message ID of a file in the storage channel used for storing database backups
DATABASE_BACKUP_MSG_ID = int(os.getenv("DATABASE_BACKUP_MSG_ID"))

# Database backup interval in seconds. Backups will be sent to the storage channel at this interval
DATABASE_BACKUP_TIME = int(os.getenv("DATABASE_BACKUP_TIME", 60))  # 1 minute
