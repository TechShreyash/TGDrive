# TGDrive bug plan

The current [backlog](bugs.md) sets the execution order. Verification results
below cover the local dev checkout at `287862d`, including working-tree changes.
The original first batch focused on bot intake recovery and pooled downloads
(BUG-01 and BUG-02). Its results are retained below.

## Backlog in execution order

| ID | Expected behavior | Status | Smallest useful verification |
| --- | --- | --- | --- |
| BUG-16 | Fail running jobs after a restart and require an explicit retry. | Verified locally | Restart with a running job and a completed job. Confirm only the running job becomes Failed and stays idle until retry. |
| BUG-01 | Fail a timed-out bot import with "Telegram timed out. Try again." Retain explicit owner review for a copy whose outcome is uncertain. | Verified locally | Simulate the timeout and a lost copy response. Check the error, durable journal, and owner-only queue recovery. |
| BUG-02 | Process shared-channel imports sequentially, reserve staging safely, and clean up temporary copies while browsing and playback keep working. | Locally verified. Live Telegram check blocked. | Send two encrypted files through a dedicated intake channel while playing a video with at least two eligible readers. Inspect processing order, reader usage, and leftover messages. |
| BUG-04 | Honor the automatic interval after a manual backup, and distinguish pin discovery from restore availability. | Fixes verified locally. Reported burst remains unattributed. | Compare a burst's timestamps and effective interval with publication triggers, the actual Telegram pin, and the local archive. |
| BUG-07 | Keep transfer history and browsing responsive. Expose independent upload and download limits. | Verified locally | Browse during an upload, restore filtered transfer history, and reload after saving two different limits. |
| BUG-08 | Report resource limits accurately and keep cache and competing reads within policy. | Verified locally | Compare reported limits with Docker and exercise competing reads and bounded video caching. |
| BUG-03 | Identify the cause of the reported video stalls and keep sound synchronized through recovery. | Audio hold, repeated refill recovery and sparse-frame playback verified locally. Hosted desync remains open. | Hold video delivery in a local browser fixture and check presented frames, audio output and playback position before, during and after recovery. Compare live range and wait evidence if available. |
| BUG-06 | Paint the right frame on paused open and paused seek. | Verified locally | Check that rendered pixels change after a seek while playback stays paused. |
| BUG-09 | Improve startup and seeking, remember progress, and make chapter, hover, compatibility, and ZIP previews usable. | Verified locally | Open and seek video and PDF, check resume and previews, play unsupported media through the fallback, and browse a nested ZIP. |
| BUG-15 | Preview readable DOCX text and XLSX worksheets. | Verified locally | Open a document, switch worksheets, and download a damaged original from its error state. |
| BUG-05 | Start Move in the real source parent and show its canonical breadcrumb. | Verified locally | Open Move on a nested file from My Files, Recent, and Starred. Confirm mixed-parent selections open at My Files. |
| BUG-12 | Create and extract password-protected ZIPs. | Verified locally | Round-trip an encrypted ZIP and check that a wrong password publishes no output. |
| BUG-13 | Add, change, or remove a PDF password by saving a separate copy. | Verified locally | Apply all three operations and compare password behavior, page text, and source bytes. |
| BUG-14 | Require a password on selected share links and invalidate unlocks on changes, expiry, and revocation. | Verified locally | Unlock a folder in a fresh browser and check direct child access, embeds, and revocation. |

## Verification status

### BUG-16 · Implemented and verified locally

- **Changed files** — `tgdrive/core/runtime.py`, `tgdrive/transfers/{tasks,manager}.py`, `docs/{architecture,operations}.md`, and the durable-job, lineage-retry, multipart, remote-job and poster recovery tests in `tests/transfers/`.
- **Checks** — The shutdown and crash regression failed before the fix. After the fix, startup records the interrupted transfer as Failed with a short reason and sends nothing until the owner retries. The retry keeps the staged bytes, provider deadline and send identity. The transfer, bot journal and runtime suite passed 569 tests and 24 subtests. Scoped Ruff passed.
- **Next** — Implement the revised BUG-01 timeout handling. Previously queued work and already requested cancellation cleanup keep their existing behavior.

### BUG-01 · Uncertain-copy recovery verified locally

- **Changed files** — `tgdrive/transfers/tasks.py`, `tgdrive/transfers/manager.py`, `tgdrive/telegram/bot_intake.py`, `tgdrive/web/api_v1.py`, `website/src/shared/types.ts`, `website/src/features/transfers/{TransferPanel.tsx,TransferPanel.css,transferTasks.ts,transfers.ts}`, `tests/telegram/test_shared_intake.py`, `tests/storage/test_mongodb.py`, `tests/web/test_demo_admin_auth.py`, `tests/support/browser_transfer_app.py`, `tests/browser/test_browser_bot_settings.py`, and `docs/{api,architecture,usage}.md`.
- **Checks** — 91 focused Python tests, 2 isolated MongoDB cases, 313 Transfers frontend tests, and 2 Chromium cases against local TGDrive with synthetic Telegram passed. The production frontend build and scoped Ruff and Prettier checks passed. Inspected the desktop and phone dialog renders. The lost-response test confirms the queue remains paused through restart and a failed journal write, then advances after a durable owner acknowledgement. The unknown copy and source are left alone, and retry cannot replay it.
- **Next** — The original uncertain-copy task is complete. See the backlog reread below for the separate timeout request.

### BUG-01 · Revised timeout handling implemented and verified locally

- **Changed files** — `tgdrive/telegram/bot_mode.py`, `tests/telegram/test_bot_intake.py`, and `docs/usage.md`.
- **Checks** — Source lookup and download timeout tests failed before the fix. They now confirm that the transfer reports Failed with “Telegram timed out. Try again.” and succeeds when the owner retries. The 112 focused bot, pooling, settings, and reconnect tests passed. Scoped Ruff and Pyright passed.
- **Next** — BUG-02. When a copy may already have succeeded, the transfer keeps the review instructions so a timeout cannot lead to an unsafe duplicate.

### BUG-02 · Cleanup fix implemented and verified locally. Live verification blocked

