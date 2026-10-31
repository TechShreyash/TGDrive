# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary: self-hosters.** One person or a household running their own TGDrive on a laptop or server, using a private Telegram channel as file storage. They upload, organize, stream, read and share their own files from a desktop or phone browser.
- **Secondary: share viewers.** People who open a read-only file or folder link, or an embedded viewer on someone's website. They have no account, may never have heard of TGDrive and only need to view or download.

## Product Purpose

TGDrive makes a private Telegram channel work like a cloud drive: a file browser, previews, streaming, sharing and background transfers on top of storage the user already controls. Success means users trust it with their files, find things quickly and can recover the drive after a failure.

## Positioning

Storage lives in the user's own Telegram channel, files are encrypted before upload with a key only the user holds, and there is no fixed total-storage quota. It is free, open source and self-hosted, with every feature included.

## Operating Context

- Used on desktop and phone browsers; the phone layout matters (see `docs/assets/mobile-drive.jpg`).
- Long-running work (large split uploads, torrent imports, ZIP creation and extraction, preloads) continues on the server and is followed in Transfers.
- Users set up Telegram, a bot and MongoDB themselves, so setup, connection and recovery screens are read by people who may not know Telegram internals.

## Capabilities and Constraints

- Views: My Files, Starred, Trash, Shares, Activity, Connections, Storage, Settings, Preloads, Performance; plus public share pages, the embed view and Login.
- Previews: video and audio streaming with Continue, photos, PDF, text, EPUB, CBZ/CBR.
- Light, dark and system appearance; system is the default.
- Telegram limits file size and availability; TGDrive does not guarantee permanent storage. Do not promise unlimited or permanent storage in UI copy.

## Brand Commitments

- Name: **TGDrive**. Tagline: "Your Telegram files, organized like a cloud drive."
- Voice: plain, practical, honest about limits (see `README.md`).

## Evidence on Hand

- Screenshots and tour video in `docs/assets/` (captured 28 October 2026), gallery in `docs/showcase.md`.
- Public demo: <https://tgdrive.nexgenbots.us/>.
- No testimonials, user counts or benchmarks exist; do not invent them.

## Product Principles

1. Never lose or expose a user's files; safety and recovery messaging beat visual polish.
2. Show what is happening with long transfers honestly, including failures and what to do next.
3. Share pages must make sense to someone who has never seen TGDrive.
4. Everything works on a phone, not only on desktop.
