# TGDrive bugs and requests

See the [bug plan](bug-plan.md) for progress, work order, and verification.

## Restarts and crashes

- **Show interrupted jobs as failed.** If TGDrive shuts down or crashes while
  a job is running, show it as **Failed** the next time TGDrive starts, with
  a short reason. The job should only run again if the user starts it. (BUG-16)

## Bot uploads and backups

- **Telegram timeout during a bot import.** Mark the import as failed and show
  “Telegram timed out. Try again.” (BUG-01)
- **Make bot uploads easier to use and more reliable.** (BUG-02)
  - Clarify bot messages and settings, make the encryption toggle easy to find,
    and reconnect automatically.
  - Reserve temporary disk space safely and process files in a batch one at a time.
  - Verify that pooled shared-channel downloads clean up their temporary copies
    and leave enough capacity for browsing and playback.
- **Backups seem to run too often.** Find out why and explain the “backup discovery
  unverified” warning. Clarify what’s backed up and how to recover it. (BUG-04)

## Transfers, storage, and cache

- **Transfers can feel slow.** Load the page faster, keep results visible when
  returning to it, and keep browsing responsive during uploads. Make separate
  upload and download limits easy to find on Performance. (BUG-07)
- **Make storage and memory easier to manage.** Report storage and RAM accurately.
  Let owners choose how much cache to keep and for how long. Adjust streaming
  memory as needed, let competing streams make progress, and avoid caching more
  video than necessary. (BUG-08)

## Playback and previews

- **Video sometimes buffers.** Find out what causes the reported stalls. (BUG-03)
  - Live observation on 2026-11-21 in Chrome with the enhanced MKV player
    confirmed one buffering event around 04:30 of a 48:09 video. The player
    showed “Waiting for video data…” and later resumed. The stall was already
    in progress at the first check, so its total duration is unknown. Later
    fullscreen samples showed advancing frames with no visible loading
    indicator. The samples establish a minimum count for this viewing session.
  - The owner also reports that audio keeps playing while the picture buffers
    in the same Chrome preview, then jumps ahead or resumes out of sync.
    Expected behavior is to hold sound and playback position when the picture
    cannot advance, then resume synchronized audio and video from that position.
- **A paused video can fail to show a frame.** Show the right frame
  when opening a video paused or jumping to another time while paused. (BUG-06)
- **Make previews faster and more useful.** (BUG-09)
  - Open videos and PDFs faster and make it quicker to jump to another time or page.
  - Remember video and PDF progress. Resume videos automatically and show watch
    progress on file cards.
  - Make chapter and hover previews work, play videos with unsupported codecs
    through a compatible playback option, and show what’s inside ZIP files.
- **Preview Office files.** Let users read common documents and spreadsheets
  directly in the drive. (BUG-15)

## Moving files and password protection

- **Move can open in the wrong folder.** Start in the selected item’s parent
  folder and show its real folder path, including from Recent or Starred. (BUG-05)
- **Archives.** Create and extract password-protected archives. (BUG-12)
- **PDFs.** Add, change, or remove a PDF password. (BUG-13)
- **Shared links.** Let the owner require a password to open a link. (BUG-14)
