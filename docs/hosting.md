# Run TGDrive locally or on a server

[Docs home](README.md) · [Setup](setup.md) · [Using TGDrive](usage.md)

**Every TGDrive feature is free and open source** under the [MIT License](../LICENSE).
Run it on your computer, use your own server, or pay for optional hosting and
maintenance. Hosting buys resources and help; it does not unlock app features.

Start with the [comparison](#local-computer-or-server), or see
[managed hosting](#optional-managed-hosting) and [server-only plans](#server-plans-and-customization).

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
| Resources and maintenance | Your computer, connection and maintenance | Your chosen server; maintain it yourself or use optional management |

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

## Optional managed hosting

We offer **managed TGDrive hosting from ₹100/month (INR), including a server**.
This is the total starting monthly price.

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
counts**. App limits, available disk and the server's bandwidth allowance still
apply. Larger plans provide more capacity for simultaneous work.

[Contact TechShreyash on Telegram](https://t.me/TechShreyash) to arrange setup and
choose resources for your workload. Confirm CPU, RAM, disk, monthly bandwidth,
upgrade pricing and availability in your quote. The server-only prices below
are separate from the managed offer.

If you prefer to manage TGDrive yourself, you can also buy just a server from us.
You are equally free to use your own server or another provider.

## What you prepare

Create these in accounts you control. For managed setup, share only the required
deployment credentials through the private setup conversation.

| Prepare | What is needed |
| --- | --- |
| MongoDB Atlas | A cluster, dedicated database, database user with read/write access and connection URI. Allow the TGDrive server's IP in Network Access. [MongoDB steps](setup.md#1-prepare-mongodb) |
| Telegram application | Your API ID and API hash from Telegram's application page. [Telegram steps](setup.md#2-prepare-telegram) |
| Private Telegram channel | A dedicated private broadcast channel, its numeric ID, message auto-delete off, and no unrelated message management |
| Main bot | A bot you create through BotFather and its token; add it as a channel administrator with **Post Messages** and **Delete Messages**. It initially provides storage and the private-chat upload interface. |
| Allowed Telegram users | Your numeric Telegram user ID, plus any other users allowed to send files to your main bot. [Find your ID](setup.md#find-your-telegram-user-id) |
| Owner sign-in | A private TGDrive web password, kept separately from the public demo password |

Extra storage bots, Premium and a separate backup channel are optional.
Basic managed setup does not need your Telegram account's login code/password.
Use TGDrive's normal authorization flow if you choose Premium.
[Bot uploads](usage.md#optional-telegram-bot-interface) ·
[Additional bots and Premium](setup.md#additional-bots-and-premium-accounts)

We use the credentials to connect your channel/database and publish your HTTPS
address. Never post them in an issue, screenshot or repository. Credential
changes can interrupt the drive; follow [connection maintenance](operations.md#connection-changes).

At handoff, keep your server access, TGDrive login, MongoDB access and private
deployment configuration. [Save a confirmed recovery file and its separate key](backup-recovery.md#keep-the-mongodb-recovery-kit-outside-the-server)
outside the server.

## Server plans and customization

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

## Capacity and practical limits

Before choosing a larger server, use the
[load and capacity guide](performance-tuning.md) to match CPU, RAM, disk, network
or Telegram clients to the bottleneck shown by your workload.

| Resource | What it affects |
| --- | --- |
| CPU and RAM | How much preview, parsing and concurrent request work can fit; memory caching and background work share these resources |
| Server disk | Unfinished uploads, caches, application files and system overhead; it is not your total Telegram library size |
| Network and monthly bandwidth | Transfer throughput and the amount of traffic the server plan can carry |
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

Before hosting ends, preserve your database and configuration. Completed Telegram
files can reopen on another host with the matching catalog and channel access;
unfinished uploads still need staged bytes or originals. Sharing and background
work stop until TGDrive is reachable again.
[Move or reopen your drive](operations.md#when-a-hosting-plan-expires).

## Help improve TGDrive

Contributions, feedback, bug reports and feature requests are welcome from
everyone, including people running it entirely themselves. Hosting is optional.
[Open a GitHub issue](https://github.com/TechShreyash/TGDrive/issues) or follow the
[development guide](development.md) and [contributor guidance](../AGENTS.md).
Keep credentials and private links out of public reports.