- **Change of direction** — The checkout already pooled background reads and serialized bot imports. The terminal-cleanup fault case exposed an unsaved retry deadline when the transfer was already terminal. Chromium also showed that Transfers replaced the pending-cleanup label with a generic completion message. Persist the deadline, retain the original transfer outcome, and keep pending cleanup visible through history pagination.
- **Changed files** — `tgdrive/telegram/shared_intake.py`, `tgdrive/transfers/tasks.py`, `website/src/features/transfers/{TransferPanel.tsx,transferHistory.ts,transferHistory.test.ts}`, `tests/telegram/test_shared_intake.py`, `tests/support/browser_transfer_app.py`, `tests/browser/test_browser_bot_settings.py`, and `docs/usage.md`.
- **Checks** — The terminal-cleanup regression failed before the fix and passed after it. The local provider fixture verified that the known copy and its retry deadline survive restart and history clearing. A video range returned the expected bytes while a bot read was held. Once released, two queued files finished in order, multiple clients fetched chunks, and both temporary copies were removed. The final focused run passed 193 Python tests, 314 Transfers frontend tests, and 7 Chromium bot settings and intake cases. The frontend build, scoped Ruff, Pyright, Prettier, and `git diff --check` passed. Two isolated MongoDB cases also passed during the batch.
- **Next** — Live verification needs a local TGDrive instance with a disposable private intake channel, the main bot with post and delete rights, and at least two eligible storage clients that can read the channel. No local instance was configured with an intake channel. Still unverified against Telegram are the two-file queue order, distribution of actual Telegram reads, continued playback during intake, and removal of the actual temporary messages.
- **Current checkpoint** — Rechecked the local setup after BUG-16 and the revised BUG-01. There is still no configured private intake channel. The focused reruns passed 118 transfer-recovery and runtime cases and 112 bot cases. A combined rerun stalled and was stopped. The live check stays open while independent local tasks proceed in backlog order.
- **Docker verification** — Built the checkout as `tgdrive-bug02-qa:local`. Added an authenticated folder listing check to the existing pooled-intake test in `tests/telegram/test_shared_intake.py`. Docker passed 17 pooled-intake and isolated MongoDB tests. The actual folder-list API and a foreground video range both returned while intake held a background read open. Docker also reached the MongoDB server configured in `.env`, and both journal reopen tests passed against fresh `tgdrive_test_*` databases on that server. The existing database and Telegram channels were outside the test. Scoped Ruff and Pyright passed.
- **Live setup still needed** — A secrets-safe check of `.env` found a main bot and one storage client, with no separate `BOT_INTAKE_CHANNEL`. The live two-client test needs a disposable storage channel and private intake channel, a main bot with post and delete rights, and two configured readers with access. Use an owner-authorized test account to send the files.
- **Docker HTTP follow-up** — The pooled-intake case now opens a populated folder through the authenticated nodes endpoint and reads its video through `/file` while the background transfer is held. The response is HTTP 206 with the expected bytes. All 17 isolated Docker checks passed again, plus scoped Ruff and Pyright. A fresh configuration check still found one reader and no intake channel.

- **Configuration follow-up** — A read-only Docker check of `.env` found one distinct bot credential, zero account sessions, and no dedicated intake channel. No TGDrive app container was running locally. The live verification remains blocked.

### BUG-04 · Local fixes verified. Reported burst unattributed

- **Changed files** — `tgdrive/recovery/mongo_backup.py`, `tests/storage/test_mongodb_unit.py`, and `docs/backup-recovery.md`.
- **Findings** — A manual confirmation close to the scheduled timer could trigger a second standalone backup. Startup also treated the unstarted Telegram pool as a failed historic pin check and changed the saved backup record. The scheduled wait now follows the latest confirmation. Startup preserves older backup evidence and checks the pin when Telegram connects. The guide distinguishes scheduled runs, retries, status refreshes, and independent checks.
- **Checks** — The new scheduler and cold-pool regressions failed before their fixes. Eight focused regressions, 201 Docker backup and recovery cases, and four Chromium Storage warning cases passed. Scoped Ruff, Pyright, and whitespace checks passed.
- **Retry follow-up** — The scheduler woke every 60 seconds and logged a failed retry during a longer saved Telegram cooldown. The new regression reproduced waits of 1, 60, and 60 seconds. With the fix, the scheduler honored a 600.25-second cooldown, pinned the same verified message, and returned to the normal backup interval. The log shows the planned retry wait. All 202 focused Docker backup, recovery API, and replication cases passed, along with scoped Ruff, Pyright, and whitespace checks. The local `.env` sets or defaults to 86,400 seconds between standalone backups.
- **Next** — Continue with BUG-07. The running instance has no available backup event logs for the reported period, so the cause of that particular burst remains unverified.

### BUG-07 · Verified locally

- **Changed file** — `tests/browser/test_browser_performance_ui.py` now checks that the actual settings endpoint saves different upload and download limits, and that both values appear after reload.
- **Checks** — Five Chromium cases covered retained history rows during slow reloads, browsing during uploads, the Performance link to transfer settings, and directional limits. Fifty-five Docker checks passed for history, destination admission, and Telegram receipts. Scoped Ruff and whitespace checks passed.
- **Next** — BUG-08. The tests establish responsiveness under held requests and upload bursts. Cold-load latency against the running deployment was not measured.

### BUG-08 · Verified locally

- **Changed files** — `tgdrive/storage/mongo_store.py` and `tests/media/test_cache_metadata.py`. The existing implementation provided the controls. The final sweep caught cache-label pagination stopping when the catalog root sorts at a page boundary. The scanner now accepts the root ID as a continuation cursor.
- **Checks** — 154 Docker cases passed for resource measurements, cache retention, adaptive memory, reader fairness, and bounded prefetch. A real 384 MiB Docker cap and filesystem totals matched TGDrive's resource snapshot. Nine Chromium cases passed for cache controls and resource labels.
- **Follow-up check** — A deterministic root-cursor regression failed before the fix. All 12 cache metadata tests passed in Docker afterward.
- **Next** — BUG-03. Hosted deployment measurements were outside the isolated checks.

### BUG-03 · Local fixes verified. Hosted stall open

- **Changed files** — `website/src/features/previews/video/{AdvancedVideo.tsx,videoPlayerPresentation.ts}`, `tests/browser/test_browser_player_controls.py`, and `docs/performance.md`.
- **Checks** — Four Chromium cases passed for delayed opening and retry, playable startup before the rest of the response arrives, and the engine's buffered range after seek. These cover simulated delivery stalls and recovery. They do not identify the cause of the reported stalls.
- **Follow-up checks** — Five additional Chromium cases passed. Injecting a frozen picture held the audio until frames advanced. The video continued through two seeks within prefetched bytes during an outage. Simulated delivery that stopped or became unavailable offered retry sooner than delivery that kept receiving bytes.
- **Reproduced stall** — A generated MP4 with one picture every three seconds could trigger a corrective seek from the engine while data was available. Disabling that one check for a diagnostic run allowed the playhead and frames to advance. TGDrive still showed a buffering overlay because its frame-progress threshold discarded three-second advances.
- **Fix and verification** — The player now uses the active track's frame rate to time freeze detection and recognize advances in sparse footage. The final Chromium case passed twice, showing at least three presented frames without a corrective seek or buffering overlay. Five controlled playback and six compatibility or paused-frame Chromium cases passed. The 37 video unit cases passed in Docker, all 823 frontend tests passed locally, and the frontend build, Ruff, and whitespace checks passed.
- **Live report** — A Chrome session using the enhanced MKV player visibly buffered near 04:30. The owner additionally heard audio advance while the picture waited or resume out of sync. The passive browser observation confirmed the loading state and subsequent visual recovery. It did not measure audio output or identify the deployed player build.
- **Audio and picture hold implemented locally** — `AdvancedVideo.tsx` applies the existing bound-audio and playback-clock hold when it first shows a buffering wait. During a refill it keeps the wait until real picture progress. A temporary internal pause with play intent still set no longer clears the wait.
- **Audio regression checks** — The early-wait MKV test failed against the earlier build because the visible wait still had a playing engine. It passed with the hold in place. Ten focused Chromium checks passed for early waiting, frame starvation, real stalled byte-range delivery, sparse video, seeking, and audio exceptions. The generated MKV range probe measured a suspended AudioContext during the stalled response and audio/picture skew within 24 ms once frames resumed. Audible output began about 73 ms before the first fresh frame during the engine restart. Tests live in `test_browser_player_controls.py` and `test_browser_mkv_refill.py`.
- **Refill follow-up** — Under a delayed audio restart the engine emitted a compatibility pause while the player still intended to play. That event removed the loading indicator before the first recovered frame. The indicator now ignores that event while the play intent remains active. A deterministic held-range test failed on frozen assets and passed after the fix. Nine Chromium checks covered the refill, genuine user pause, seeking, and related recovery paths.
- **Repeated refill follow-up** — `AdvancedVideo.tsx` gives the renderer a fresh frame deadline when the engine returns to playing. Its old timeout could suspend playback again even with sufficient decoded audio and pictures. The local Chromium regression in `test_browser_mkv_refill.py` failed on the frozen build and passed on the candidate. Two genuine held synthetic MKV ranges at 1.5× left audio and pictures stationary during the waits. Fresh pictures arrived 837 and 421 ms after delivery resumed. The first audible output led the fresh picture by 59 ms in one refill and with no lead detected at 45 ms sampling in the other. An intentionally delayed renderer still produced short audio leads.
- **Sparse-frame follow-up** — The full pinned Chromium run exposed an intermittent stall in a generated 9s clip with a picture every three seconds. Stack probes found two engine paths. Audio recovery sought ahead even when scheduled sound covered the playback clock, and picture catch-up discarded the decoder queue during a file’s normal frame gap. The build transform in `website/scripts/movi-player-build.ts` now bounds audio demand by the playback clock and allows the active frame interval before picture catch-up. Its reviewed package version and hash are unchanged. `AdvancedVideo.tsx` also starts the deadline from the first real frame and leaves the seek indicator for explicit seek events. Six instrumented sparse runs, 13 focused Chromium checks and all ten transform tests passed against the same isolated candidate.
- **Next** — Verify synchronization on the owner’s hosted file. The recovered picture timing and audio output on that deployment remain unverified.

