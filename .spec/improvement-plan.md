# TGDrive improvement plan for dev

See the [live frontend audit and ordered implementation plan](frontend-audit.md).

Reassess each of the 14 bug IDs and all 80 frontend feedback IDs in the current
[backlog](bugs.md), [bug tracker](bug-plan.md), and
[frontend tracker](frontend-polish.md). Prior completion notes document earlier
evidence. Reproduce the behavior on the current checkout, then make and test
subsequent improvements against that baseline. The owner approved playback
diagnostics and share-open counts. BUG-10 and BUG-11 are absent from the current
backlog.

Follow the backlog order and keep each change independently verifiable. The
first execution batch is BUG-16 followed by BUG-01. Record results in the bug
tracker after each item.

## Ordered bug work

| Order | ID | Improvement | Acceptance |
| --- | --- | --- | --- |
| 1 | BUG-16 | Show what stopped, whether the upload can use its saved data, and what the owner must do next. Distinguish retry from uploading the original again. | Stop or crash during a staged upload. Reopen the journal twice without sending again. Retry explicitly and verify one published file, the same send identity, and an unchanged completed transfer. Repeat after losing the staged data. Capture desktop and phone recovery. |
| 2 | BUG-01 | Align Telegram timeout wording and actions across the bot and Transfers. Offer retry when safe. For an uncertain copy, explain private-channel review and what happens to the queue. | Inject a definite timeout and a lost acknowledgement. Restart with each state. Check durable failure, explicit retry, owner review, and a second activation that leaves the queue and send counts unchanged. Capture desktop and phone. |
| 3 | BUG-02 | Clarify bot destination, encryption, ordering, temporary-space waits, and completion. Keep read capacity and cleanup predictable through reconnect and abandonment. | Queue two encrypted imports and play a third file while the first is held. Check order, reservation release, temporary copy removal, and reader use. Repeat with a dedicated Telegram intake channel and two eligible readers when available. |
| 4 | BUG-04 | Explain why the last backup ran, when another can run, and which copy has been verified. Distinguish an unverified pin from missing restore material. | Make a manual backup before the automatic deadline, restart, and inspect publication count and trigger. Restore into a fresh database. Attribute the reported burst only if matching events are available. |
| 5 | BUG-07 | Retain history and filters, prioritize interactive work during upload, and show the slow stage and next action. Keep independently configurable speeds reachable from Performance. | Compare five runs with populated history and a held refresh. Search, page, leave, return, and open a file under load. Save and reload separate upload and download limits. |
| 6 | BUG-08 | Populate the demo tuning report with representative effective limits and reasons. Clarify disk reserve, upload staging, cache, stream memory, and drive inventory. | Compare reported limits in a constrained Docker instance. Check competing streams, preload, and eviction. Verify populated, missing, stale, and recovered tuning reports. |
| 7 | BUG-03 | Add Copy playback diagnostics. Trace held delivery, delayed decoding, sparse source frames, and audio restart. Fix the earliest observed clock divergence. | Hold two MKV byte-range responses at 1× and 1.5×. Measure picture, audible output, and playhead through each refill. Exercise user pause during recovery. When accessible, revisit the hosted sample around 04:30 and record the deployed build. |
| 8 | BUG-06 | Preserve pause intent across open, resume, seek, and fullscreen. Keep late frames from an earlier seek away from the current picture. | Open a marked clip paused. Seek quickly to three positions and check final pixels, silence, and a stationary playhead. |
| 9 | BUG-09 | Improve time to the first video frame or PDF page, seek latency, and transferred bytes. Clarify original and compatible playback, retry, and download. Check preview features and saved progress together. | Compare cold and warm opens of seven existing format fixtures and fallbacks. Seek before full delivery, switch files during loading, and reopen. Check chapter and hover previews, PDF progress, ZIP contents, bounded requests, and fallback. |
| 10 | BUG-15 | Make long documents and wide sheets readable on phones. Keep the selected sheet and original download identifiable. Distinguish empty sheets from failures. | Use a workbook with wide, empty, and long-value sheets. Switch by touch and keyboard, then open a damaged document and download its original. |
| 11 | BUG-05 | Show the real source and intended destination for Move. Keep a recoverable selection and explain mixed parents, duplicate folder names, and a missing destination. | Move from nested My Files, Recent, and Starred. Try mixed parents and duplicate names. Invalidate the chosen destination before confirming and recover with the selection intact. |
| 12 | BUG-12 | Clarify archive inputs, destination, and retry. Handle incorrect passwords without leaving partial output. | Round-trip an encrypted ZIP with nested, Unicode, long names. Try a wrong password, then a correct one. Verify one complete result and intact originals. |
| 13 | BUG-13 | Give add, change, and remove password a consistent flow. Identify the PDF source and output copy, and distinguish current from new passwords. | Round-trip all operations in Docker. Check text, password behavior, source bytes, incorrect credentials, and failed output writes. |
| 14 | BUG-14 | Show protection, expiry, and revoke clearly. Make a failed guest unlock recoverable and verify sessions after permission changes. Add owner-visible Link opens after these checks. | Use fresh owner and guest sessions for direct links and embeds. Unlock, navigate to a child, rotate the password, expire, and revoke. Check access and counting independently. |

## Frontend review by workflow

