# TGDrive

Your own web file manager, with **Telegram for files** and **MongoDB for the file list**.

Upload files or folders, preview documents and media, share links, and download
originals. Files above 2 GB are split automatically while appearing as one item.

## Start here

1. Prepare a MongoDB database, a storage bot and a private Telegram channel.
2. Open a Linux/WSL terminal.
3. Clone the beta and run the guided Docker setup:

~~~bash
git clone --branch dev https://github.com/TechShreyash/TGDrive.git
cd TGDrive
python3 scripts/setup.py
~~~

The wizard checks access, creates private configuration and starts the app.

**[Follow the setup guide](docs/setup.md)** for prerequisites and steps.
Then read **[Using TGDrive](docs/usage.md)**.

## Keep your drive accessible

- Reinstalling can reopen the same MongoDB database.
- Complete encrypted database backups also go to Telegram.
- Save the recovery key somewhere separate.
- Wait for an upload to finish before removing its local source or temporary files.

## Project status

This is a **public beta for testing**, available on the `dev` branch. Keep
independent copies of important files and save your recovery key. Start with
disposable files while evaluating uploads, downloads and recovery.

Run one installation and one worker per drive. Large live transfers after the
MongoDB migration, Premium limits and additional deployment platforms still
need validation. See the linked test record and known limits before relying
on this beta.

[Documentation](docs/README.md) · [Known limits and roadmap](docs/roadmap.md) ·
[Tested versus unverified](docs/development.md#ci-and-release-evidence)

[MIT License](LICENSE) · [Project updates](https://t.me/TechZBots) ·
[Support](https://t.me/TechZBots_Support)