### BUG-06 · Verified locally

- **Changed file** — `tests/browser/test_browser_preview_player.py` now asserts that the default player paints a visible picture on opening and changes the actual pixel output on a paused seek.
- **Checks** — Chromium passed the new H.264 MP4 and software MPEG-4 WMV cases. The existing compatible-mode WMV case also passed its decoded-pixel and paused-seek assertions. Scoped Ruff and whitespace checks passed.
- **Next** — BUG-09.

### BUG-09 · Verified locally with expanded video fallback

- **Changed files** — `tgdrive/media/{compatible_playback,compatibility_worker}.py`, `website/src/features/previews/{Preview.tsx,Preview.routing.test.ts}`, `tests/browser/test_browser_compatible_playback.py`, and `docs/performance.md`.
- **Checks** — Twenty-one Chromium cases passed for PDF ranges and page restoration, video resume and card progress, chapter/hover previews, and ZIP browsing. Sixty-six Docker API and media cases passed. Added fallback coverage for 3GP, FLV, OGV, VOB, and MPEG transport streams, with ASF routed to the player. Generated samples in Docker passed native probing and conversion for 3GP, FLV, OGV, VOB, and M2TS. Chromium verified actual TS and ProRes MOV compatible playback and seeking. The frontend build, eight routing tests, and scoped Ruff and Pyright passed.
- **Next** — BUG-15. The tested playback paths improve format coverage. The live buffering and audio sync follow-up stays in BUG-03.

### BUG-15 · DOCX and XLSX previews implemented and verified locally

- **Changed files** — `tgdrive/media/book_worker.py`, `tgdrive/media/books.py`, `tgdrive/media/previews.py`, `website/src/features/previews/OfficePreview.tsx`, `website/src/features/previews/Preview.tsx`, `website/src/shared/types.ts`, `docs/usage.md`, Office parser and browser regression tests, and the browser fixture.
- **Checks** — Nine Office and ZIP parser tests passed in Docker. Chromium opened a DOCX, switched between two XLSX worksheets, and downloaded an invalid DOCX from the preview error. The frontend build, Ruff, Pyright, and whitespace check passed.
- **Follow-up check** — Updated `website/src/features/previews/Preview.test.ts` to cover lazy-load failures in all four viewers, including ZIP and Office. The complete frontend suite passed 823 tests.
- **Next** — BUG-05. The preview scope is read-only document text and worksheet cells, with original download available for full formatting and unsupported Office formats.

### BUG-05 · Verified locally

- **Changed files** — `tests/browser/test_browser_move_start.py` and `tests/support/browser_transfer_app.py`. The working-tree Move dialog already uses the real parent path and server breadcrumbs.
- **Checks** — Both Chromium regressions passed. A three-level path was identical from My Files, Recent, and Starred. An intermediate ancestor was selectable, and a mixed-parent selection opened at My Files.
- **Next** — BUG-12.

### BUG-12 · Password-protected ZIP implemented and verified locally

- **Changed files** — `tgdrive/transfers/{archive_worker,archives,manager,tasks}.py`, `tgdrive/web/{archive_api,api_v1}.py`, the archive dialog/API and transfer retry controls in `website/src/`, archive tests, and `docs/{api,usage}.md`.
- **Checks** — Ninety-five Docker archive, format, and API tests passed. An AES-256 ZIP round trip preserved nested files and empty folders. A missing password requested a fresh one, a wrong password published nothing, and the correct password restored identical bytes. Chromium passed 16 archive cases and the password retry case passed on rerun after a required-label selector correction. One hundred twenty frontend unit tests, the build, Ruff, Pyright, and whitespace checks passed.
- **Next** — BUG-13. Archive password support covers ZIP. Passwords are held for one processing attempt and omitted from the journal.

### BUG-13 · PDF password operations implemented and verified locally

- **Changed files** — `tgdrive/transfers/{archives,manager,pdf_passwords,tasks}.py`, `tgdrive/web/{archive_api,api_v1}.py`, the PDF password file action and Transfers retry controls, `Dockerfile`, usage/API/development guides, and focused transfer, API, and browser tests.
- **Checks** — Ninety-seven Docker tests passed using QPDF 12.2, including an add/change/remove round trip. Each copy preserved text, the old password stopped opening the changed PDF, the removed-password copy opened without credentials, and the source bytes stayed identical. A wrong password published no output. The API check verified owner authorization and exposure consent. Six Chromium password cases passed, including the new file-menu flow, the existing PDF reader, and ZIP retries.
- **Next** — BUG-14. Production password operations require QPDF and prlimit, now included in the Dockerfile.

### BUG-14 · Password-protected share links implemented and verified locally

- **Changed files** — `tgdrive/storage/mongo_store.py`, `tgdrive/web/{api_v1,app,http_guard,share_links}.py`, owner share dialogs and public unlock forms, API and recovery tests, the Chromium embed test, and the usage/API/architecture guides.
- **Checks** — The Docker suite passed 107 share, embed, access-revalidation, media and PDF tests. A separate Docker MongoDB test verified lock persistence after reopening the database and restoring a portable snapshot. Chromium verified the share creation form, incorrect password, scoped unlock, child download, file embed, and revocation from fresh contexts. A local HTTPS cross-site iframe check confirmed that an unlock worked inside its partition while direct top-level navigation still required a password. The FastAPI check verified hash rotation, session expiry, and removing protection.
- **Next** — Live BUG-02 verification needs isolated Telegram channels and two eligible readers. BUG-04 needs events from the reported backup burst. BUG-03 needs a reproducible stall.

