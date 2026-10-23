# TGDrive documentation

**New here? Start with [Setup](setup.md), then [Using TGDrive](usage.md).**

TGDrive is your own file manager. You run the app; Telegram holds your files,
and MongoDB remembers their names, folders and sharing settings.

## What do you want to do?

| I want to… | Read |
| --- | --- |
| Run TGDrive on my laptop only when needed | [Private Docker setup](setup.md) |
| Host TGDrive for remote access and sharing | [Recommended: Coolify](setup.md#coolify), or [server Compose](setup.md#docker-compose-on-a-server) |
| Offer a public demo with settings locked | [Public demo mode](configuration.md#public-demo-mode) |
| Upload, organize, preview or share files | [Using TGDrive](usage.md) |
| Understand adaptive uploads, device/server stages or when a tab can close | [Upload speed and queue](usage.md#upload-speed-and-queue), [tab closure](usage.md#can-i-close-the-tab) |
| Read PDFs, ebooks or comics without downloading every byte first | [Reading books and comics](reading.md) |
| Understand generated image, video and PDF covers | [Thumbnail generation](thumbnails.md) |
| Restart, upgrade or fix a problem | [Operations](operations.md) |
| Configure an independent backup channel, save recovery keys or restore after data loss | [Backup & recovery](backup-recovery.md) |
| Understand settings, restore safe defaults or tune a larger server | [Configuration & tuning](configuration.md) |
| Improve streaming, control cache budgets or prepare shared downloads | [Performance and caching](performance.md) |
| Review fresh-browser playback, protocol and cached-seek checks | [Streaming validation](streaming-validation-2026-10-18.md) |
| Check what works and what still needs work | [Roadmap](roadmap.md) |

## For contributors

| I need… | Read |
| --- | --- |
| A map of how the app works | [Architecture](architecture.md) |
| An environment variable or API contract | [Reference](reference.md) |
| Build commands, tests or release instructions | [Development](development.md) |

## Three things to know

- **Premium is optional.** Large files can be split across Telegram messages
  while appearing as one file. Every account uses the same default and maximum
  2000 MiB document size, which you can reduce. Premium is preferred for transfer speed.
- **Keep your database access and recovery key.** Reinstalling the app can reopen
  the same MongoDB drive. Telegram database backups offer a separate recovery route.
- **Close the tab after server acceptance; keep originals until “Completed”.**
  The upload panel confirms when all selected files have reached the server.
  Telegram processing can then continue while TGDrive itself stays running.
  Unfinished uploads can still need originals or staged bytes during recovery.

Local access uses this computer's loopback address. Other people cannot open its
share links, and nothing runs while the app is stopped. Keep the private Telegram
channel app-managed: no manual message edits/deletions or automatic cleanup.

## Current release status

This is an **unreleased development version**. Focused MongoDB and live Chrome
checks have passed; final release testing and packaging remain pending.
See [tested versus unverified](development.md#ci-and-release-evidence).

These pages are the maintained documentation. Behavior changes should
update the relevant page, rather than add another overlapping guide. Contributor
and agent changes follow the [documentation maintenance rules](../AGENTS.md).
