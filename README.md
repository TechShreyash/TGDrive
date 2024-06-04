# TGDrive - A Google Drive Clone with Telegram Storage

Welcome to TGDrive! This web application replicates Google Drive's functionalities using Telegram as its storage backend. Manage folders and files, perform actions like uploading, renaming, and deleting, utilize trash/bin support, enable permanent deletion, and share public links. The application offers admin login and automatically backs up the database to Telegram.

![Total Repo Views](https://stats.techzbots.co/api/views_badge?page=https%3A%2F%2Fgithub.com%2FTechShreyash%2FTGDrive&color1=394066&color2=fd3201&label=Total%20Repo%20Views&counter_type=1)

## Features

- **File Management:** Upload, rename, and delete files with integrated trash/bin functionality and permanent deletion support.
- **Folder Management:** Easily create, rename, and delete folders.
- **Sharing:** Seamlessly share public links for files and folders.
- **Admin Support:** Secure admin login for efficient management.
- **Automatic Backups:** Automated database backups sent directly to Telegram.
- **Multiple Bots/Clients:** Support for multiple bots/clients for file operations and streaming from Telegram.
- **Large File Support:** Upload files up to 4GB using Telegram Premium accounts.
- **Auto Pinger:** Built-in feature to keep the website active by preventing idle timeouts.

## Tech Stack

- **Backend:** Python, FastAPI
- **Frontend:** HTML, CSS, JavaScript
- **Database:** Local storage as a class object, saved to a file using the pickle module.
- **Storage:** Telegram

## Demo

Explore the live demo of TGDrive:

- **Website:** [TGDrive Demo](https://tgdrive.techzbots.co)
- **Password:** admin

Feel free to test the features with the provided login credentials.

## Todo List

- [x] 4GB file upload using Telegram Premium account
- [x] Folder/File sorting by date/time uploaded (Newest First)
- [x] Add search feature for files/folders
- [ ] Video player and image viewer support on website
- [ ] Remote URL upload support

## Deploying Your Own

### 1. Clone the Repository

```bash
git clone https://github.com/TechShreyash/TGDrive
cd TGDrive
```

### 2. Set Up Your Environment Variables

Create a `.env` file in the root directory and add the following environment variables:

> Note: You can also set environment variables directly in some hosting services instead of creating a `.env` file.

#### Required Variables

| Variable Name            | Type    | Example                   | Description                                                          |
| ------------------------ | ------- | ------------------------- | -------------------------------------------------------------------- |
| `API_ID`                 | integer | 123456                    | Your Telegram API ID                                                 |
| `API_HASH`               | string  | dagsjdhgjfsahgjfh         | Your Telegram API Hash                                               |
| `BOT_TOKENS`             | string  | 21413535:gkdshajfhjfakhjf | List of Telegram bot tokens for file operations, separated by commas |
| `STORAGE_CHANNEL`        | integer | -100123456789             | Chat ID of the Telegram storage channel                              |
| `DATABASE_BACKUP_MSG_ID` | integer | 123                       | Message ID of a file in the storage channel for database backups     |

> Note: All bots mentioned in the `BOT_TOKENS` variable must be added as admins in your `STORAGE_CHANNEL`.

> Note: `DATABASE_BACKUP_MSG_ID` should be the message ID of a file (document) in the `STORAGE_CHANNEL`.

#### Optional Variables

| Variable Name          | Type                 | Default                                    | Description                                                                   |
| ---------------------- | -------------------- | ------------------------------------------ | ----------------------------------------------------------------------------- |
| `ADMIN_PASSWORD`       | string               | admin                                      | Password for accessing the admin panel                                        |
| `STRING_SESSIONS`      | string               | None                                       | List of Premium Telegram Account Pyrogram String Sessions for file operations |
| `SLEEP_THRESHOLD`      | integer (in seconds) | 60                                         | Delay in seconds before retrying after a Telegram API floodwait error         |
| `DATABASE_BACKUP_TIME` | integer (in seconds) | 60                                         | Interval in seconds for database backups to the storage channel               |
| `MAX_FILE_SIZE`        | float (in GBs)       | 1.98 (3.98 if `STRING_SESSIONS` are added) | Maximum file size (in GBs) allowed for uploading to Telegram                  |
| `WEBSITE_URL`          | string               | None                                       | Website url (with https/http) to auto-ping to keep the website active         |

> Note: Premium Client (`STRING_SESSIONS`) will be used only to upload when file size is greater than 2GB.

> Note: File streaming/downloads will be done by bots (`BOT_TOKENS`).

### 3. Install/Update Dependencies

```bash
pip install -U -r requirements.txt
```

### 4. Run the FastAPI Application

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Access the application at `http://127.0.0.1:8000`.

> Note: For public deployment, refer to online guides on deploying FastAPI apps.

## Deploy Tutorials

**Deploy To Render.com For Free :** https://youtu.be/S5OIi5Ur3c0

<div align="center">

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/TechShreyash/TGDrive)

</div>

## Contributing

Contributions are welcome! Fork the repository, make your changes, and create a pull request.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Support

For inquiries or support, join our [Telegram Support Group](https://telegram.me/TechZBots_Support) or email [techshreyash123@gmail.com](mailto:techshreyash123@gmail.com).