No push was made.

### Backlog reread

- [The current backlog](bugs.md) uses BUG-01 for timeout handling. The two BUG-01 progress entries above distinguish timeout handling and the earlier uncertain-copy recovery work.
- The backlog adds BUG-16 for interrupted jobs after restart and omits BUG-10 and BUG-11. The approved first-batch scope and its verification history are preserved here.
- Rechecked the first batch on the current checkout. All 42 focused Python tests and both Chromium intake-review cases passed. The isolated MongoDB rerun also passed both the intake-review persistence case and the job-journal reopen case. Live BUG-02 verification remains blocked because no intake channel is configured.

### Final regression pass

- The frontend build, all 823 Vitest cases, and the combined 44-case Chromium suite passed.
- The broad Docker regression pass recorded 2,607 passing tests, 36 passing subtests, 51 opt-in or unavailable-tool skips, and four failures. The follow-up fixed the cache cursor, used a still-unsupported 7z fixture, and kept the exact-version dependency assertion compatible with pinned prereleases. The corrected Docker mount supplied the frontend source files. The 20-case Docker rerun covered all four failures plus adjacent paths and passed.
- All 14 entries in the execution table match the current backlog in order. Live pooled intake, the reported backup burst, and the original buffering report remain open for the evidence described above.

## Improvement pass on dev

The [detailed improvement plan](improvement-plan.md) records the full ordered
scope. This pass checks the current working tree independently of earlier
completion labels.

### BUG-16 · Recovery guidance implemented and verified locally

- **Confirmed before editing.** Startup already failed running jobs and preserved
  their send identity. The transfer row gave the same retry action after losing
  staged bytes. Five new recovery assertions failed before the change.
- **Changed files.** Transfer manager, registry and job type now surface an
  interrupted upload's recovery path. Transfers shows whether Retry uses the
  staged file or finishes existing work, and tells the owner when to restore
  staging or cancel and upload the original. Missing input is checked again on
  Retry. Transfer history now enables the row actions as soon as the matching
  live job loads, even if history arrived first. Updated the API and operations
  guides and the durable-job and browser recovery tests.
- **Checks.** 543 transfer/runtime tests and 24 subtests passed. A QPDF test was
  skipped because the host lacks the required native tools. The tests simulated
  shutdown and crash, reopened twice, preserved a completed transfer, and
  confirmed one saved file after retry with the same send identity. They also
  covered missing staging before dispatch, restored staging after dispatch,
  and finalization after the catalog commit with staging absent. The isolated
  Docker MongoDB journal check and 314 Transfers unit tests passed. Nine
  Chromium recovery and pagination cases passed, including a held job-list
  response, keyboard retry at 1536×695, and touch retry at 390×844. The production
  build, scoped Ruff and whitespace checks passed.
- **Evidence.** [Backend log](../cache/bug-review/bug16/backend.log),
  [browser log](../cache/bug-review/bug16/browser.log),
  [laptop capture](../cache/bug-review/bug16/bug-16-interrupted-1536.png), and
  [phone capture](../cache/bug-review/bug16/bug-16-interrupted-390.png).
- **Next.** BUG-01. Recovery tests used a simulated Telegram transport. The
  checks verify local journal and dispatch behavior.

### BUG-01 · Timeout and intake review guidance implemented and verified locally

- **Confirmed before editing.** Definite Telegram timeouts already appeared as
  Failed and could be retried. The original bot chat could stay at Queued after
  a source lookup timeout. The copy warning did not name the review action.
  Four new assertions reproduced the missing feedback before the change.
- **Changed files.** Updated the Telegram bot workflow, private reply adapter
  and shared-intake copy message. Transfers labels retryable bot failures and
  explains what reviewing an uncertain copy does. Updated the usage guide,
  Telegram tests, and Chromium bot workflow fixture and tests.
- **Checks.** 247 Telegram and demo authorization tests passed. Source lookup
  and download timeouts persisted across journal reopen, with no retry before
  owner action. Two retry requests produced one completed import. Failed chat
  feedback left the transfer failure intact. For a known copy, Retry became
  available after cleanup. For an uncertain copy, the queue stayed paused
  through restart and a simulated journal write failure. Repeated confirmation
  preserved the original review time. Tests checked the exact copy and delete
  counts and the original attachment. Two isolated Docker MongoDB checks,
  314 Transfers unit tests, 14 Chromium bot and recovery cases, the production
  build, scoped Ruff, and whitespace checks passed. Desktop keyboard and phone
  touch checks exercised Retry, review confirmation, Escape, and focus return.
- **Evidence.** [Backend log](../cache/bug-review/bug01/backend.log),
  [browser log](../cache/bug-review/bug01/browser.log),
  [MongoDB log](../cache/bug-review/bug01/mongo.log),
  [laptop timeout](../cache/bug-review/bug01/bot-intake-timeout-1536.png),
  [phone timeout](../cache/bug-review/bug01/bot-intake-timeout-390.png),
  [laptop review](../cache/bug-review/bug01/bot-intake-review-1536.png), and
  [phone review](../cache/bug-review/bug01/bot-intake-review-390.png).
- **Next.** BUG-02 in the detailed plan. A dedicated Telegram intake channel
  and two eligible readers are needed for live pooled-intake verification.
  Real Telegram timing and chat delivery were outside this local batch.

### BUG-02 · Bot reconnect and staging feedback verified in Docker

- **Confirmed on the current checkout.** Existing code serializes intake and
  reserves space for the active file. The pooled reader preserves foreground
  capacity and journals cleanup. The fresh Docker regressions exposed two gaps.
  A storage wait reused the generic upload-slot phase and the initial bot reply
  omitted destination and encryption. A main bot connection error also aborted
  startup even when the storage connection and catalog were ready.
- **Changes.** Bot replies show the accepted folder and encryption mode. The
  bot and Transfers name a temporary-storage wait, and Transfers identifies a
  reconnect wait. The runtime leaves the drive ready during a bot-only startup
  failure and retries the same bot instance after storage is ready. A bot-only
  retry does not trigger another storage backup. Updated the runtime, bot
  mode, Transfers job presentation, related tests and architecture and usage
  guides.
- **Docker verification.** 340 focused tests passed with the current source
  mounted read-only and a fresh MongoDB 8 replica set on a private Docker
  network. The pooled test used a randomly named `tgdrive_test_*` database, a real catalog
  and job journal, and simulated Telegram. While the first 8 MiB import was held, it alone reserved staging
  space. An authenticated folder listing returned HTTP 200 and a foreground
  video range returned HTTP 206 with the expected bytes. At least two readers fetched
  intake data. Both files completed in queue order. Reopening the journal
  confirmed completion and cleanup. Both distinct temporary messages were
  deleted, both source attachments survived, and the staging reservation
  returned to zero. Other cases verified cancellation while waiting, automatic
  resumption when storage or the bot connection returned, and startup recovery
  with one registration of the bot handlers and one storage backup.
