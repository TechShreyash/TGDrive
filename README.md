# TGDrive - A Google Drive Clone with Telegram Storage

Welcome to TGDrive! This project is a web application that replicates Google Drive's functionalities using Telegram as its storage backend. Users can manage folders and files, perform actions like uploading, renaming, and deleting, utilize trash/bin support, enable permanent deletion, and share public links. The application offers admin login and automatically backs up the database to Telegram.

![Total Repo Views](https://stats.techzbots.co/api/views_badge?page=https%3A%2F%2Fgithub.com%2FTechShreyash%2FTGDrive&color1=394066&color2=fd3201&label=Total%20Repo%20Views&counter_type=1)

## Features

- **File Management:** Upload, rename, and delete files with integrated trash/bin functionality and support for permanent deletion.
- **Folder Management:** Create, rename, and delete folders with ease.
- **Sharing:** Seamlessly share public links for files and folders.
- **Admin Support:** Secure admin login for efficient application management.
- **Automatic Backups:** Automated database backups sent directly to Telegram.
- **Multiple Bots/Clients:** Supports multiple bots/clients for file operations and streaming from Telegram.
- **Large File Support:** Upload files up to 4GB using Telegram Premium accounts, ensuring you can handle larger data needs.

## Tech Stack

- **Backend:** Python, FastAPI
- **Frontend:** HTML, CSS, JavaScript
- **Database:** Data stored locally as a class object, saved to a file using the pickle module.
- **Storage:** Telegram

## Demo

Explore the live demo of TGDrive:

- **Website:** [TGDrive Demo](https://tgdrive.techzbots.co)
- **Password:** admin

Feel free to test out the features with the provided login credentials.

## Todo List

- [x] 4gb file upload using telegram premium account
- [ ] Add search feature for files/folders
- [ ] Video player and image viewer support on website
- [ ] Remote URL upload support
- [ ] Folder/File sorting according to name/date uploaded

## Deploying Your Own

### 1. Clone the repository

```bash
git clone https://github.com/TechShreyash/TGDrive
cd TGDrive
```

### 2. Set up your environment variables

Create a `.env` file in the root directory and add the following environment variables:

> Note: You can also add environment variables directly in some hosting services instead of creating a `.env` file.

#### Required Variables

| Variable Name            | Type    | Example                   | Description                                                                                |
| ------------------------ | ------- | ------------------------- | ------------------------------------------------------------------------------------------ |
| `API_ID`                 | integer | 123456                    | Your Telegram API ID                                                                       |
| `API_HASH`               | string  | dagsjdhgjfsahgjfh         | Your Telegram API Hash                                                                     |
| `BOT_TOKENS`             | string  | 21413535:gkdshajfhjfakhjf | List of Telegram bot tokens used for file upload/download operations, separated by commas. |
| `STORAGE_CHANNEL`        | integer | -100123456789             | Chat ID of the Telegram storage channel where files will be stored                         |
| `DATABASE_BACKUP_MSG_ID` | integer | 123                       | Message ID of a file in the storage channel used for storing database backups              |

> Note: To use multiple bot tokens for file upload/download operations, add multiple bot tokens separated by commas in the `BOT_TOKENS` variable. Example: `bot_token1,bot_token2,bot_token3,...`

> Note: All bots mentioned in the `BOT_TOKENS` variable must be added as admins in your `STORAGE_CHANNEL`.

> Note: `DATABASE_BACKUP_MSG_ID` must be the message ID of a file (document) in the `STORAGE_CHANNEL`. Just upload any new file to the channel and get its file ID.

#### Optional Variables

| Variable Name          | Type                 | Default                                    | Description                                                                                        |
| ---------------------- | -------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `ADMIN_PASSWORD`       | string               | admin                                      | Password used to access the website's admin panel                                                  |
| `STRING_SESSIONS`      | string               | None                                       | List of Premium Telegram Account Pyrogram String Sessions used for file upload/download operations |
| `SLEEP_THRESHOLD`      | integer (in seconds) | 60                                         | Time delay in seconds before retrying after a Telegram API floodwait error                         |
| `DATABASE_BACKUP_TIME` | integer (in seconds) | 60                                         | Database backup interval in seconds. Backups will be sent to the storage channel at this interval  |
| `USE_SESSION_FILE`     | bool (True/False)    | True                                       | Choose whether to use `.session` files for session persistence or in-memory sessions               |
| `MAX_FILE_SIZE`        | float (in GBs)       | 1.98 (3.98 if `STRING_SESSIONS` are added) | Maximum file size (in GBs) allowed for uploading to Telegram                                       |

### 3. Install/Update Dependencies

```bash
pip install -U -r requirements.txt
```

### 4. Run the FastAPI application

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Access the application at `http://127.0.0.1:8000`.

> Note: For deploying the application publicly, refer to online guides on running a FastAPI app.

## Contributing

Contributions are welcome! Fork the repository, make your changes, and create a pull request.

## License

This project is licensed under the MIT License. Refer to the [LICENSE](LICENSE) file for details.

## Support

For inquiries or support, join our [Telegram Support Group](https://telegram.me/TechZBots_Support) or email [techshreyash123@gmail.com](mailto:techshreyash123@gmail.com).
