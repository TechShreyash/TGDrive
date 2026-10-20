# TGDrive documentation

**New here? Start with [Setup](setup.md), then [Using TGDrive](usage.md).**

TGDrive is your own file manager. You run the app; Telegram holds your files,
and MongoDB remembers their names, folders and sharing settings.

## What do you want to do?

| I want to… | Read |
| --- | --- |
| Install TGDrive for the first time | [Setup](setup.md) |
| Upload, organize, preview or share files | [Using TGDrive](usage.md) |
| Restart, upgrade or fix a problem | [Operations](operations.md) |
| Save recovery keys, understand encrypted backups or restore after data loss | [Backup & recovery](backup-recovery.md) |
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
  while appearing as one file.
- **Keep your database access and recovery key.** Reinstalling the app can reopen
  the same MongoDB drive. Telegram database backups offer a separate recovery route.
- **Wait for “Completed” before removing local files.** Unfinished uploads can
  still need their originals and temporary files on your machine.

## Current release status

This is an **unreleased development version**. Focused MongoDB and live Chrome
checks have passed; final release testing and packaging remain pending.
See [tested versus unverified](development.md#ci-and-release-evidence).

These pages are the maintained documentation. Behavior changes should
update the relevant page, rather than add another overlapping guide.