- **UI and code checks.** Ten Chromium cases passed, including the bot
  destination, encryption toggle, timeout and review flows, and both wait
  labels at 1536×695 and 390×844. The frontend build, 314 Transfers unit tests,
  scoped Ruff and Pyright and whitespace checks passed.
- **Broader suite result.** The broader Docker run had 360 passes and one
  share-listing test failure, reproduced in isolation. Its authorization
  stub accepts two arguments and the current listing passes a third unlock
  argument. The test times out waiting for the stub's synchronization event.
  This run leaves that share fixture outside BUG-02.
- **Unverified.** Local configuration has no dedicated Telegram intake
  channel. Actual account reconnect behavior, read distribution, temporary
  message deletion and uninterrupted playback under real Telegram load remain
  unverified. The live check needs a private test intake channel and two
  authorized readers, with the main bot able to post and delete. Local
  browser and byte-range checks do not measure hosted playback continuity.
- **Evidence.** [Focused Docker run](../cache/bug-review/bug02/docker-focused.log),
  [broader Docker run](../cache/bug-review/bug02/docker.log),
  [share-test rerun](../cache/bug-review/bug02/docker-share-rerun.log),
  [browser results](../cache/bug-review/bug02/browser.log),
  [laptop](../cache/bug-review/bug02/bot-intake-waits-1536.png) and
  [phone](../cache/bug-review/bug02/bot-intake-waits-390.png).
- **Next.** BUG-04 is next in the improvement plan.

### BUG-04 · Backup trigger and schedule verified locally

- **Confirmed on the current checkout.** Standalone scheduling already waited a
  full interval after manual confirmation. Publication history lacked a trigger.
  Reopening with a newer pending attempt could hide the older confirmed point.
  The pin-discovery warning and downloadable archive remained separate.
- **Implemented.** Standalone backups record whether creation was scheduled or
  requested through Back Up Now and keep that reason across retries and restarts.
  Storage and Backups shows the previous confirmation, the next eligible time
  or pending retry, and any known retry deadline. Older points without a recorded
  reason still show their confirmation. Existing independent-backup status
  remains separate.
- **Changed files.** `tgdrive/recovery/mongo_backup.py`,
  `website/src/shared/types.ts`, `website/src/features/settings/StorageView.tsx`,
  `website/src/features/settings/StorageView.css`, `docs/backup-recovery.md`,
  `tests/storage/test_mongodb_unit.py`, and
  `tests/browser/test_browser_channel_backup.py`.
- **Docker verification.** 191 storage and replica tests passed. A separate probe
  used the production snapshot, pin confirmation, archive staging, and activation
  paths with a fresh MongoDB 8 replica set and simulated Telegram. It published
  two archives, one scheduled and one manual within the interval. Reopening and
  invoking the early scheduled path left send, pin, and history counts at two.
  The manual archive restored to a second, empty database with the original
  drive identity and file reference. Reapplying the activation was a no-op.
  Both database names were disposable and independent of the existing TGDrive DB.
- **Browser and code checks.** 21 Chromium cases passed, including the pending
  confirmation and Check Again at 1536×695 dark, 820×1180 dark and 390×844 light.
  The existing unverified-discovery cases checked both file availability states.
  Inspected dark laptop and light phone captures. Build, scoped Ruff, Pyright,
  and whitespace checks passed.
- **Evidence.** [Docker tests](../cache/bug-review/bug04/docker.log),
  [fresh-database restore](../cache/bug-review/bug04/docker-restore.log),
  [Chromium results](../cache/bug-review/bug04/browser.log),
  [laptop schedule](../cache/bug-review/bug04/backup-schedule-1536.png) and
  [phone retry](../cache/bug-review/bug04/backup-retry-390.png).
- **Unverified.** The reported burst has no matching events or timestamps.
  Actual Telegram publication, pin persistence, and channel discovery from a
  clean installation remain outside the isolated check. The local restore
  verified the encrypted archive and catalog reference without downloading
  the original file from Telegram.
- **Next.** BUG-07 in the improvement plan.


### BUG-07 · Retained Transfers and speed controls rechecked

- **Confirmed on the current checkout.** Five independent Chromium runs kept
  the selected history page and Successful filter visible before a held
  refresh finished. Returning stayed quiet while off-page, and a failed
  refresh kept rows visible with a retry. Search reset when leaving the route
  and an old response could not overwrite a newer search. The current speed
  settings already persisted separately.
- **Implemented.** Transfer rows report automatic retry once and show a
  countdown when the server supplies a future retry deadline. Unknown or
  expired deadlines stay quiet. Removed the initial fades from transfer rows
  and finished history's empty state. The two settings links now appear at
  the top of Tuning, with the directional speed link named explicitly.
- **Changed files.** `transfers.ts`, `TransferPanel.tsx`,
  `TransferMetrics.test.ts`, `TransferHistory.css`, `PerformancePanel.tsx`,
  `website/src/shared/pages/motion.css`, `docs/usage.md` and Chromium cases
  in `test_browser_upload_progress.py` and `test_browser_performance_ui.py`.
- **Docker checks.** 111 tests passed on a separate Docker MongoDB 8 replica set.
  Coverage included journal reopen, archive/history overlap, staging admission,
  interactive media priority, rate settings and a foreground HTTP range during
  a held encrypted import. The first run omitted the frontend source mount
  required by the settings-default check. Adding it made the complete run pass.
- **Browser checks.** 13 focused Chromium cases passed on the final build,
  apart from the expanded tuning check run separately. They covered a held
  listing refresh during uploads, retained selection and pagination, history
  search, retry and controller races, and speed and ETA at 1440, 390 and 320 px.
  A separate four-case run checked the speed settings link and save/reload at
  1536×695 dark, 820×1180 light (keyboard Enter) and 390×844 dark. Checked actual
  saved values of 80 Mbps and 20 Mbps. The no-fade assertion failed before the
  CSS change and passed afterward. Inspected light Transfers and dark Tuning
  phone captures. 375 focused frontend unit cases, build, Ruff and whitespace
  checks passed.
- **Evidence.** [Held-history repetitions](../cache/bug-review/bug07/history-repeat.log),
  [Docker](../cache/bug-review/bug07/docker-rerun.log),
  [Chromium](../cache/bug-review/bug07/browser-final.log),
  [tuning](../cache/bug-review/bug07/browser-tuning.log),
  [phone transfer retry](../cache/bug-review/bug07/transfer-retry-390.png) and
  [phone tuning link](../cache/bug-review/bug07/tuning-speed-limits-390.png).
- **Unverified.** The held-request evidence shows that interactions remain
  available before refresh completion. It gives no production latency or
  sustained throughput measurement under actual Telegram traffic.
- **Next.** BUG-08. Confirm and populate the demo tuning report and recheck
  constrained measurements against the effective Docker limits.


### BUG-08 · Automatic tuning verified in the demo and constrained Docker

- **Confirmed on the current checkout.** The local showcase returned no tuning
  report. The dashboard separated drive inventory, server cache and temporary
  upload space, but the Automatic tuning panel was empty. A new governor
  regression also showed that an apply failure advertised the candidate
  values as though they had been applied.
