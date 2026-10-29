# Self-host TGDrive on your computer or server

[Docs home](README.md) · [Setup](setup.md) · [Using TGDrive](usage.md)

**Every TGDrive feature is free and open source** under the [MIT License](../LICENSE).
Run it on your computer or a server you control. You maintain the installation
and choose any infrastructure providers you need.

Start with the [comparison](#local-computer-or-server), then follow [Setup](setup.md).
For self-hosting or setup questions, ask in the
[support group](https://t.me/TechZBots_Support). Keep credentials and private links
out of public messages.

## Local computer or server

The default local address, `127.0.0.1:8000`, works on the computer running
TGDrive. A public HTTPS server lets you use it while that computer is off.
Both need internet access to Telegram and MongoDB.

| Feature or workflow | Your computer, with default local access | Server with a public HTTPS address |
| --- | --- | --- |
| Access on the go | This computer by default; remote access needs extra setup | Phone, tablet or computer through an internet-connected browser |
| Multiple people using the same drive | Possible if they can reach your installation | Simultaneous access within server capacity |
| Public file and folder links | Other people need a reachable address; localhost links do not work on their devices | Recipients can use the public address while the server is available |
| Accepted uploads, URL imports, manual preloads, bot uploads and backups | Continue while your computer and TGDrive remain running | Continue while the server and TGDrive remain running |
| Work while your laptop is asleep or off | Stops when the computer hosting TGDrive stops | Continues independently of your laptop |
| Resources and maintenance | Your computer, connection and maintenance | Your chosen server, connection and maintenance |

For device uploads, keep the browser open until **You can close this tab** and
keep originals until **Completed**. Manual whole-file preloads can continue
without viewers; playback read-ahead follows active viewers.
[Transfer status meanings](usage.md#can-i-close-the-tab).

Multiple people can sign in with the same owner password. They share full access
to files and settings; TGDrive does not provide separate personal accounts or
per-person permissions. Use read-only share links for selected files or folders.
[Device access and sessions](usage.md#use-several-devices) · [Sharing](usage.md#share-and-revoke)

To make a computer-hosted installation reachable remotely, follow
[HTTPS deployment setup](setup.md#hosting-beyond-your-machine) and keep it awake.

## What you prepare

Create these in accounts you control:

| Prepare | What is needed |
| --- | --- |
| MongoDB Atlas | A cluster, dedicated database, database user with read/write access and connection URI. Allow the TGDrive server's IP in Network Access. [MongoDB steps](setup.md#1-prepare-mongodb) |
| Telegram application | Your API ID and API hash from Telegram's application page. [Telegram steps](setup.md#2-prepare-telegram) |
| Private Telegram channel | A dedicated private broadcast channel, its numeric ID, message auto-delete off, and no unrelated message management |
| Main bot | A bot you create through BotFather and its token; add it as a channel administrator with **Post Messages** and **Delete Messages**. It initially provides storage and the private-chat upload interface. |
| Allowed Telegram users | Your numeric Telegram user ID, plus any other users allowed to send files to your main bot. [Find your ID](setup.md#find-your-telegram-user-id) |
| Owner sign-in | A private TGDrive web password, kept separately from the public demo password |

Extra storage bots, Premium and a separate backup channel are optional.
Use TGDrive's normal authorization flow if you choose Premium.
[Bot uploads](usage.md#optional-telegram-bot-interface) ·
[Additional bots and Premium](setup.md#additional-bots-and-premium-accounts)

Never post credentials in an issue, screenshot or repository. Credential
changes can interrupt the drive; follow [connection maintenance](operations.md#connection-changes).

Keep your server access, TGDrive login, MongoDB access and private
deployment configuration. [Save a confirmed recovery file and its separate key](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server)
outside the server.

## Capacity and practical limits

Before choosing a larger server, use the
[load and capacity guide](performance-tuning.md) to match CPU, RAM, disk, network
or Telegram clients to the bottleneck shown by your workload.

| Resource | What it affects |
| --- | --- |
| CPU and RAM | How much preview, parsing and concurrent request work can fit; memory caching and background work share these resources |
| Server disk | Unfinished uploads, caches, application files and system overhead; it is not your total Telegram library size |
| Network and bandwidth allowance | Transfer throughput and the amount of traffic your connection can carry |
| Telegram and MongoDB access | Provider availability, account cooldowns, database capacity and connection health |
| Device and viewer connections | Your upload connection and each recipient's download connection remain part of the path |

New uploads default to a **16 GiB file limit and 16 GiB temporary upload budget**.
Larger files need both settings raised and enough free server disk, even though
TGDrive splits them in Telegram. See [upload storage settings](configuration.md#upload-size-and-available-disk-space)
and [exact file/catalog limits](reference.md#file-capacity-staging-and-work-limits).

Larger servers help when server resources are the bottleneck. They do not
guarantee a speed or active-user count. See [performance factors](performance.md#follow-the-data-path).

## Keep control of your drive

Keep Telegram access, MongoDB, private configuration and the recovery file/key
under your control. [Where data is saved](operations.md#what-is-saved-where).

Managing the server requires administrative access to its deployment credentials
and data. TGDrive does not separately encrypt Telegram files, so the operator is
a trusted party. Encryption of the recovery file does not encrypt your originals.
See [privacy essentials](new-user-guide.md) and [recovery limits](backup-recovery.md).

Before moving or shutting down a host, preserve your database and configuration.
Completed Telegram files can reopen on another host with the matching catalog and channel access;
unfinished uploads still need staged bytes or originals. Sharing and background
work stop until TGDrive is reachable again.
[Move or reopen your drive](operations.md#move-to-another-host).

## Help improve TGDrive

Contributions, feedback, bug reports and feature requests are welcome from
everyone.
[Open a GitHub issue](https://github.com/TechShreyash/TGDrive/issues) or follow the
[development guide](development.md) and [contributor guidance](../AGENTS.md).
Keep credentials and private links out of public reports.
