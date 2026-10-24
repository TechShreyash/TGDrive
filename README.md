<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="website/public/static/brand/tgdrive-logo-dark.svg">
    <img src="website/public/static/brand/tgdrive-logo.svg" alt="TGDrive" width="400">
  </picture>
</h1>

<p align="center">
  <strong>Your personal cloud. Unlimited storage. Big files welcome.</strong>
</p>

Keep your media and files in one place. Upload whole folders,
stream and read in your browser, and share a file or an entire folder with a link.

TGDrive is free, open-source software you can use, modify and deploy under the
[MIT License](LICENSE). Run it on your laptop or server; it stores your files in
a private channel on Telegram.

[Try the public demo](#try-the-public-demo) · [Set up your drive](docs/setup.md) ·
[See it in action](#see-it-in-action) · [Visual walkthroughs](docs/showcase.md) · [User guide](docs/usage.md) · [Documentation](docs/README.md)

![TGDrive desktop file browser with sample folders and files](docs/assets/drive-overview.png)

## Features

- **No total storage quota.** Store files in your own Telegram channel without a TGDrive quota on the total amount of data stored.
- **Large file uploads.** TGDrive automatically splits large uploads for Telegram and presents each as one file for playback and downloading.
- **Play videos and audio in your browser.** Stream supported media directly from your drive.
- **View documents, photos, ebooks and comics.** Open images, PDFs and text, and read EPUB ebooks and CBZ/CBR comics in your browser.
- **Resumable file and folder uploads.** Upload whole folders with their structure preserved, and resume interrupted device uploads by reselecting the originals.
- **Import from URLs or Telegram.** Paste a direct file URL, or send and forward files to your configured Telegram bot, to add them to your drive.
- **Organize and find files.** Create folders, search by name, star favorites, switch between list and grid views, and restore items from Trash.
- **Share files and folders with a link.** Give others read-only access, choose an expiry and revoke access when needed.
- **Backups and recovery.** Save encrypted backups of your file list, folders and settings, and optionally keep a second copy of completed files in an independent Telegram channel.

Individual uploads default to **16 GiB per file**. You can raise the file limit
and temporary upload budget when your server has enough free disk space.
[File-size and storage settings →](docs/configuration.md#upload-size-and-available-disk-space)

## Try the public demo

**[Open the live demo](https://tgdrive.nexgenbots.us/)** · **Password:** `password12345`

No installation is needed. Browse, upload sample files, preview media, organize
folders and try sharing.
Configuration and Management stay visible, with changes locked.
Browse the protected **Demo Files** collection, then use **My Files** for your own
test uploads and changes.

It is a shared test drive: visitors can change or delete files outside Demo Files. Use only
disposable, non-private content. [What to try →](docs/usage.md#try-the-public-demo)

## See it in action

| Watch and read | Your drive on mobile |
| :---: | :---: |
| <img src="docs/assets/media-preview.png" alt="Video open in TGDrive's player" width="520"> | <img src="docs/assets/mobile-drive.png" alt="TGDrive file browser in a phone-sized browser viewport" width="240"> |

[Browse every part of TGDrive →](docs/showcase.md) File organization, transfers,
sharing, previews, readers, preloads, monitoring and settings, with practical steps.

<details>
<summary>Watch · upload and share</summary>

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

Run TGDrive on your own computer or a server. Both give you the same free app
and all its features.

### Why host on a server?

| Feature | Localhost on your computer | Server with public HTTPS |
| --- | --- | --- |
| Uploads, previews, readers and file management | All features included | All features included |
| Access on the go | This computer by default; remote access needs setup | Phone, tablet or computer through an internet-connected browser |
| Multiple people and shared links | Need an address reachable beyond localhost | Simultaneous access within server capacity |
| Accepted transfers after closing the tab | Continue while your computer and TGDrive run | Continue while the server and TGDrive run |
| Bot uploads, URL imports, preloads and backups | While your computer and TGDrive run | While the server and TGDrive run, independently of your laptop |
| Available while your laptop sleeps or is off | No, if it hosts TGDrive | Yes, while the server is available |
| Deployment and maintenance | You handle it | Handle it yourself or choose optional management |

For help with setup and maintenance, we offer optional **managed TGDrive hosting
from ₹100/month, including a server**, deployment, ongoing management and a
personal HTTPS subdomain. You get full server and TGDrive access, using your own
Telegram channel, bots and MongoDB account. TGDrive itself remains free.

There are no additional hosting-plan caps on file size or upload/download counts.
Higher plans buy capacity for more simultaneous requests and users, with more
headroom for speed. Configured upload limits, disk space and bandwidth still apply.

You can also buy just a server from us and manage TGDrive yourself.
[Hosting details, discounted server plans and ownership →](docs/hosting.md) ·
[Contact TechShreyash](https://t.me/TechShreyash)

<details>
<summary>Storage, backups and limits</summary>

TGDrive is a single-owner, internet-connected drive. The server needs disk for
unfinished uploads and caching. Keep its Telegram channel private and app-managed,
preserve persistent volumes, and save recovery keys separately.

Keep originals until uploads say **Completed**. Once **You can close this tab**
appears, server processing can continue while TGDrive stays running. Shared
links also need a reachable running app.

Unlimited means TGDrive does not meter total stored bytes. Uploads default to
16 GiB and are configurable. Its catalog supports up to 250,000 files/folders;
per-file limits and available staging space still apply.
Telegram access and availability are still required. MongoDB holds the catalog
and settings.

Database backups cannot recover deleted Telegram files. Keep independent copies
of important originals. See [Backup & recovery](docs/backup-recovery.md).

</details>

<details>
<summary>Configuration and operations</summary>

[Configuration](docs/configuration.md) · [Operations](docs/operations.md) ·
[Architecture](docs/architecture.md) · [Development](docs/development.md) ·
[Roadmap](docs/roadmap.md)

Maintainers can use [release verification](docs/release.md).

For screenshots, recordings and replacement instructions, see [Screenshots and demos](docs/assets/README.md).

</details>

## Contribute

Contributions, feedback, bug reports and feature requests are always welcome.
[Open an issue](https://github.com/TechShreyash/TGDrive/issues) or start with the
[development guide](docs/development.md) and [contributor guidance](AGENTS.md).
Include your release and reproduction steps in bug reports; remove credentials
and private links.

[MIT License](LICENSE)