- **Implemented.** The showcase generates a clearly labeled example with the
  production governor's policy. Tuning shows effective values and their
  reasons, keeps numeric values together on narrow screens, and identifies
  the last applied report after it goes stale. Removed repeated ceilings from
  rows with no change and skipped unnamed internal values. Inactive
  Performance panels hide immediately on tab selection. The live governor
  publishes its new measurement and value report after successful application.
- **Changed files.** `tgdrive/core/governor.py`,
  `tests/core/test_governor.py`, fixture setup in
  `tests/support/{browser_transfer_app,showcase_data}.py`,
  `website/src/features/performance/{PerformancePanel.tsx,Performance.css,PerformanceOverview.css,performanceApi.ts}`,
  `tests/browser/test_browser_performance_ui.py`, and `docs/development.md`.
- **Docker verification.** 196 governor, runtime, capacity, adaptive memory,
  prioritized admission, cache policy, preload and cache metadata tests passed.
  Docker reported a 384 MiB cgroup limit and 1.5 CPUs as configured. Its real
  filesystem total matched the resource report. The applied cache cap matched
  25% of available memory, the reserve matched 5% of the measured filesystem,
  the staging cap was 16 GiB, and the startup ceiling of eight preload jobs was
  reduced to six. This run had no network and used local fixtures where a
  store was needed.
- **Browser and code checks.** 37 Chromium Performance and history cases
  passed. The new local showcase cases checked the real response, a stale
  report retaining values, a missing report, and automatic recovery at
  1536×695 dark and 390×844 light. The sample showed a 6 GiB disk cache, 12 GiB
  upload staging budget, and 2 GiB reserve. Inspected both layouts and
  separately checked the running demo at these widths. 61 frontend unit cases,
  the build, Ruff, Pyright, and whitespace checks passed.
- **Evidence.** [Docker tests](../cache/bug-review/bug08/docker-final.log),
  [container measurements](../cache/bug-review/bug08/container-capacity.log),
  [Chromium](../cache/bug-review/bug08/browser-final.log),
  [desktop demo](../cache/bug-review/bug08/demo-tuning-1536.png) and
  [phone demo](../cache/bug-review/bug08/demo-tuning-390.png).
- **Unverified.** Hosted resource reporting and sustained real Telegram
  stream competition remain unmeasured. The local demo values are examples,
  while the separate constrained probe measured the Docker process.
- **Next.** BUG-03. Add copyable playback diagnostics and trace held delivery
  and audio/video recovery on the current player.


### BUG-03 · Copyable diagnostics and bound refill verified locally

- **Confirmed on the current checkout.** Two held byte ranges at both 1×
  and 1.5× left the playhead and picture stationary and the audio context
  suspended. One 1.5× restart registered sound 83 ms before the next picture.
  At that restart the paused clock was 9.954 s, the last painted picture was
  9.417 s and the queued 9.458 s picture was still needed. The resume path
  discarded it using the paused clock, and the next picture was at 9.750 s.
- **Implemented.** The bound refill path keeps the next queued pictures
  relative to the last frame displayed. A plain resume keeps the original
  stale-frame cutoff. The player options and recovery controls copy local
  diagnostics, with a selectable report if clipboard access fails. The
  report includes build, mode, available codecs, position and timing. It
  uses an explicit field list without file or access identifiers. Escape
  dismisses the fallback dialog and returns to the preview.
- **Changed files.** `website/scripts/{movi-player-build.ts,movi-player-build.test.ts}`,
  `website/src/features/previews/Preview.tsx`,
  `website/src/features/previews/video/{AdvancedVideo.tsx,videoPlayerPresentation.ts,playbackDiagnostics.ts}`,
  `tests/browser/{test_browser_player_controls.py,test_browser_mkv_refill.py,test_browser_compatible_playback.py}`,
  `docs/usage.md` and `docs/performance.md`.
- **Measured result.** Repeated both ranges at both rates. The sound clock,
  picture and playhead stayed stationary throughout the visible waits. In
  the matching second 1.5× refill the next frame advanced from 9.417 s to
  9.458 s and appeared 156 ms after release, down from 521 ms in the initial
  run. No sound was detected before the next picture in the four candidate
  refills, using a 45 ms Web Audio analyser sample. After the first 500 ms
  of recovery, observed audio-minus-picture offsets ranged from −40 to
  +13 ms. This probe measures the browser audio signal.
- **Other checks.** 11 focused Chromium cases passed on the changed player,
  including sparse footage, delayed renderer restart, diagnostics during a
  real wait, and a user pause held after release. Six Chromium cases checked
  clipboard success and denial, and real bounded compatible playback.
  64 isolated Docker tests passed for previews, compatibility and watch
  progress. 240 frontend preview and build-transform tests passed. Frontend
  build, scoped Ruff, and whitespace checks passed.
- **Evidence.** [Measured timing](../cache/bug-review/bug03/refill-measurements.log),
  [Chromium regressions](../cache/bug-review/bug03/player-checks.log),
  [compatible and clipboard checks](../cache/bug-review/bug03/diagnostics-final.log)
  and [phone clipboard fallback](../cache/bug-review/bug03/playback-diagnostics-fallback.png).
- **Unverified.** Chrome opened the hosted link at the owner login screen.
  It served frontend build `BqewRmv6`. The hosted sample near 04:30, on-device
  sound output and the original cause of that report remain unverified.
- **Next.** BUG-06. Recheck pause intent and pixels after overlapping seeks.


### BUG-06 · Existing pause and seek behavior verified with marked frames

- **Confirmed on the current checkout.** Real paused opens and seeks already
  painted fresh frames. The new test generated three distinct colored sections
  with an audio track. It held the oldest seek after decoder flush, started
  two more seeks, checked that the last seek painted the blue section, then
  released the stale seek. The blue frame and paused position survived the
  late completion. The analyser detected silence throughout the paused
  sequence. Both 1536×695 and 390×844 passed, with the latter in fullscreen.
- **Changed file.** `tests/browser/test_browser_preview_player.py` now
  preserves this regression. Production behavior passed the baseline and
  required no change in this item.
- **Checks.** 12 Chromium cases passed, including real MP4 and WMV paused
  pixels, saved-position reopen, early manual seek and resume, and switching
  quality in fullscreen without restarting playback. Scoped Ruff and
  whitespace checks passed.
- **Evidence.** [Chromium results](../cache/bug-review/bug06/chromium.log),
  [desktop frame](../cache/bug-review/bug06/latest-paused-seek-1536.png) and
  [phone fullscreen frame](../cache/bug-review/bug06/latest-paused-seek-390.png).
- **Unverified.** Browser emulation measured the audio graph and canvas.
  Hardware output and this exact overlap sequence in Firefox and Safari
  remain outside these checks.
- **Next.** BUG-09. Compare cold and warm preview startup, seeking and byte
  demand across the existing format fixtures.


### BUG-09 · Faster video opening and short compatible playback verified

- **Confirmed on the current checkout.** A pending optional poster kept the
  player from mounting. The held-poster regression missed its three-second
  ready threshold on the prior build. The existing two-second video-only WMV
  fixture also produced a manifest but failed with HTTP 422 on the first
  compatible segment. The same FFmpeg command failed to start a thread under
  the worker address-space cap and succeeded with fewer allocator arenas.
