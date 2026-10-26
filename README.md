<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="website/public/static/brand/tgdrive-logo-dark.svg">
    <img src="website/public/static/brand/tgdrive-logo.svg" alt="TGDrive" width="400">
  </picture>
</h1>

<p align="center">
  <strong>Turn Telegram into your personal cloud drive.</strong>
</p>

TGDrive stores your files in a private Telegram channel and gives you a familiar
web drive to manage them. Upload large files or whole folders, stream videos,
read documents and share files or folders with a link—all from your browser.

Free and open source under the [MIT License](LICENSE). Run it on your computer or
a server; MongoDB is required for the file list, folders, settings and transfer
records.

**[Try the demo](#try-the-public-demo)** · **[Set up your drive](docs/setup.md)** · **[Explore screenshots and videos](docs/showcase.md)**

New here? Start with the [storage, privacy and backup essentials](docs/new-user-guide.md).

[![TGDrive desktop file browser with sample folders and files](docs/assets/drive-overview.png)](docs/showcase.md)

## Features

- **No fixed total storage limit.** TGDrive does not cap the total space your files use in Telegram.
- **Upload large files without manual splitting.** TGDrive handles the Telegram parts and presents them as one file for playback and downloading.
- **Play videos and audio in your browser.** Stream supported media directly from your drive.
- **View documents, photos, ebooks and comics.** Open images, PDFs and text, and read EPUB ebooks and CBZ/CBR comics in your browser.
- **Resumable file and folder uploads.** Upload whole folders with their structure preserved, and resume interrupted device uploads by reselecting the originals.
- **Import from URLs or Telegram.** Paste a direct file URL, or send and forward files to your configured Telegram bot, to add them to your drive.
- **Organize and find files.** Create folders, search by name, star favorites, switch between list and grid views, and restore items from Trash.
- **Share files and folders with a link.** Give others read-only access, choose an expiry and revoke access when needed.
- **Backups and recovery.** Save encrypted backups of your file list, folders and settings, and optionally copy completed files to a separate Telegram channel.

Individual uploads default to **16 GiB per file**. You can raise the file limit
and temporary upload budget when your server has enough free disk space.
[File-size and storage settings →](docs/configuration.md#upload-size-and-available-disk-space)

**Keep another copy of important files outside Telegram.** Account or channel
restrictions can still make stored files unavailable.

## Try the public demo

**[Open the live demo](https://tgdrive.nexgenbots.us/)** · **Password:** `password12345`

Try TGDrive in your browser without installing anything. Explore the protected
**Demo Files** collection, or upload disposable samples to **My Files** to try
organizing, previews and sharing. Configuration and Management are visible, with
changes locked.

**The demo is shared.** Other visitors can change or delete files outside
**Demo Files**. Use only disposable, non-private content.
[Demo guide →](docs/usage.md#try-the-public-demo)

## See it in action

| Watch and read | Your drive on mobile |
| :---: | :---: |
| <img src="docs/assets/media-preview.png" alt="Video open in TGDrive's player" width="520"> | <img src="docs/assets/mobile-drive.png" alt="TGDrive file browser in a phone-sized browser viewport" width="240"> |

[Explore the full screenshot gallery and video walkthroughs →](docs/showcase.md)

<details>
<summary>Watch the upload and sharing walkthrough</summary>

[![Watch the TGDrive upload and sharing walkthrough](docs/assets/drive-overview.png)](docs/assets/upload-and-share.mp4)

[Open the video](docs/assets/upload-and-share.mp4) · [All walkthroughs](docs/showcase.md)

Silent walkthroughs recorded at normal speed, with a visible cursor overlay,
click highlights and step captions. The mobile image uses an emulated viewport,
not a physical phone. See [capture notes](docs/assets/README.md).

</details>

<details>
<summary>Extra screenshots · uploads and transfers</summary>

![Expanded TGDrive upload panel showing device progress, speed and time remaining](docs/assets/upload-progress.png)

</details>

## Make it yours

Choose where TGDrive runs. All features are free in every setup; you provide
your own Telegram channel, bots and MongoDB database.

| Where to run it | Get started |
| --- | --- |
| Your computer | [Local setup](docs/setup.md) for use while your computer is on |
| Your own server | [Server deployment](docs/setup.md#hosting-beyond-your-machine) for a drive you can reach while your laptop is off |
| A server we manage for you | [Optional managed hosting](docs/hosting.md#optional-managed-hosting) for help with setup and maintenance |

### Why host on a server?

A server lets you use your drive from other devices, share links with others and
keep accepted transfers running while your laptop is off. TGDrive, MongoDB and
Telegram must remain available, and recipients must be able to reach the app.
[Compare running on your computer and a server →](docs/hosting.md#local-computer-or-server)

For device uploads, keep the tab open until **You can close this tab** appears,
and keep the originals until **Completed**.
[Upload status meanings →](docs/usage.md#can-i-close-the-tab)

### Prefer help with hosting?

We offer optional **managed TGDrive hosting from ₹100/month, including a
server**. We handle deployment and ongoing maintenance, and provide a personal
HTTPS subdomain. You keep full access to the server and TGDrive, using your own
Telegram channel, bots and MongoDB account.

Higher plans provide more server capacity, with the same app features. There
are no extra hosting-plan limits on file size or upload/download counts.
Configured upload limits, disk space and bandwidth still apply.

You can also buy just a server from us and manage TGDrive yourself.
[Hosting details, discounted server plans and ownership →](docs/hosting.md) ·
[Contact TechShreyash](https://t.me/TechShreyash)

## Find your guide

| What you want to do | Where to go |
| --- | --- |
| Understand storage, privacy and what to protect | [New-user guide and FAQ](docs/new-user-guide.md) |
| Upload, organize, preview and share | [User guide](docs/usage.md) |
| Back up your drive or recover after a failure | [Backup & recovery](docs/backup-recovery.md) |
| Change settings, maintain the app or fix a problem | [Configuration](docs/configuration.md) · [Operations](docs/operations.md) |
| Explore all guides, technical details and contributor resources | [Documentation home](docs/README.md) |

## Contribute

Contributions, feedback, bug reports and feature requests are always welcome.
[Open an issue](https://github.com/TechShreyash/TGDrive/issues) or start with the
[development guide](docs/development.md) and [contributor guidance](AGENTS.md).
Include your release and reproduction steps in bug reports; remove credentials
and private links.

[MIT License](LICENSE)