| Workflow and feedback IDs | Improvement | Acceptance |
| --- | --- | --- |
| Shared design and accessibility · FP-01, 03, 04, 16, 20, 24, 26, 30, 31, 33, 36, 40, 42, 69 | Use Settings for density, typography, and control hierarchy. Keep blue primary. Fix shared CSS and controls for help alignment, contrast, focus, and notification collisions. Use motion for feedback. | Complete keyboard, pointer, and touch workflows. Check both themes, forced colors, reduced motion, zoom, phone landscape, dialog focus return, and help dismissal. Verify control clicks leave no selection and file text can still be selected. Inspect real captures. |
| Shell and access · FP-08, 10, 11, 21, 35, 68, 76 | Keep file navigation direct and make Tools and settings collapsible. Keep the active tool easy to find and the drawer usable. Review setup, rejected login, expiry, logout, and recovery-only access together. | Reach final sidebar items at 1536×695 without moving the work area. Use wheel over links and footer. Dismiss the phone drawer and recover a rejected login with the keyboard open. |
| Explorer and file actions · FP-09, 12, 13, 14, 23, 27, 37, 41, 43, 47, 48, 50, 64–67, 73, 78–80 | Preserve first-visit List, saved preference, aligned metadata, and filename-first Grid cards with 16:9 thumbnails and five columns at 1536 px when the desktop workspace has room. Check complete filenames, filtering, search, sort, breadcrumbs, pagination, indicators, selection, and uploads together. | Find and inspect two similarly named files across pages. Switch layout once, select, open the action menu, and open the file. Verify desktop click, Shift-click ranges, Ctrl/Cmd-click toggles, right-click, and keyboard focus as well as touch taps and holds. Check checkbox accuracy and the read-only share. |
| Transfers and Preloads · FP-17, 32, 52 | Keep state, filename, progress, speed, and next action easy to scan. Keep history filters, torrent selection, batch actions, and cache eviction reachable while work runs. | Upload while browsing, return to filtered history, retry only one item, and inspect or evict one cached file. Check focus, overflow, and filter retention at 320 px, in landscape, and at zoom. |
| Performance · FP-02, 25, 39, 44–46, 49, 54–61, 75, 77 | Preserve the approved layout and finish the populated Automatic tuning demo. Simplify repeated readings and raw labels. Keep five-second live updates, direct chart interaction, and clear separation of library, server, and staging data. | Identify an observed constraint and follow its action. Read a tuning value and reason. Check all tabs, hover/tap/keyboard chart inspection, a missed poll, and automatic recovery. |
| Settings, Connections, and Backups · FP-05, 07, 15, 22, 29, 51, 75 | Preserve independent drafts and scoped saves. Put errors beside fields. Make account identity, health, and Manage easy to correlate. Distinguish backup creation, readiness, and recovery. | Edit two categories, save one, recover a conflict in the other. Correct timezone and Premium code with the phone keyboard open. Walk restore from a clean test installation. |
| Previews and readers · FP-06, 19, 53, 71, 72, 74 | Prioritize content and keep Close, retry, download, and relevant controls reachable. Check PDF toolbar height and first readable content. Include image, audio, text, video, PDF, EPUB, comics, Office, ZIP, and unknown files. Restore the latest PDF and video positions and remove the large resume prompt. | Open, interact, close, and return focus to the source for each family. At 320 px and in landscape check long names, load failure, retry, popover Escape, and original download. |
| Shares and public embeds · FP-18, 28, 34, 38, 52, 62, 63, 70 | Make protection, expiry, link opens, and actions scannable. Preserve anonymous read-only access, mutation ordering, copy feedback, and pagination focus. | Create, edit, copy, unlock, expire, and revoke in separate owner/guest contexts. Check password field sizing and guest unlock in both themes. Delay an old refresh during an edit. Move focus while an embed pagination request is pending. |

## New features

- **Copy playback diagnostics** belongs in the existing player options and
  recovery UI. Generate it locally on request using allowlisted observations
  from the active player. Include build, player mode, available container and
  codec, position, rate, last buffering transition, buffer coverage, and frame
  progress. Add audio or delivery state when measured. Exclude filenames,
  paths, URLs, share tokens, IDs, and headers. If clipboard access fails,
  provide selectable text. Check native and enhanced playback, a real held
  response, and absent measurements.
- **Link opens** counts a successful public share or embed entry once after
  display. A deliberate reload or later visit counts again. Unlock followed by
  display counts once. Child navigation within the entry, failed access,
  downloads, pagination, and media requests add zero. Use an opaque event ID
  to deduplicate retries and an owner-only aggregate with no visitor history.
  Counting is best effort so access stays available. Preserve counts across
  edits and portable restore. Remove them on revocation. Older records start
  at zero with a tracking-start indication. Test concurrency, retries, old
  backups, and restoration to an empty database.

## Verification and handoff

- Capture the current behavior and the failure path before changes. Record the
  build, fixture, viewport, theme, input method, and observed result. Compare
  latency with matched cold and warm runs and visuals with matched data.
- Use the project UI skills, existing Mantine controls, and UI SDK only for
  behavior the existing controls lack. Use Settings as the density reference
  and skip Impeccable. Review actual rendered results.
- Compare 1536×695 and 390×844 first. Also check 320 px portrait, 740×360
  landscape, and 820×1180 tablet where layout is affected. Use Chromium broadly
  and Firefox/WebKit for relevant media, reader, input, and embed workflows.
  Record real-device and assistive-technology checks distinctly from emulation.
- Exercise populated, empty, loading, stale, failed, retrying, and recovered
  states where relevant. Use Docker with a fresh database name and isolated
  storage. Use dedicated Telegram channels for live checks.
- Record code changes, verification actually run, results, remaining gaps,
  and next action after each ID. Treat fixture results, live verification, and
  owner visual acceptance as distinct evidence. Preserve local changes and
  keep delivery on dev.

The biggest gaps at the start of this pass are pooled Telegram intake under
live load, the cause of the reported backup burst, and hosted MKV audio/video
synchronization.