- **Implemented.** A stored poster has a 350 ms startup budget, after which
  the player cancels it and can decode its own image. The compatibility
  worker sets a small allocator arena limit, retaining its existing memory,
  CPU, timeout, and output limits.
- **Changed files.** `website/src/features/previews/video/videoPoster.ts`,
  `tgdrive/media/compatibility_worker.py`,
  `tests/browser/{test_browser_progressive_startup.py,test_browser_preview_player.py,test_browser_compatible_playback.py}`
  and `docs/performance.md`.
- **Matched local comparison.** Reopened seven fixtures cold and warm on
  1536×695, with an 850 ms poster delay and the fixed worker for both frontend
  builds. First readable video times fell 424–480 ms cold and 467–475 ms warm
  for the three original videos. The compatible pipeline opens the player
  twice and fell 997 ms cold and 1055 ms warm. Payload bytes at first readable
  content were unchanged across the seven fixtures. The PDF, ZIP and PNG
  timings are individual local observations with no targeted speed change.
- **Focused checks.** Ten Chromium startup, poster, quality-switch, and
  compatible playback cases passed. The 40 MiB progressive clip played
  while only its first 2 MiB was delivered and the poster was still held.
  The short video-only WMV returned a successful segment and painted
  real colored pixels. Ten PDF range and ZIP navigation cases passed.
  The native Docker probe converted the short file within the original
  limits. 59 isolated Docker preview, compatibility, ZIP and progress
  tests passed. 230 frontend preview unit tests, the build, scoped Ruff
  and whitespace checks passed.
- **Evidence.** [Seven-fixture comparison](../cache/bug-review/bug09/preview-comparison.log),
  [progressive startup bytes](../cache/bug-review/bug09/progressive-video.json),
  [Chromium](../cache/bug-review/bug09/startup-final.log),
  [PDF and ZIP](../cache/bug-review/bug09/pdf-zip.log),
  [native Docker conversion](../cache/bug-review/bug09/docker-native.log).
- **Unverified.** The measurements are for local Chromium and sample files.
  Production cold and warm latency, real Telegram delivery, and all codec
  combinations remain unmeasured. The short-video browser probe reported
  a blocked inline style from the compatible player even though the
  existing CSP and the visible fallback controls worked. Retain for
  preview polish review.
- **Next.** BUG-15. Test wide and empty worksheets, long values, and error
  recovery with touch and keyboard.


### BUG-15 · Wide and empty Office previews locally verified

- **Confirmed on the current checkout.** Wide sheets let one long value
  stretch its column, and the cell scroller could not be focused by keyboard.
  The two baseline browser cases failed when looking for the focusable region.
- **Implemented.** Sheet columns have predictable widths, long cell values
  and document headings wrap, and the horizontal scroll region has a keyboard
  focus target. Row numbers remain visible while moving across a sheet. The
  selected worksheet stays identifiable and an empty sheet has its own message.
- **Changed files.** `website/src/features/previews/OfficePreview.{tsx,css}`,
  `tests/browser/test_browser_office_preview.py`,
  `tests/media/test_office_preview.py` and `docs/usage.md`.
- **Checks.** Four local Chromium cases passed at 1536×695, 820×1180,
  and 390×844 in both themes. They covered touch opening, keyboard scroll,
  reaching the twentieth column, selecting the empty sheet with the keyboard,
  and reading the final paragraph of a long DOCX. The existing damaged-file
  test verified a real original download. 64 isolated Docker tests passed
  across Office, EPUB, comics and ZIP. The build, Ruff and whitespace passed.
- **Evidence.** [Chromium](../cache/bug-review/bug15/browser-final.log),
  [laptop](../cache/bug-review/bug15/office-wide-1536.png),
  [tablet](../cache/bug-review/bug15/office-wide-820.png) and
  [phone](../cache/bug-review/bug15/office-wide-390.png).
- **Unverified.** The tests used browser touch emulation for opening files.
  Horizontal swiping and native worksheet menus on physical devices are
  still unverified.
- **Next.** BUG-05. Check ambiguous folder names and recover a pending move
  after its destination disappears.


### BUG-05 · Move paths and missing-destination recovery verified locally

- **Confirmed on the current checkout.** Move already opened the canonical
  three-level source path from My Files, Recent and Starred. Mixed selections
  opened at My Files. A real trash request between choosing a destination
  and confirming Move left the invalid destination selected with **Move Here**
  still enabled.
- **Implemented.** The dialog separates **From** and **Destination**, describes
  selections from different folders and lets the destination path distinguish
  same-named folders. A missing destination replaces confirmation with
  **Choose another folder** and keeps the original selection and conflict
  policy. Keyboard focus goes to My Files in the picker. A failed root
  listing can be retried in place.
- **Changed files.** `website/src/features/files/ActionDialog.{tsx,css}`,
  `tests/browser/test_browser_move_start.py`,
  `tests/support/browser_transfer_app.py` and `docs/usage.md`.
- **Checks.** Nine Chromium cases passed. Three exercised a real trash and
  recovered into the other Planning folder at 1536×695, 820×1180 and
  390×844 in both themes. Both originally selected files reached the correct
  destination. Canonical source, keyboard recovery, a transient folder error,
  a root retry and public exposure confirmation passed. Five MongoDB tests
  passed against a fresh isolated Docker replica set. The build, Ruff and
  whitespace checks passed.
- **Evidence.** [Chromium](../cache/bug-review/bug05/browser-final.log),
  [Docker MongoDB](../cache/bug-review/bug05/docker.log),
  [laptop](../cache/bug-review/bug05/move-recovery-1536.png),
  [tablet](../cache/bug-review/bug05/move-recovery-820.png) and
  [phone](../cache/bug-review/bug05/move-recovery-390.png).
- **Unverified.** The smaller viewports used Chromium with touch support.
  This recovery was not exercised in physical mobile Safari.
- **Next.** BUG-12. Verify an encrypted ZIP round trip, wrong-password
  cleanup, and retry from Transfers.


### BUG-12 · Encrypted ZIP cleanup and retry verified locally

- **Confirmed on the current checkout.** The native AES ZIP round trip
  already worked. The wrong-password attempt published nothing but left a
  numbered partial file in temporary storage. The retry dialog omitted the
  archive name and reported request failures outside the open dialog.
- **Implemented.** Invalid extraction output is removed after the decoder
  exits, and only the downloaded archive remains reserved for retry.
  Archive dialogs use the canonical destination when available and explain
  which parts of a ZIP the password protects. The retry dialog identifies
  the archive, presents request errors inline, and returns focus to the
  password field.
- **Changed files.** `tgdrive/transfers/archives.py`,
  `website/src/features/{files/ArchiveDialog.tsx,transfers/TransferPanel.tsx}`,
  `tests/{transfers/test_archives.py,browser/test_browser_archives.py}` and
  `docs/usage.md`.
