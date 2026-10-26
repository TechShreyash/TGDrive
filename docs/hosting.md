# Run TGDrive locally or on a server

[Docs home](README.md) · [Setup](setup.md) · [Using TGDrive](usage.md)

Read the [new-user guide and FAQ](new-user-guide.md) before storing important
files. Its privacy, channel and backup responsibilities also apply to managed hosting.

**TGDrive is completely free and open source.** Anyone can use, modify and deploy
it under the [MIT License](../LICENSE). All features are available whether you
run it on your own computer, choose your own server or ask us to manage hosting.

[Compare local and server use](#local-computer-or-server) · [Optional managed hosting](#optional-managed-hosting) · [Prepare your accounts](#what-you-prepare) · [Capacity and limits](#capacity-and-practical-limits)

## Local computer or server

Localhost is the default installation at `127.0.0.1:8000`, accessible from the
computer running TGDrive. It still needs the internet to reach Telegram and
MongoDB. A hosted server provides a reachable address while your personal
computer is off.

| Feature or workflow | Localhost on your computer | Server with a public HTTPS address |
| --- | --- | --- |
| All TGDrive features | Included, free | Included, free |
| Files, folders, search, stars and Trash | Available while TGDrive runs | Available while TGDrive runs |
| Uploads, URL imports, previews and readers | Available while TGDrive runs | Available while TGDrive runs |
| Access on the go | This computer by default; remote access needs extra setup | Phone, tablet or computer through an internet-connected browser |
| Multiple people using the same drive | Possible if they can reach your installation | Simultaneous access within server capacity |
| Public file and folder links | Other people need a reachable address; localhost links do not work on their devices | Recipients can use the public address while the server is available |
| Accepted uploads, URL imports and manual preloads after closing the tab | Continue while your computer and TGDrive remain running | Continue while the server and TGDrive remain running |
| Bot uploads, scheduled backups and other background work | Continue while your computer and TGDrive remain running | Continue while the server and TGDrive remain running |
| Work while your laptop is asleep or off | Stops when the computer hosting TGDrive stops | Continues independently of your laptop |
| Resources and maintenance | Your computer, connection and maintenance | Your chosen server; maintain it yourself or use optional management |

Device uploads need the browser until **You can close this tab** appears. Keep
originals until **Completed**. Automatic playback read-ahead follows active
viewers; a manual whole-file preload can continue without them. The app,
MongoDB, Telegram and network must remain available. See
[transfer status meanings](usage.md#can-i-close-the-tab).

Multiple people can sign in with the same owner password. They share full access
to files and settings; TGDrive does not provide separate personal accounts or
per-person permissions. Use read-only share links for selected files or folders.
[Device access and sessions](usage.md#use-several-devices) · [Sharing](usage.md#share-and-revoke)

You can also configure remote access to a computer-hosted instance, but the
computer must stay awake and reachable. See [server deployment](setup.md#hosting-beyond-your-machine)
for HTTPS and deployment options. Hosting is a way to keep the same app available,
not a requirement to unlock features.

## Optional managed hosting

If you want to use TGDrive without deploying it, maintaining the server or fixing
hosting problems yourself, we offer **managed TGDrive hosting from ₹100/month
(INR), including a server**. This is the total starting monthly price, not a
management fee added to a separate server bill.

| Included | What you receive |
| --- | --- |
| Server and TGDrive access | Full access to your server and TGDrive, with every TGDrive feature available |
| Deployment and ongoing management | We configure and deploy TGDrive, handle server/deployment maintenance and updates, and help troubleshoot hosting problems |
| Personal public HTTPS subdomain | Your own drive address for access from a browser on the go and for sharing links |
| Your accounts and storage | Your Telegram account, private channel and bots; MongoDB in your own account |
| Capacity choices | More server resources when you need to handle more simultaneous requests, viewers, uploads or downloads |

Your HTTPS address is public, but your owner drive remains password-protected.
You choose which files or folders to expose through read-only share links.

There are **no additional hosting-plan caps on file size or upload/download
counts**. Every plan includes the same app features. Higher payments buy more
capacity for work happening at the same time, not feature unlocks. Configured
upload limits, real disk space and the server's bandwidth allowance still apply;
see [practical limits](#capacity-and-practical-limits).

[Contact TechShreyash on Telegram](https://t.me/TechShreyash) to arrange setup and
choose resources for your workload. Confirm the CPU, RAM, disk and monthly
bandwidth included in your managed quote, any upgrade price and current
availability. The ₹100 starting offer includes a server; the server-only table
below is a separate set of prices, not an extra charge added to that offer.

If you prefer to manage TGDrive yourself, you can also buy just a server from us.
You are equally free to use your own server or another provider.

## What you prepare

Your drive belongs to you, so some account setup stays on your side. Create the
following in accounts you control, then provide the required deployment
credentials privately to us for managed setup.

| Prepare | What is needed |
| --- | --- |
| MongoDB Atlas | A cluster, dedicated database, database user with read/write access and connection URI. Allow the TGDrive server's IP in Network Access. [MongoDB steps](setup.md#1-prepare-mongodb) |
| Telegram application | Your API ID and API hash from Telegram's application page. [Telegram steps](setup.md#2-prepare-telegram) |
| Private Telegram channel | A dedicated private broadcast channel, its numeric ID, message auto-delete off, and no unrelated message management |
| Storage bot | A bot you create through BotFather and its token; add each storage bot as a channel administrator with **Post Messages** and **Delete Messages** |
| Owner sign-in | A private TGDrive web password, kept separately from the public demo password |

For optional Telegram chat uploads, also configure the main bot and administrator
Telegram user IDs. Premium account sign-in and a separate backup channel are
optional, not prerequisites for ordinary storage or large split files. Use the
normal authorization flow if you choose Premium; basic managed deployment uses
the credentials above, not your main Telegram account's login code/password.
[Bot uploads](usage.md#optional-telegram-bot-interface) ·
[Additional bots and Premium](setup.md#additional-bots-and-premium-accounts)

We use the supplied credentials to configure the server, connect your channel and
database, and publish your personal HTTPS address. Share them only through the
private setup conversation, never in a public issue, screenshot or repository.
Changes to database access or revoked bot credentials can interrupt the drive;
coordinate replacements through the [connection maintenance guide](usage.md#settings-and-connections).

At handoff, keep your server access, TGDrive login, MongoDB access and deployment
configuration. Download **Storage & Backups → Save recovery key**, keep it off
the server, and confirm a database backup before relying on recovery. Follow the
[recovery kit guide](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server).

## Server plans and customization

<details>
<summary>Optional server-only plans and temporary discounted prices</summary>

These are **temporary launch-discount prices** for a server you manage yourself,
in INR per month. Contact us for current availability and pricing.

| vCPU | RAM | Server storage | Monthly bandwidth | Price/month |
| --- | --- | --- | --- | --- |
| 1 | 1 GB | 32 GB | 1 TB | ₹99 |
| 1 | 2 GB | 64 GB | 2 TB | ₹149 |
| 2 | 4 GB | 100 GB | 5 TB | ₹249 |
| 4 | 8 GB | 180 GB | 6 TB | ₹399 |
| 8 | 16 GB | 350 GB | 8 TB | ₹699 |

Some resource customization, such as additional RAM or storage, can be arranged.
[Contact TechShreyash](https://t.me/TechShreyash) for availability and a quote.
Ask how bandwidth is counted and what happens if its allowance is reached;
unlimited upload/download counts do not mean unlimited network traffic.

Managed capacity upgrades are quoted separately from this server-only table.
For self-managed deployment, follow [Setup](setup.md); the
[self-hosted Coolify option](setup.md#coolify) can help manage deployment.

</details>

## Capacity and practical limits

The same limits and scheduling rules apply on localhost and servers. The hosting
offer adds no paid file-size or transfer-count restriction; it does not remove
the app's technical bounds or the infrastructure's finite resources.

| Resource | What it affects |
| --- | --- |
| CPU and RAM | How much preview, parsing and concurrent request work can fit; memory caching and background work share these resources |
| Server disk | Unfinished uploads, caches, application files and system overhead; it is not your total Telegram library size |
| Network and monthly bandwidth | Transfer throughput and the amount of traffic the server plan can carry |
| Telegram and MongoDB access | Provider availability, account cooldowns, database capacity and connection health |
| Device and viewer connections | Your upload connection and each recipient's download connection remain part of the path |

New uploads default to a **16 GiB file limit and 16 GiB temporary upload budget**.
Larger files require raising both settings and having enough actual free server
disk; the smallest server plans cannot stage arbitrarily large files. TGDrive
splits large files into Telegram documents and presents them as one logical file.
Use the [configuration guide](configuration.md#upload-size-and-available-disk-space)
for current adjustable bounds and the [reference](reference.md#file-capacity-staging-and-work-limits)
for multipart and catalog limits.

Higher server capacity can support more simultaneous users and requests when
server resources are the bottleneck. It does not create separate user accounts,
guarantee a particular number of active users or set a guaranteed transfer speed.
See [performance factors](performance.md#follow-the-data-path) before choosing or
upgrading a plan.

## Keep control of your drive

File bytes live in your private Telegram channel. MongoDB stores the catalog,
folder structure, part mappings, settings and shares. Local server disk holds
unfinished uploads, recovery state and disposable cache. Keep control of all
three parts, plus the matching recovery keys.

Managing the server requires administrative access to its deployment credentials
and data. Keeping the accounts in your name does not make their contents
inaccessible to the operator: Telegram payloads are not separately encrypted by
TGDrive. Encrypted database backups protect their backup archives. See the
[backup and security limits](backup-recovery.md) for the full model.

If hosting ends, completed Telegram files remain in your channel. With the
matching catalog, Telegram access and recovery kit, you can reopen the same drive
on your laptop or another server without re-uploading completed files. Preserve
your database and configuration before server access ends. Public links and
background work need a reachable running app, and unfinished uploads still need
their staged bytes or original device files.
[Move or reopen your drive](operations.md#when-a-hosting-plan-expires).

## Help improve TGDrive

Contributions, feedback, bug reports and feature requests are welcome from
everyone, including people running it entirely themselves. Hosting is optional.
[Open a GitHub issue](https://github.com/TechShreyash/TGDrive/issues) or follow the
[development guide](development.md) and [contributor guidance](../AGENTS.md).
Keep credentials and private links out of public reports.