- **Checks.** 96 isolated Docker cases passed. The real ZIP worker wrote and
  decoded AES-256 with a Unicode password, nested Unicode directories, an
  empty directory and a 164-character filename. Absent and wrong credentials
  published nothing. The wrong attempt left only the staged source ZIP. The
  correct retry published one complete folder and preserved the source
  bytes. 19 Chromium cases passed, including both destination choices,
  320 px dialog layout, and recovering after a rejected retry request at
  1536×695, 820×1180 and 390×844. 355 frontend unit tests, the build, Ruff,
  Pyright and whitespace checks passed.
- **Evidence.** [Docker](../cache/bug-review/bug12/docker.log),
  [Chromium](../cache/bug-review/bug12/browser-final.log),
  [laptop](../cache/bug-review/bug12/archive-retry-1536.png),
  [tablet](../cache/bug-review/bug12/archive-retry-820.png) and
  [phone](../cache/bug-review/bug12/archive-retry-390.png).
- **Unverified.** Docker used an isolated catalog double and Telegram sender
  fixture. The browser tested job and retry responses with fixtures. End-to-end
  ZIP delivery through a live Telegram channel remains unverified.
- **Next.** BUG-13. Repeat add, change and remove with the real QPDF worker.
  Check incorrect password and failed output writes.


### BUG-13 · PDF password copies and write-failure recovery verified locally

- **Confirmed on the current checkout.** Add, change and remove already used
  separate copies. The wrong-password path gave the same message as an
  output write failure. The action dialog omitted the source filename and
  output kind. At phone width, native select text was clipped and password
  fields could draw two borders.
- **Implemented.** QPDF checks the supplied password before writing and
  distinguishes incorrect credentials from an output-size failure. The
  dialog labels the source, the protected or unlocked copy, and the two
  password fields. Retry shows the specific operation. Shared dialog styles
  restore readable Mantine select and password fields.
- **Changed files.** `tgdrive/transfers/pdf_passwords.py`,
  `website/src/features/{files/PdfPasswordDialog.tsx,transfers/TransferPanel.tsx}`,
  `website/src/{app/style.css,shared/pages/dialogs.css}`,
  `tests/{transfers/test_pdf_passwords.py,browser/test_browser_pdf_features.py}`
  and `docs/usage.md`.
- **Checks.** 97 isolated Docker tests passed with QPDF 12.2. The actual
  round trip preserved text and the source bytes, rejected the old password
  after changing it, and opened the unlocked copy without credentials.
  A native 128-byte output cap left only the original staged PDF, with no
  catalog entry or committed receipt. Incorrect credentials were identified
  separately and retry succeeded. Chromium exercised all three action
  payloads at 1536×695, 820×1180, and 390×844, password retry from Transfers
  and the existing reader unlock flows. ZIP extract and retry layout was
  rechecked after the shared control changes. The build, Ruff, Pyright and
  whitespace checks passed.
- **Evidence.** [Docker](../cache/bug-review/bug13/docker.log),
  [PDF Chromium](../cache/bug-review/bug13/browser-current.log),
  [ZIP Chromium](../cache/bug-review/bug13/browser-zip-current.log),
  [laptop](../cache/bug-review/bug13/pdf-password-copy-1536.png),
  [tablet](../cache/bug-review/bug13/pdf-password-copy-820.png) and
  [phone](../cache/bug-review/bug13/pdf-password-copy-390.png).
- **Unverified.** The native PDF worker used isolated fixtures for the
  catalog and Telegram bytes. No live Telegram delivery or physical-device
  password manager was exercised.
- **Next.** BUG-14. Verify password rotation, expiry, revocation and embed
  access before adding owner-visible Link opens.


### BUG-14 · Protected shares, Link opens and guest unlock verified locally

- **Confirmed on the current checkout.** Existing share protection denied
  wrong credentials and honored rotation, expiry and revocation. The normal
  and embedded viewers could be unlocked. The requested per-link count was
  absent. The Edit share link password field drew a second border and was
  shorter than the label and expiry fields. A cross-site HTTP iframe lost
  the unlock after the server accepted the password and asked again.
- **Implemented.** Shared Links shows the owner a count and tracking start.
  A public entry records an open after it displays. A retry reuses the same
  event ID. The write revalidates scope, expiry and the current unlock.
  Idempotency digests expire after one day and stay out of snapshots. Counts
  live with the link, survive edits and portable restore, and disappear on
  revocation. View traffic does not change the catalog revision.
- **Implemented for FP-62 and FP-63.** Shared dialog fields have consistent
  46 px minimum heights and a single border. The unlock screen uses the
  TGDrive wordmark, a brief password instruction and the existing blue action.
  The iframe version is compact. If the browser cannot keep the unlock,
  it offers **Open in a new tab**. The valid root-folder share now maps to
  the catalog root in the browser.
- **Changed files.** `tgdrive/storage/mongo_store.py`,
  `tgdrive/web/{api_v1,http_guard}.py`, `website/src/app/App.tsx`,
  `website/src/features/shares/{EmbedView,SharedUnlock,SharesView}.tsx`,
  `website/src/features/shares/{SharedUnlock,SharesView}.css`,
  `website/src/features/shares/useShareOpen.ts`,
  `website/src/shared/{api.ts,types.ts,pages/dialogs.css}`,
  `tests/{web/test_share_passwords.py,storage/test_mongodb.py,browser/test_browser_embeds.py}`,
  `docs/{api,usage}.md` and the frontend tracker.
- **Checks.** 38 focused tests passed on a dedicated Docker MongoDB replica
  set. Concurrent events, repeats, older catalogs and protected/expired/
  revoked scope passed. The previously stale authorization stub now invokes
  the same signature as the browser listing. The native encrypted snapshot
  restored two opens and the link capability into a separate empty database
  without copying retry digests. 48 broader Chromium tests passed before the
  targeted follow-up. Seven final Chromium cases passed, including a lost
  HTTP response, repeated entry and zero increments for wrong passwords,
  child navigation and downloads. The editor had equal-height fields at
  1536×695, 820×1180 and 390×844. The 320×260 cross-site HTTP frame offered
  a working top-level path. 25 frontend unit tests, the build, Ruff, Pyright
  and whitespace checks passed.
- **Evidence.** [Docker](../cache/bug-review/bug14/docker-recovered.log),
  [fresh restore](../cache/bug-review/bug14/docker-restore.log),
  [broader Chromium](../cache/bug-review/bug14/browser-final.log),
  [final Chromium](../cache/bug-review/bug14/after/screen-final.log),
  [old editor](../cache/bug-review/bug14/before/share-edit-password-1536.png),
  [new editor](../cache/bug-review/bug14/after/share-edit-password-1536.png),
  [guest laptop](../cache/bug-review/bug14/after/share-unlock-ready-1536.png),
  [guest phone error](../cache/bug-review/bug14/after/share-unlock-error-390.png).
- **Unverified.** Supported HTTPS partitioned unlock cookies passed the API
  harness. Production HTTPS cross-site embeds and physical phones remain
  unverified. An initial wider Docker sweep ended when its temporary MongoDB
  exited. The reported 38-case rerun used a fresh isolated replica set.
- **Next.** Begin the ordered frontend review with shared design and
  accessibility. Include the control text selection in FP-69. Shell and
  Explorer include the owner's newer FP-64 through FP-68 feedback.
