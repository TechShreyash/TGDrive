# TGDrive frontend polish

See the [live frontend audit and ordered implementation plan](frontend-audit.md).

Work on `dev` in the order below. The fixture uses sample files and local
transfer state. [The bug plan](bug-plan.md) records the earlier fixes and the
live integration checks still open.

## Coverage inventory

| Batch | Surfaces and controls to walk | Important states | Status |
| --- | --- | --- | --- |
| 1 · Drive | My Files and nested folders, Recent, Starred, Trash, search, breadcrumbs, sidebar and mobile drawer, grid/list, filters, sort and paging, selection, item actions, New, file/folder pickers and drag/drop | Populated and empty folders, no matches, loading and retry, long names and trails, read-only sample folders, loaded and missing thumbnails, bulk/exposure confirmations | Scoped local walkthrough and regression checks passed. |
| 2 · Transfers and performance | Transfers history and floating receipt, filters/search/paging, cancel/retry/resume/locate, torrent selection, deletion notice, Performance overview and history, cache and memory details, Preloads | Queued, held, running, failed, finished, loading, stale history, empty cache/history, missing metrics | Scoped local walkthrough and regression checks passed. |
| 3 · Preview and sharing | Image/audio/text/video/PDF/book/Office/ZIP and unsupported previews, fullscreen, zoom, chapters, seek and resume, download, Shared Links, create/edit/revoke/copy, public folders and files, embed and password unlock | Locked/authenticated, no thumbnail, loading/unsupported, interrupted seek and retry, revoked/expired links, read-only actions | Scoped local checks passed. Reported live stall unresolved. |
| 4 · Access and administration | Login, setup, logout, Connections and Premium challenge, Settings cache/transfers/regional, bot settings, Storage and Backups, independent protection, restore and recovery-only shell | Invalid input, disconnected, permission-denied, save and failure, lost credentials, empty backups, confirmation and recovery | Scoped local walkthrough and regression checks passed. |
| 5 · Shared consistency | Theme, responsive spacing, page headings, focus and motion, status, retry, help, toasts, empty states, setup and any remaining direct entry points | Light/dark/system, keyboard/pointer/touch, narrow landscape, long labels, dialogs and navigation history | Cross-page and final regression checks passed. |

### Shared components and dialogs

- Shell and style layers live in `app/App.tsx`, `app/style.css`,
  `shared/DesignSystem.tsx`, and `shared/refresh.css`. Shared controls include
  ThemeControl, Modal, Spinner, ErrorNotice, file icons, help, and toasts.
- File dialogs include create/rename/move, upload, URL/torrent import, share,
  download link, trash/restore/delete, preload, ZIP creation/extraction, and PDF
  passwords. Transfers, Shared Links, Connections, and recovery add their own
  confirmation or credential dialogs.
- Owner routes use `view`, `path`, and `section` query parameters. Public
  links use a scoped token, with a separate `/embed` entry point.

## Workflow log

### 1 · Drive shell and file workflows

- **Shared controls and navigation** — Sort and Appearance use UI SDK menus.
  Sort now opens at opposite ends with the two arrow keys, supports typeahead,
  and returns focus on Escape. The sidebar, logo, and breadcrumbs preserve
  native modified clicks. Chromium confirmed Ctrl-click on Recent opens a
  separate tab and leaves the source URL alone.
- **Mobile upload entry** — With active uploads at 740×360, the New menu
  previously started 66 px above the viewport. It opens beside the trigger in
  short landscape and scrolls within the safe area when space is limited.
- **Verification** — The frontend build and four focused menu/theme cases
  passed. Four more cases passed for the landscape New menu, phone navigation,
  and Ctrl-click. Sample-drive captures at 390×844 and 820×1180 light, and
  740×360 and 1440×960 dark have a local
  [visual evidence receipt](../cache/frontend-polish/drive/visual-evidence.json).
  Its integrity check passed. All 823 frontend unit tests passed.
- **Listing and actions** — Seventeen Chromium cases passed for read-only
  public browsing, Unicode names, folder upload preparation, list refresh and
  retry, move/restore confirmation, debounced search, and selection. Twenty-nine
  cases passed for request cancellation, loading without stale rows, deep
  breadcrumbs, Recent, scoped search, rename, move, and touch selection. The
  earlier broad file-view run passed 50 cases. Its 320 px owner-control case
  hit the missing-build page during a concurrent rebuild and passed in the
  dedicated rerun.
- **Pickers and access** — Twenty-two Chromium cases passed for archive actions,
  inline errors, native upload progress/cancel/retry, and guest/owner demo access.
  The demo intentionally shows its guidance dialog for attempted write actions
  and still allows preview and download. Desktop Firefox 157 also passed sort
  typeahead, Escape focus return, and a theme switch. WebKit could not start
  because this WSL host lacks its GTK and GStreamer dependencies. Touch was
  emulated. Physical devices, assistive hardware, and live Telegram/MongoDB
  were outside these Drive checks.
- **Next** — Review Transfers, Performance, and Preloads.

### 2 · Transfers, Performance, and Preloads

- **Transfers** — The page reserved a separate row for Cancel All, pushing the
  first progress bar under the phone tab bar in landscape. The action now sits
  beside the first work group. The same 740×360 sample view shows the full
  first transfer and progress bar. The action keeps its confirmation and
  returns focus on Escape. The floating receipt retains its existing controls.
- **Performance and Preloads** — Walked overview, account and history controls,
  cache capacity and entries, and eviction controls. The 390×844, 740×360,
  820×1180 and 1440×960 Chromium sample captures reported zero viewport
  overflow in the scanned workspace controls. Captures and the accessibility
  snapshots are in `cache/frontend-polish/transfers/`.
- **Verification** — Forty-three Chromium cases passed for transfer paging and
  torrent selection, cache and preload controls, missing metrics, and refresh.
  Ten more passed for history range and stale requests, transfer recovery,
  refresh, and scoped eviction. The build and the seven-case rerun for
  history and recovery passed after repositioning Cancel All. Real Telegram
  throughput and production cache behavior remain outside the fixture.
- **Next** — Check previews, readers and sharing on the same viewport matrix.

### 3 · Previews, readers, and sharing

- **Video compatibility** — The owner reported buffering and unplayable
  videos. Seven locally generated samples advanced playback and painted
  decoded frames from original bytes in Chromium. These were H.264/AAC MP4,
  HEVC/AAC MP4, VP9/Opus WebM, H.264/AAC MKV, MPEG-4/MP3 AVI, MJPEG/PCM MOV,
  and H.264/AAC M4V. Results are in
  `cache/frontend-polish/video-formats.json`.
- **Playback checks** — Forty-two Chromium cases passed for progressive
  playback while delivery is held, sparse frames, seek and retry, paused
  pictures, and compatible WMV, ProRes MOV and MPEG-2 TS playback. The TS test
  triggers fallback deliberately because the original decoder can play it.
  Twenty-four media and share-password backend tests also passed. A
  network-isolated Docker smoke test decoded synthetic WMV and encoded
  H.264/AAC using the cached TGDrive application's FFmpeg. The owner later
  supplied a live Chrome example. The [BUG-03 observation](bugs.md) confirms
  a stall around 04:30 without identifying the cause.
- **Readers and sharing** — Fifty-nine Chromium cases passed for PDF ranges
  and resume, EPUB/comics, Office, ZIP and public embeds before the focused
  UI changes below. Shared Links had no scanned horizontal overflow at phone,
  landscape phone, tablet and desktop widths.
- **UI changes** — Download File now precedes metadata on an unsupported
  preview, where it previously fell below the fold in short landscape.
  Password unlock errors now describe the field, return focus to it, and
  clear when the guest starts correcting the password. The unlock form
  has phone gutters and a larger input and submit target. The password attempt
  and permission checks still run on the server.
- **Focused verification** — Seventeen Chromium cases passed for fullscreen,
  preview recovery, original download, and Shared Links management and retry.
  Fourteen passed for embeds and phone previews, including the fully visible
  download action for a long name at 740×360. The protected share regression
  passed again after resizing the unlock form. The 24 sample preview captures
  are in `cache/frontend-polish/previews/`. These fixtures do not measure a
  live Telegram wait, the original reported stall, or physical device codecs.
- **Next** — Walk login, setup, Connections, Settings, and recovery.

### 4 · Access, administration, and recovery

- **Login** — A rejected owner password formerly left focus outside the form.
  The field now regains focus, describes the error, and clears it on correction.
  Short-landscape spacing keeps Log in visible before and after a rejection.
  Both viewport regressions passed. Screenshots are in
  `cache/browser-e2e/login-retry-*.png`.
- **Administration and recovery** — Audited Connections, the three Settings
  tabs, the nested bot controls, Storage, and the recovery-only shell.
  Twenty sample admin views at four widths had no scanned control overflow.
  Screenshots and accessibility snapshots are in `cache/frontend-polish/admin/`.
  Installation is documented in [setup](../docs/setup.md). The in-app
  connection and backup setup and recovery paths use isolated fixtures.
- **Verification** — Thirty-eight Chromium cases passed for backup setup,
  recovery, scoped settings, and bot folder selection. Four passed for
  connection and Premium flows and session expiry. Eight passed in the
  final login, logout, and demo-permissions rerun. A live Telegram Premium
  challenge, backup/restore, and production MongoDB were outside these
  fixtures.
- **Next** — Check common keyboard navigation, page structure, responsive
  spacing, notifications and final evidence together.

### 5 · Cross-page consistency

- **Navigation and feedback** — Renamed the skip link to match its main
  content target across Drive and management pages. Keyboard checks reached
  My Files and Storage and Backups directly. The Transfers page keeps a
  screen-reader total alongside its compact groups.
- **Cross-page checks** — Thirty-seven Chromium cases passed for mobile
  navigation, fixed directory controls, notifications, thumbnail rebuilds,
  help links, loading and empty states, refresh errors, and theme persistence.
  Twelve passed for picture-in-picture, video gestures, chapters and keyboard
  player settings. Four passed on the final build for transfer navigation
  and confirmation, protected embeds, and owner login retries. Desktop
  Firefox passed sort typeahead, Escape focus return, and switching to dark
  again. The final frontend unit suite passed all 823 tests across 63 files.
  The production build, scoped Prettier and Ruff, and whitespace checks passed.
  Vite still reports a large-chunk warning and a warning in Movi Player's
  CommonJS import.
- **Evidence** — Sample fixtures cover 320–390 px portrait phones, 740×360
  landscape, 820×1180 tablet, and 1440×960 desktop in Chromium. The workflow
  logs give the themes, input modes and tested states. Browser tests and
  screenshots used the offline fixture and left the existing installation's
  database and Telegram storage untouched. The
  [final visual evidence receipt](../cache/frontend-polish/visual-evidence.json)
  links the screenshots to the source hashes. The integrity check passed.
- **Open verification** — WebKit needs GTK and GStreamer libraries on this
  host. Physical devices, screen readers, hosted performance, production
  Telegram/MongoDB and the reported intermittent video stall remain unverified.
  Recheck the stall with an affected codec, browser and playback position
  when a repeatable example is available.
- **Delivery** — All changes and the tracker are local on `dev`.

## Follow-up visual and interaction pass

The local demo was reviewed again across Drive, monitoring, sharing, previews,
access, and management. The pass used the existing visual system as its baseline
and covered both portrait and short-landscape phone layouts.

| Surface | Change | Evidence |
| --- | --- | --- |
| File browsing | Short touch landscape uses denser file tiles and matching skeletons. A row menu closes when its trigger scrolls behind the file toolbar. | At 740×360, Recent shows three file columns and thumbnail height drops from 192 px to 76 px. The desktop scroll regression passed. |
| Short-landscape shell | Routine upload progress outside My Files goes to the existing Transfers count. Upload issues get priority in the status strip. | Recent, Connections, Performance, Preloads and Shared Links showed 237 px of workspace at 740×360, up from 175 px. |
| Performance and Preloads | Tabs and Refresh share a phone toolbar row. Narrow phone metrics keep common rates together. Nonzero cache use below 1% has a distinct visual and accessible reading. | At 320 px, the toolbar went from 96 px to 44 px. The sample disk cache shows less than 1% and the empty memory cache shows 0%. |
| Shared Links | Opening a shared item's folder shows progress, ignores duplicate activation, and stops the lookup when the user leaves the page. | The held-response browser case showed the pending state and issued one lookup after two activations. |
| Embedded folders | Small frames keep root, parent and current location on one line. Keyboard folder navigation moves focus to the destination breadcrumb. | The 320×210 header stayed 57 px tall after four nested opens. All 12 embed cases passed, including the wide trail and existing access controls. |

The production build, all 823 frontend tests, focused Chromium interaction
checks, Ruff and whitespace checks passed. The Chromium demo walkthrough
captured 25 views across five viewport sizes with light and dark themes and
checked for horizontal page overflow. [Follow-up visual evidence](../cache/frontend-polish-pass2/visual-evidence.json)

The local demo serves this build at [localhost](http://127.0.0.1:18769/).
Physical devices and production storage were outside this pass.

## Larger feature ideas

- **Copy playback diagnostics** — A person who hits an intermittent stall
  could copy the codec, player mode, position, and last delivery status
  without including the filename, share token, or file contents.

## Owner feedback and active follow-up

This register has 80 frontend feedback IDs, FP-01 through FP-80. The
[bug execution plan](improvement-plan.md#ordered-bug-work) tracks 14 BUG IDs.
Some work, including Link opens and Automatic tuning, appears in both. The
[current dev review](#current-dev-review-pass) records the previous workflow review.
The [77-entry demo audit](#fp-01-through-fp-77-current-demo-audit) records
individual evidence. See [FP-78](#fp-78-five-cards-per-row) for grid density and
[FP-79 and FP-80](#fp-79-and-fp-80-file-focus-and-selection) for file focus and selection.

| ID | Owner report | Work and status |
| --- | --- | --- |
| FP-01 | Make sure TGDrive works well on phones. | Chromium and isolated WPE WebKit fixture checks covered compact navigation, selection, menus, prompts, Settings, sharing, and readers. WebKit emulated iPhone SE and iPhone 13 landscape. Physical phone verification remains open. |
| FP-02 | Rebuild Performance around useful information for an owner. | Implemented Overview, Server resources, Tuning, and Accounts. Overview leads with transfer issues and service waits, then rates and history. Server resources covers disk, memory, cache, and temporary upload storage. Tuning shows applied rules and diagnostic totals, and Accounts shows Telegram queues and cooldowns. Drive totals appear under Drive contents in Storage and Backups. Chromium verified single-row tabs and Refresh at 320 px, tablet stacking, genuine zero versus unavailable metrics, and refresh ordering. |
| FP-03 | Colors and design feel inconsistent across the website. | Blue remains primary. Neutral surfaces, select backgrounds, help overlays, borders, and active states now use shared light/dark tokens. Checked light and dark captures across drive, owner tools, and previews. The dark Cache settings select uses its shared field color. |
| FP-04 | The info button is misaligned and the previous audit missed detail-level issues. | Fixed the Encrypt bot uploads help alignment shown in the screenshot. The help and switch centers matched to 0.0 CSS px at four sampled widths. The popup now scrolls within the measured available space at 740×360, with 8 px of viewport clearance. Desktop and phone checks cover theming, disclosure, and Escape focus return. |
| FP-05 | Redesign Storage and Backups completely. | Confirmed backup and recovery indicators lead the page. Missing evidence says “Not confirmed yet.” The controls for backup, readiness, and independent protection use their existing outcomes. Chromium exercised confirmations and failure states. WebKit and Chromium showed the surface in portrait, landscape, and desktop. Live protective storage remains outside this fixture. |
| FP-06 | Redesign previews for all supported file types to make them intuitive and useful. | A common preview frame keeps close, fullscreen where applicable, and original download predictable. Chromium fixture coverage includes image, audio, text, video, PDF, EPUB, CBZ, CBR, Office, ZIP, unknown formats, and public embeds. The live BUG-03 stall remains open. |
| FP-07 | Bring Cache settings, Transfer limits, and Timezone into a single redesigned Settings page. | One sidebar destination and three linked sections. Direct sections, browser Back, preserved drafts, scoped saves, conflicts, server errors, and unsupported section fallback passed in Chromium. |
| FP-08 | Redesign the sidebar for intuitive navigation, with direct tools/settings links and a clearer active destination. | Files and Activity keep direct links and the active destination styling. The owner later requested collapsing Tools and settings again. See FP-68 for the current behavior. |
| FP-09 | Improve the file explorer's usability, List layout, and folder and file cards. | List starts a fresh session and saved List/Grid preference survives. List columns align name, added date, size, and actions. Folders and files use aligned card footers. Desktop filter buttons become a native Type picker at narrow widths. Grouping preserves item count, selection, sort, layout, breadcrumbs, search, paging, and file actions. Starred and shared labels are announced once. |
| FP-10 | Improve the login screen. | The wordmark, owner-password form, error feedback, and visibility toggle follow the theme. Chromium and WebKit checked retry, focus, and reachable controls down to 320 px portrait and 568×320 landscape. |
| FP-11 | Root scrolling can shift the sidebar or clip the logo. | The header stays pinned while the sidebar contents scroll independently. At 1280×420, Chromium verified that reducing the available browser height after following a deep link reveals the active destination without moving keyboard focus. See FP-21 for wheel and touch scrolling. |
| FP-12 | A mobile file checkbox hit area can accidentally reach the bottom navigation. | The reported navigation hit did not reproduce in sampled layouts. Checkboxes had 44×44 px targets and at least 109 px of bottom-navigation clearance in the selection probes. Touch on the label padding selected the row. A separate 320 px New-versus-action collision was reproduced and corrected by moving New to the header. The original action center now opens its own menu with the upload banner present. |
| FP-13 | Make file activation consistent between desktop, touch, and public folders. | Desktop click selects an owner row or focuses a public row. Desktop double-click and Enter open. Touch tap opens, hold selects, and release, movement, or scroll do not cause false opens. Chromium used real emulated touch sequences. WebKit used real emulated taps and synthetic pointer hold/cancellation, with physical holds open. |
| FP-14 | Long filenames are limited in the file explorer. | Rows and cards keep action placement and expose the full filename, including in phone actions. A 110-character example wrapped fully in List and Grid in WebKit. Existing stored-name validation still allows up to 255 characters. |
| FP-15 | Settings and Connections validation can be hard to find or stay visible after correction. | Timezone validation is next to the field above the transfer dock, with focus and clearing on correction. Live field edits clear stale required feedback in the Connections SDK forms. Scoped settings, 422 errors, conflict handling, and connection retry passed on fixtures. |
| FP-16 | Settings and Connections disclosures and dialog focus need clearer feedback. | Chromium and WebKit checked chevrons, visible focus and both Tab and Shift+Tab in management dialogs. Existing disclosure semantics were reused. |
| FP-17 | Transfers and history need clearer rows, especially long names and the 320 px filter row. | Progress, names, rates and state have clearer grouping. Clear history gives every filter room at 320 px. Checks covered keyboard filters, safe confirmation, pagination hit target, layout dock, long names, and retained history when returning. Short-landscape routine uploads appear in the navigation count and actionable failures keep their banner. |
| FP-18 | Improve Shared Links, expiry controls, and public views. | No expiry has a 44 px target. Unavailable copy gets a focusable explanation and preserves disabled semantics. Fixture suites covered create, edit, expiry, copy, revoke, late refresh ordering, and public read-only access. The owner separately verified a copied folder in a fresh anonymous session. Embedded breadcrumbs can change folders during a held response without stale content replacing the new folder. |
| FP-19 | Improve PDF and reader controls, including the extra toolbar height on phones. | The UI SDK options popup fits at 320 px, Escape closes it before the viewer, focus returns, and zoom is announced. Page jump rejects malformed values and tracks the final page even at the end of the scroller. The PDF toolbar went from 113 to 101 px at 320×568 and from 113 to 60 px at 568×320. Chromium and WebKit passed fullscreen, loading, progress, and focus checks. |
| FP-20 | Check accessibility and responsive behavior across the app. | Browser checks cover keyboard input, focus return and containment, control names and descriptions, 44 px targets where needed, status contrast, wrapping, mobile notifications, and loading feedback. The browser walkthrough captured phone portrait and landscape, tablet, and two desktop heights. Physical screen readers and full device coverage remain open. |
| FP-21 | The owner reported that sidebar scrolling feels bad on their everyday laptop. | Navigation and support links share one native scroller. The brand and New stay visible on desktop, and the header and Close stay visible in the phone drawer. Wheel checks covered 1366×768, 1536×760, and 1920×870. A Windows Chrome check at a 1536×695 viewport verified that the wheel also works over Documentation, with the logo and main page stationary. Emulated swipes verified the phone drawer at 320 px portrait and 740×360 landscape. |
| FP-22 | Premium code retry loses keyboard focus. | On a 320 px phone fixture, a wrong code or lost response re-enabled the field while focus stayed on the page body. The field now receives focus after it becomes usable. Keyboard retry passed in Chromium and isolated WebKit. |
| FP-23 | File actions can cover the next focused row. | The open disclosure closes when Tab or Shift+Tab moves focus outside it. Escape returns focus to its trigger. Copy Download Link and Preload file now expose the same native button roles as the other actions. Ten focused Chromium checks passed. |
| FP-24 | Automatic preloading and bot encryption switch labels have low contrast. | The checked light bot switch measured 3.55:1 and the checked dark cache switch measured 1.96:1. Both switches now use the shared theme tokens. Sixteen sampled Chromium states had at least 5.77:1 contrast and 44 px targets. |
| FP-25 | Performance Storage readings use invalid definition-list markup. | Explanatory notes are now part of each reading's definition, with the visible wording and numbers preserved. Five Chromium Performance layout and measurement cases passed. |
| FP-26 | A long phone notification can cover the New menu. | The direct cause is the older phone rule that puts the notification at the top while the menu is open. The notification now makes room for the menu and returns if the user presses Escape. A popup that opens under the mouse also pauses its timer on the first mouse movement. Seven Chromium notification cases passed. |
| FP-27 | Toggling selection can move file rows. | Select mode changed the heading width and moved files by 32–34 px at 320, 360, and 1440 px. The new layout preserves the title and control space. Fourteen Chromium viewport checks kept List/Grid coordinates within one pixel. Seven focused Chromium cases and six WPE WebKit selection cases passed, including touch menus. |
| FP-28 | Some share and preview labels are attached to roles that cannot be named. | Share expiry keeps a native time role with a full timestamp description. Focusable text and enhanced video previews expose a named region and group, respectively. Chromium confirmed accessible names and retained reader and player controls. |
| FP-29 | A phone keyboard can hide the focused Timezone field. | When the visible viewport shrinks, Settings keeps the focused field in view and leaves room to scroll to Save. The old frozen build failed both keyboard resize simulations. The fix passed both in Chromium, and emulated touch scrolling reached Save at 320×300 and 390×390. Physical keyboards remain unverified. |
| FP-30 | The desktop interface feels oversized. Main navigation and complete file rows are cut off in the owner’s Grid screenshot. | Use the owner’s screenshot and the measured 1536×695 browser viewport as the baseline. Tighten the desktop sidebar, workspace edges, filter spacing, and Grid previews while preserving readable type and touch targets. The local demo exposes every main navigation link and shows three complete Grid rows at 1536×695 and 1366×768. The six-column Grid at 1536 px and the phone touch targets are preserved. |
| FP-31 | Use Google Drive as inspiration and keep TGDrive’s design original. | Treat all product references as inspiration. Use compact, distinct folder tiles and readable file grouping in TGDrive’s own blue theme. Keep TGDrive’s direct owner navigation and media-first Grid cards. Use List for file metadata. |
| FP-32 | At 200% browser zoom, Preloads clips Clear Cache and page titles can break mid-word. | The cache toolbar wraps. Medium-width headings reserve room for the title, and file controls use their own row when needed. The failed 785 px cache regression now passes. |
| FP-33 | At 400% zoom with active uploads, the routine upload banner leaves too little room for files. | Compact views use the existing Transfers count when the available height is 320 CSS px or less. Uploads that need attention still show a status button. Actual Chromium zoom and the 392×197 regression cover file access and Transfers navigation. |
| FP-34 | Public folder embeds lose keyboard focus when Show more loads. | Keep focus available during loading and retry. Keyboard activation focuses the first newly added item when it arrives. Intentional focus movement takes precedence, and pointer pagination preserves the scroll position. |
| FP-35 | The compact desktop sidebar made the GitHub shortcut 36 px tall. | Restore the 44 px GitHub and Documentation targets, with their edges aligned within the compact sidebar. The existing desktop and phone sidebar tests cover the project link. |
| FP-36 | Selected files and active controls can be hard to distinguish in browser high-contrast mode. | Use system-color underlines and outlines for selected rows, active navigation and tabs, view toggles, and the selected filters. Preserve the independent keyboard focus outline. Two Chromium palette regressions and 11 captures passed. |
| FP-37 | Grid cards show unnecessary sizes and dates, and the previews look flattened. | Use 16:9 previews and put the name and actions together under the preview. Grid omits size and date. List retains both fields. Folder tiles keep their type and status indicators. |
| FP-38 | Shared Links could show a count of views. | Implemented **Link opens** in BUG-14. Counts are owner-only and survive catalog restore. Chromium verified public entries, retry deduplication, repeat visits, and zero increments for failed unlocks and navigation. See [bug-plan.md](bug-plan.md). |
| FP-39 | Performance mixes server resources with drive inventory, and repeats hard-to-read details. | Put file, folder, and trash totals under Storage and Backups. Use Server resources for host RAM, disk, cache and staging. Simplify Overview and keep detailed diagnostics within the section they describe. |
| FP-40 | Settings looks good and the rest of TGDrive should feel consistent with it. | Use Settings' grouped sections, direct labels, and control spacing as the comparison. Keep shared blue theme tokens and verify both themes, desktop and phone across the site. |
| FP-41 | The List and Grid icons look too similar in the dark toolbar. It is unclear which is active and where to click. | Label both desktop options and give the active option a blue fill with a 44 px target. In the compact menu, show the current view icon and a chevron. Verify one-click switching, keyboard selection, and the active label’s contrast in both themes. |
| FP-42 | Escape can miss a help popup while the trigger still has focus. | Keep the existing popover and handle Escape on its trigger as well as inside the dialog. The desktop regression exercises both focus positions, then opens it again at phone width. |
| FP-43 | The desktop List/Grid switch is too large and visually heavy. | Implemented and verified first. Each desktop option is 32 px high with a subtle blue fill and underline. Touch layouts retain 44 px targets. Nine focused Chromium cases passed. |
| FP-44 | Performance has poor sizing, alignment, and spacing. The overview chart is cluttered and hard to scan. | Implemented and verified after FP-43. Network and CPU share a top edge on wide layouts, RAM sits under CPU, and all charts stack at narrow widths. Current network labels double as the legend. Lines keep measured gaps and use markers for isolated readings only. |
| FP-45 | Remove unnecessary expandable blocks in Performance, including All-time totals in Tuning. | Implemented and verified after FP-44. Totals, applied limits, memory and cache details, and filesystem readings appear directly in their sections. Chart scopes use the existing info buttons. |
| FP-46 | Bringing a chart into view can shift the page and clip the navigation. | Reproduced at 1536×695 and 390×844. Each slider now has a direct accessible name. The workspace scrolls while the document stays fixed. Chromium regressions failed on the old build and passed with the new labels. Firefox and WebKit also kept navigation in place. |
| FP-47 | A short Grid filename wraps at 320 px and competes for space with its action. | The filename gets more room in narrow cards and the menu retains its 44 px target. Chromium verified that the short name fits, the long name wraps, and the menu opens on tap. |
| FP-48 | The file grid shifts down when selection starts on a 1440 px touch layout. | Use the existing compact bulk actions before the toolbar would wrap. Selecting no longer moves the test file. The List/Grid switch and accessible action names remain available. |
| FP-49 | Performance text and other elements feel too large. | Reduced the title, network rates, resource values, progress bars, cards, and table padding. Resource explanations now use 13 px regular text. Phone tabs and Refresh keep their 44 px targets. |
| FP-50 | The phone file browser feels crowded and hard to use. The Type popup obscures the grid. | Use a compact title and a themed **All types** picker with a visible selection and 44 px options. Outside this browser's own uploads, show background work in the Transfers count. Verified filtering and viewport bounds in Chromium, Firefox, and WebKit. |
| FP-51 | Improve Connections as well. | Moved Refresh beside the health summary, kept the channel ID in one place, and put detailed account guidance in the existing help control. Phone rows show the full status and Manage together. The first client and action fit the initial 320×640 screen. |
| FP-52 | Shared Links and the Performance Accounts tab can extend the document on phones. | Anchor the invisible share descriptions and account table headers inside their own panels. The extra scroll range fell from 447 px to zero on Shared Links at 320×640 and from 40 px to zero on Accounts at 740×360. |
| FP-53 | Keep preview recovery usable on a phone or in a short landscape viewport. | Implemented and verified on the isolated demo. Preview errors put Retry or Reload above scrollable details. Unsupported previews keep Download within reach. Nine focused tests passed. |
| FP-54 | Make Performance Overview the next acceptance checkpoint. Surface attention, evidence of current waits, and the next action. Use Settings for density, typography, and spacing. | Implemented and browser-verified. The local demo is ready for visual review. Compare the same dark-theme snapshot at 1536×695 and 390×844 before accepting the checkpoint. |
| FP-55 | Consider a TGDrive stats panel in Performance. | Implemented a TGDrive library summary below the Overview charts with files, folders, and stored bytes from the existing snapshot. Storage and Backups keeps the full inventory and Trash figures. |
| FP-56 | Remove chart sliders and the duplicate readings underneath. | Implemented one on-demand sample overlay per chart. Verified hover, tap, keyboard stepping, Escape, and access to the selected reading through assistive text. |
| FP-57 | Explain what “backing storage” means. | “Verified free disk space” uses both the filesystem and configured disk check. The page labels the optional host check and reports filesystem readings separately. Tested with and without a verified reading. |
| FP-58 | Replace the Performance refresh button with automatic updates every five or ten seconds. | Live readings poll every five seconds while visible. Upload space polls on Server resources. Rolling history polls every minute and a successful custom range stays fixed. Tested failed requests, recovery and an outstanding request across several poll intervals. |
| FP-59 | Remove raw measurement names such as proc_meminfo and host from owner-facing labels. | The resource rows explain whether automatic sizing uses Linux memory or a container limit and what CPU capacity refers to. Verified the Linux fallback without a container hard limit. |
| FP-60 | Redesign the whole Performance page. Identical cards, labels, values, headings and descriptions obscure the reading order, particularly in Server resources and Temporary upload storage. | Implemented across all four tabs. Transfer issues and next actions lead Overview. Capacity meters, upload reservations and diagnostics have distinct layouts. Temporary upload storage combines missing disk readings into one explanatory row. Tuning groups settings links with the tuning report. Accounts leads with account health followed by capacity and queue data. Awaiting visual review. |
| FP-61 | The local demo shows an empty Automatic tuning section. Show what TGDrive adjusted and why. | Implemented in BUG-08. The showcase runs the production sizing policy with labeled sample resources. Verified the populated, missing, stale and recovered reports in Chromium at 1536×695 and 390×844. Numbers fit on one line and explanations read below their own values. The live report keeps the last applied result if an update fails. See the BUG-08 evidence in [bug-plan.md](bug-plan.md). |
| FP-62 | The optional Share password field appears cropped in Edit share link. | Fixed the shared double border and aligned the password, label and expiry controls. Chromium confirmed the field height, reveal keyboard action and actual save at 1536×695, 820×1180 and 390×844. [Before](../cache/bug-review/bug14/before/share-edit-password-1536.png) · [After](../cache/bug-review/bug14/after/share-edit-password-1536.png). |
| FP-63 | Improve the guest password-protected link screen. | Added TGDrive branding, password guidance and a readable blue action. Focus returns on an incorrect password. A 320×260 cross-site HTTP iframe falls back to a new tab when its unlock cannot persist. Verified Chromium on laptop, tablet and phone. [Laptop](../cache/bug-review/bug14/after/share-unlock-ready-1536.png) · [Phone](../cache/bug-review/bug14/after/share-unlock-error-390.png). |
| FP-64 | Right-click a file or folder to open its options, as in the reference. | Reuse TGDrive's existing action menu and pin it to the pointer. Check List, Grid, keyboard context-menu keys, viewport edges and public read-only permissions. |
| FP-65 | The Google Drive reference looks clean and polished. | Use clear selection, concise action grouping and predictable placement as references. TGDrive keeps its own typography, blue palette and components. The Explorer pass verifies how the menu, toolbar and underlying grid read together. |
| FP-66 | Six Grid cards per row crowd filenames and make thumbnails hard to see. | Give file cards a larger minimum width while keeping 16:9 thumbnails. The earlier pass used four columns at 1536 px and five on wider displays. FP-78 sets five at 1536 px too. Keep full filenames available and preserve List. |
| FP-67 | The List and Grid switch looks awkward and its text feels wrong. | Keep both modes directly reachable. Simplify the selected treatment, tune label weight and align the icons with the text. Confirm touch and keyboard use, including contrast and focus in both themes. |
| FP-68 | Collapse Tools and settings again. Its destinations do not need to be visible during everyday file browsing. | Implemented a native-button disclosure in the existing sidebar. It starts collapsed during everyday file browsing and opens on direct tool navigation. The collapsed control identifies the current tool when applicable. Keyboard, an emulated phone tap and the sidebar scroll all passed. |
| FP-69 | Clicking website controls selects their text. File content should still allow selection. | Implemented shared selection rules for buttons, menus and preview controls. Verified the zoom trigger and Fit width at 1536×695, 820×1180 and 390×844. Mouse selection still works in the PDF text layer, DOCX and text preview, and Ctrl+A still selects the search field contents. See the shared review pass below. |
| FP-70 | Revisit the design of the Shared Links page later. | Review the management list, protection and expiry labels, Link opens, action spacing and editing together. Keep owner controls distinct from the read-only guest surface. |
| FP-71 | Some PDFs reopen at an earlier page than the last page read. Recheck saved progress for PDFs and videos. | Reproduce a late PDF page update through close, reopen and reload. Verify which save wins if requests overlap. Check video progress and resume from the last observed position separately. |
| FP-72 | Remove the oversized video resume popup that offers to continue from the start. | Resume at the saved position using the existing playback intent. Keep seeking back available through the normal controls and verify no resume popup appears. |
| FP-73 | Switching Explorer sorting from Name to Date Added shifts the whole toolbar and pushes List/Grid down. | Keep the toolbar geometry stable across Name, Date Added and Size, including at widths where controls need their responsive layout. Check focus and overflow in the running demo. |
| FP-74 | Preview topbars look too large and visually heavy across all previews, including video and PDF. Improve the look and usability of every preview panel. | Tighten the shared preview header and reduce competing surface fills. Keep the filename and relevant actions legible, keep touch targets, and verify rendered headers, first content and the relevant actions for each preview family at laptop and phone widths. |
| FP-75 | The backup page and sidebar say Storage and Backups, the long label overflows, and drive totals are duplicated or buried. | Use Backups for recovery and backup operations. Put a clearly labeled TGDrive library panel above the charts on Performance Overview. Keep library size separate from server resources and retain Trash access and existing backup links. |
| FP-76 | Sidebar selection and hover boxes look joined, and the collapsed tools group leaves room for slightly larger rows. | Give destinations distinct vertical spacing and a slightly taller desktop row. Keep touch targets and access to the expanded tools and footer. |
| FP-77 | The network chart ends above the stacked CPU and RAM cards, leaving unused space. | Align the chart cards at the top and bottom on wide screens and let the network plot use the added height. Keep the stacked layout and chart interaction at narrower widths. |
| FP-78 | Four cards per row feel too large on the laptop. Five is the preferred density. | Implemented and verified in the running demo at 1536 and 1920 px. See [layout evidence](#fp-78-five-cards-per-row) for smaller widths and the browser checks. |
| FP-79 | Tabbing to a Grid filename looks like selecting text, and the card or preview moves. | Implemented and demo-verified. The focused Grid card has one outline and keeps its dimensions and thumbnail steady. |
| FP-80 | Select files more easily with Shift, Ctrl, and the mouse. | Implemented and demo-verified. Shift-click selects a range in page order. Ctrl-click or Command-click toggles an item. The bulk toolbar shows the selection. Touch and checkbox selection remain available. |

The owner approved changes to the visual theme with blue as the primary color.
Explorer review includes toolbar grouping, starred and shared indicators,
duplicate accessible metadata, and breadcrumb, search, sort, paging, and
file action behavior.

### Design decisions and reasons

| Suggestion | Decision | Reason |
| --- | --- | --- |
| One Settings page with category tabs | Use link-based section navigation styled like tabs, with Cache, Transfer limits, and Timezone. | Direct URLs, Back, and opening another tab retain their browser meaning. The same editor keeps category drafts and scoped saves. |
| A responsive Type filter | Keep visible filter buttons where they fit and use a labeled UI SDK picker on narrow phones. | The initial native popup covered the grid. The styled list keeps the options within the viewport and marks the current choice. See FP-50. |
| Long-press selection on touch | Keep the visible Select control alongside a hold shortcut, with motion and scroll cancellation. | The control makes selection discoverable and works with keyboard and assistive input. Cancellation protects normal scrolling. |
| Keep New easy to reach on phones | Use a 44 px header button with the menu anchored beneath it. | The floating button covered a visible row action in the 320 px demo. The header position leaves actions tappable in both List and Grid. |
| Make sidebar scrolling feel natural | Give navigation and support links one scroll area that uses the available height. | In the old sidebar the wheel stopped responding over the footer at ordinary laptop sizes. The single surface also keeps Close pinned while a phone drawer scrolls. |
| Fit more of TGDrive into an everyday laptop viewport | Use less desktop spacing and shorter Grid thumbnails. | The owner’s screenshot showed a partially cut-off Storage and Backups link and two full file rows. Less spacing and more compact previews make room for the full navigation and another file row. Browser, OS, and text scaling remain user controls. |
| Learn from another product’s explorer | Apply useful browsing patterns to TGDrive’s own workflows and visual language. | The supplied Google Drive screenshot suggested short folder tiles. TGDrive uses a type-colored folder icon, the available date, visible keyboard focus and its own menu. Original designs are a standing requirement. |
| Migrate PDF dialog and its options to UI SDK | Keep the shared native preview dialog and use the SDK Popover and Input inside it. | The native dialog already supplies modal focus and works with reader/player fullscreen. PDF page entry needs its own strict commit and cancel handling. |
| Extend long filename support | Expose the full existing name in browser controls and preserve the 255-character storage limit. | The reported explorer problem was a display and action-layout issue. Longer paths affect operating-system downloads and would need a separate requirement. |
| Reinvent the entire theme | Improve the shared neutral and text tokens while keeping blue primary and semantic status colors. | Shared tokens address the inconsistency across routes and give menus, fields, and cards a stable visual language in both themes. |

### Integrated browser verification

- The [local demo](http://127.0.0.1:18769/) serves the staged production build
  with isolated fixture data. The Chromium walkthrough covers Login, the drawer,
  all owner views, the three Settings sections, the four Performance tabs,
  a populated explorer in both layouts, and six loaded preview types. Public
  captures include the anonymous folder and EPUB, CBZ, and CBR readers. The
  [layout results](../cache/frontend-redesign/captures/layout.json) record 206
  captures across eight layouts. The
  [visual evidence receipt](../cache/frontend-redesign/visual-evidence.json)
  links the captures to the source hashes.
- The final scoped UI run passed 46 Chromium cases for Performance, sidebar,
  demo access, and Settings validation. A later combined check passed 25 cases
  for sidebar reveal, appearance, workspace behavior, and bot help. The
  [89-case drive and transfer suite](../cache/frontend-redesign/drive-transfers.log)
  covered catalog changes, uploads, import, archives, preloads, and pagination.
  The broader drive/owner and preview runs had failures from older assumptions
  and assets replaced across the runs. Their focused reruns confirmed the new
  theme tokens, equal card heights, toolbar geometry, affected PDF controls,
  and playback hold and recovery. The playback hold regression passed three
  times with real decoded frames withheld and released in the fixture.
- The browser walkthrough exposed an offscreen active destination at 420 px
  desktop height, a phone row action under New, and help text clipped in
  short landscape. Each was reproduced before the fix and checked again
  after. Follow-up wheel tests found that the old footer swallowed scrolling at
  1366×768, 1536×760, and 1920×870. The new sidebar moved continuously through
  the same footer positions and kept the header and page stable.
  [Before](../cache/frontend-polish-explorer-audit/sidebar-before-1366x768-dark.png) ·
  [After](../cache/frontend-polish-explorer-audit/sidebar-after-1366x768-dark.png)
  The short-landscape file view also gains 62 px with routine uploads in the
  Transfers count.
- Synthetic WebKit phone checks cover taps, navigation, Settings, sharing,
  and readers. An isolated GStreamer plugin set restored the WPE video
  verification. Native H.264/AAC MP4 played and paused-seeked. TGDrive's
  enhanced MKV played and painted paused seeks at 390×844 and 740×360, and the
  enhanced MP4 and software WMV paused-seek checks passed. Chromium also checked
  compatible playback. The production build, 823 frontend unit tests, scoped
  formatting and whitespace checks passed. An isolated Docker container
  resolved every static reference in the built entry point.
- Physical phones, screen readers, production backup and restore, and real
  Telegram playback latency remain open. The live Chrome observation under
  [BUG-03](bugs.md) confirms one stall. A causal diagnosis and full-session
  count require separate evidence. All changes are local on `dev`.

### Frozen browser run and accessibility follow-up

- One frozen bundle covered 521 Chromium cases. A host session restart
  interrupted the original run. The remaining 154 cases ran against the same
  frozen assets. Across both parts, 501 passed and 20 failed. The
  [reconciliation](../cache/frontend-redesign/browser-frozen-8bsy14_g/reconciliation.json)
  records each case and result.
- All 20 flagged cases passed in the 35-case Chromium rerun against the
  latest local bundle. The rerun covered selection, file actions, preload
  and copy-link controls, sidebar and directory layout, notifications,
  Performance, and MKV recovery. The fixture now waits for the chapter panel
  to open and holds the first thumbnail request while it checks placeholders.
  [Chromium results](../cache/frontend-redesign/axe-candidate/)
- The browser accessibility scan covered 110 page and interaction states in
  Chrome at 1440 and 320 px, each in light and dark appearance. Axe reported
  violations in 10 states on the old build and zero on the latest bundle.
  The [baseline](../cache/frontend-redesign/axe/baseline.json) and
  [latest scan](../cache/frontend-redesign/axe/current.json) contain the
  reported elements and manual-review results. Screen-reader operation on
  physical devices remains open.
- The later manual accessibility pass inspected Chromium accessibility trees
  for share expiry, plain text, and enhanced video at 320 and 1440 px.
  App-owned findings in the targeted rule were resolved. The desktop 360°
  control from the embedded player remains a manual-review item.
  [Accessibility trees](../cache/frontend-redesign/axe/manual-audit/candidate/delivered-ax-trees.json)
- The integrated bundle passed ten WPE WebKit cases for selection across six
  viewports, PDF navigation and focus at two phone sizes, and enhanced MKV
  playback and paused seek at two phone sizes. The independent native
  H.264/AAC probe played real frames and sought while paused.
  [WebKit results](../cache/frontend-redesign/integrated-webkit/)

- A follow-up run against the frozen bundle serving the local demo passed
  519 of 521 Chromium cases. Its Shared Links failure expected an obsolete
  aria-label. The corrected test checks the accessible description and exact
  tooltip. Eight focused cases passed against the same bundle. The other
  failure was sparse-frame video playback. See [BUG-03](bug-plan.md).
  [Full run](../cache/frontend-redesign/browser-frozen-cp2tn5_p/status.txt) ·
  [Shared Links rerun](../cache/frontend-redesign/owner-expiry-triage/run.log)
- In Windows Chrome at a 1536×695 browser viewport, a wheel scroll over
  sidebar links moved the navigation. A wheel scroll over Documentation
  moved it back while the brand and main page stayed put. Enter opened a
  text preview, Escape returned focus to the file, and toggling selection
  left the first file in the same position. The desktop wheel regression
  now runs at 1536×695 and 1280×420.
- The [keyboard resize checks](../cache/frontend-redesign/keyboard-height-probe/)
  reproduced the focused Timezone field being clipped by the workspace.
  The fixed build passed eleven Chromium settings and geometry cases,
  plus two touch checks that scrolled to Save. The native layout resize
  and a visual-only viewport simulation exercised separate keyboard resize
  behaviors.

### Desktop density and design references

The owner’s Chrome screenshot supplied the spacing baseline. At a 1536×695
browser viewport, the old layout clipped Storage and Backups and showed two
complete file rows. The updated desktop shell exposes all eleven main links
and shows three complete rows with six columns. At 1366×768, the compact grid
also gains a complete row. The sidebar still scrolls to help and sign-out when
there is more content than fits. Standard folder tiles in the sampled Grid
went from 84 to 60 CSS pixels, with room for longer names to wrap.

The final build was captured across 11 layouts, including the reported desktop
viewport and phone portrait and landscape. The desktop and touch browser runs
covered sidebar focus and wheel scrolling, file actions, selection, sparse
folders, scoped search, and transfer pagination. The long-name test now waits
for the thumbnail's entrance animation before measuring the full-width compact
preview. All eight long-name cases passed after that change. Axe reported zero
violations across 110 sampled states. Representative captures were visually
reviewed. Physical phone, screen reader, and touchpad checks remain open.

- [Desktop before and after](../cache/frontend-redesign/compact-explorer/density/)
- [Visual evidence receipt](../cache/frontend-redesign/compact-explorer/visual-evidence.json)
- [Accessibility results](../cache/frontend-redesign/compact-explorer/axe.json)


### Browser zoom and anonymous keyboard navigation

A temporary Chromium test profile set actual tab zoom through the browser.
At 100%, 200%, and 400%, the same window exposed 1570×788, 785×394, and
392×197 CSS pixels. Thirty views across those levels cover the file browser,
transfers, sharing, Performance, Preloads, Connections, backups, and the
three Settings sections. At 200%, Clear Cache opens its confirmation and
Escape cancels it. At 400%, the file browser and the Transfers count can both
be reached while uploads are active. A text preview opened and returned
focus to its file on Escape. Firefox checks against the integrated build
at 1440×900, 785×394, and 392×197 covered folder activation and cache
confirmation. The Firefox checks used viewport sizing.

Six new embed cases cover continued pagination, the final page, an empty final
batch, retry, changing focus mid-load, and pointer use. The 320×360 Chromium
capture shows keyboard focus on the first newly loaded file. The published
bundle passed 23 focused Chromium tests, including all 18 embed cases.
Both sidebar shortcuts passed the desktop size and alignment checks. Axe reported zero violations across 110 sampled states of the published build.

- [Zoom visual evidence receipt](../cache/frontend-redesign/browser-zoom/visual-evidence.json)
- [Published bundle browser checks](../cache/frontend-redesign/browser-zoom/publish-browser.log)
- [Chromium zoom captures and geometry](../cache/frontend-redesign/browser-zoom/released/)
- [Firefox captures](../cache/frontend-redesign/browser-zoom/firefox/)
- [Embedded keyboard pagination](../cache/frontend-redesign/input-audit-embed-focus/browser/embed-more-keyboard-focus.png)


### Full Chromium run and browser compatibility

The frozen demo build ran 533 Chromium tests. It passed 527. Six mixed Grid
cases expected the old minimum folder height. The corrected check kept its
wrapping, badge and action geometry assertions. All eight mixed Grid cases
passed when rerun against the frozen assets and the next build.

The forced-color browser probe compared the actual active and inactive states
in light and dark palettes. Both new tests failed against the old assets and
passed with system-color indicators. Firefox 157 passed MP4, WAV and PDF
preview workflows at 1440×900 and 392×700, including a paused video seek,
page navigation and Escape focus return. Firefox also passed seven focused
embed pagination and navigation cases.

- [Full frozen run](../cache/frontend-redesign/browser-frozen-t478u161/status.txt)
- [Mixed Grid reconciliation](../cache/frontend-redesign/firefox-previews/mixed-desktop-verified.log)
- [High-contrast captures](../cache/frontend-redesign/forced-colors/verified/)
- [Firefox previews](../cache/frontend-redesign/firefox-previews/results-actual-audio.json)
- [Firefox embed checks](../cache/frontend-redesign/forced-colors/firefox-embed.log)


### Card layout and clearer owner pages

Grid uses 16:9 thumbnails and places the filename and menu in one row under
the thumbnail. Dates and sizes are available in List. Folder tiles use the
existing icon, name, and status indicators. At 1536×695 and 1366×768 the
demo still shows three complete rows. The browser exercised portrait and
landscape touch menus, filename wrapping, keyboard focus, and both views.

The desktop view switch labels both choices and uses blue for the active
view. Its two buttons are 44 px high. The measured active label contrast
was 5.77:1 in the light theme and 7.52:1 in the dark theme. The compact
menu presents the current view icon and announces the current view.

Performance puts transfers needing attention first and lists each reported
service wait once. Its **Server resources** tab groups disk, RAM, cache, and
temporary upload storage. **Storage and Backups** leads with recovery and
keeps **Drive contents** in a separate section. Drive totals reuse the
existing owner management snapshot and refresh on request. The Performance
overview is 64 px shorter on desktop and 106 px shorter at a 320 px width.

**Link opens** is implemented in BUG-14. It counts displayed public entries
and repeat visits. Pagination, downloads, media ranges and failed unlocks add
zero. The owner sees the aggregate and start date. Catalog snapshots preserve
both fields, and temporary retry hashes expire without going into backups.

The bundle served by the local demo passed all 823 frontend unit tests and
12 focused Chromium checks, including help dismissal from both the trigger
and the popup. The browser walkthrough captured 274 rendered screens across
11 layouts. The final review sampled the desktop Grid and phone Performance
and Drive contents screens. Axe reported zero violations in 110 scanned states.
Physical phones, touchpads, and screen readers remain unverified.

- [List and Grid contrast and click tests](../cache/frontend-redesign/site-consistency/toggle/)
- [File card measurements](../cache/frontend-redesign/card-aspect-candidate/final/measurements.json)
- [Performance before and after](../cache/frontend-redesign/overview-simplification/)
- [Cross-page visual evidence](../cache/frontend-redesign/site-consistency/visual-evidence.json)
- [Final focused Chromium checks](../cache/frontend-redesign/site-consistency/help-release.log)
- [Final accessibility scan](../cache/frontend-redesign/site-consistency/axe-release.json)


### Compact view switch and Performance layout review

FP-43, FP-44, and FP-45 were handled in order. List and Grid switch with one
click or the existing keyboard controls. The new desktop sizes are 32 px with
6.92:1 active text contrast in the light theme and 9.19:1 in the dark theme.
Direct touch checks measured 44 px at desktop and phone widths.

Performance uses one aligned header for the history interval and settles on
side-by-side network and resource charts at wide widths. At 1920 CSS px, the
CPU plot previously rendered at 139.875 px in a 104 px plot area and overlapped
the time labels. The grid row now matches the plot area. Network rates double
as the legend, and isolated readings remain visible across gaps. All-time
totals and applied limits are directly visible in Tuning.

The final Performance review captured 70 rendered states across 14 viewport
and theme combinations. It checked desktop, tablet, phone portrait and phone
landscape. Chromium verified the compact switch and chart layout. Firefox and
WebKit exercised the history slider, navigation position, and visible tuning
totals at 1536 and 390 CSS px. The local demo mixes synthetic RAM history with
its host-derived current resource summary. Its RAM figures can differ even
when the readings have the same timestamp.

- [Compact switch before](../cache/frontend-redesign/toolbar-refinement/toggle/dark-grid-control.png)
- [Compact switch after](../cache/frontend-redesign/toolbar-refinement/after/toggle/dark-grid-control.png)
- [Desktop Performance overview](../cache/frontend-redesign/performance-layout/review-captures/1536x695-dark-overview.png)
- [Performance on a 320 px phone](../cache/frontend-redesign/performance-layout/review-captures/320x640-dark-charts.png)
- [Tuning totals on a 390 px phone](../cache/frontend-redesign/performance-layout/review-captures/390x844-light-tuning.png)
- [Performance visual evidence](../cache/frontend-redesign/performance-layout/visual-evidence.json)

The 535-case frozen Chromium run passed 532 on its first build. One assertion
still expected the removed Storage and Backups layout. The phone filename
and desktop touch selection checks caught FP-47 and FP-48. All three cases
passed against the corrected demo build. At 1440 px, opening selection moved
the sample card 39.41 px before the fix and 0 px after it.
The published build passed 64 explorer and mobile interaction cases and 13
focused Performance and history cases.

- [Frozen browser run](../cache/frontend-redesign/browser-frozen-7qc75y2t/status.txt)
- [Three focused reruns](../cache/frontend-redesign/performance-layout/recheck-final.log)
- [Published explorer checks](../cache/frontend-redesign/performance-layout/files-final.log)
- [Published Performance checks](../cache/frontend-redesign/performance-layout/published-performance.log)
- [320 px card and action layout](../cache/frontend-redesign/performance-layout/recheck-final-artifacts/last-card-clearance-320x740.png)
- [Selection mode before and after](../cache/frontend-redesign/performance-layout/selection-captures/)


### Performance density, phone browsing, and Connections

FP-49 through FP-51 were applied in order. Performance uses 18 px network
readings and 16 px resource values. Supporting descriptions use 13 px regular
text. The four tabs keep their original content and phone targets. History
range, sampling, and accuracy checks still pass.

At 348 px the phone explorer shows the workspace title on one line. The
themed type picker marks the selected option and supports tapping, keyboard
selection, and Escape focus return. Background server jobs keep their tab
count. Uploads started in this browser still have the existing status entry.

Connections now presents status with Refresh and gives the account details
their own row on phones. The first account starts at 353 CSS px instead of
605 px at 320×640. Its full status and Manage button fit before the bottom
navigation. The original saved-credential, validation, and Premium flows
passed on fixtures.

The final build yielded 51 screenshots across Chromium, Firefox, and WebKit.
The 111-state document scroll audit reported no excess scroll range, and
axe reported zero violations in 110 sampled states. All 823 frontend unit
tests passed. The final build passed 37 phone and file-browser checks,
11 Connections and sharing checks, and 6 Performance checks. These checks
used the isolated demo and browser-emulated phone layouts.

- [Phone file browser](../cache/frontend-redesign/phone-refinement/final-captures/348x752-dark-files.png)
- [Phone type picker](../cache/frontend-redesign/phone-refinement/final-captures/348x752-dark-type.png)
- [Phone Connections](../cache/frontend-redesign/phone-refinement/final-captures/320x640-dark-connections.png)
- [Performance resource text](../cache/frontend-redesign/phone-refinement/final-captures/390x844-light-performance-server-resources.png)
- [Visual evidence](../cache/frontend-redesign/phone-refinement/visual-evidence.json)
- [Before and after measurements](../cache/frontend-redesign/phone-refinement/final-measurements.json)
- [Document scroll results](../cache/frontend-redesign/scroll-audit/after/results.json)

### Preview recovery and Performance Overview acceptance

Preview recovery uses the shared Mantine error notice and puts the action before
long error details. Retry returns focus to Close in the reopened preview. A
short unsupported preview puts Download before the file details. The changes touch
`Preview.tsx`, `Preview.css`, the shared preview styles, and the usage guide.
Nine focused Playwright tests passed. Rendered action placement, retry, and
focus were also checked in Chromium, Firefox, and WebKit.

Overview uses the section spacing, type scale, and secondary buttons from Settings.
Transfer activity comes first, followed by observed waits and their corresponding
actions. Telegram pool and account counts are displayed without adding overlapping
queues. Cooldowns only appear with evidence of waiting work. Configured speed
limits and read buffer waits link to their corresponding controls or details.
Charts and history remain below the summary. The scrubber still exposes detailed
samples, while the initial live view shows the headline values once. Changes are
in `PerformanceOverview.tsx`, `PerformanceOverview.css`, `PerformancePanel.tsx`,
the Performance browser tests, and `docs/performance-tuning.md`.

All 39 Performance, history, and refresh Playwright checks and 31 Performance
unit tests passed. Coverage includes live and historical chart inspection,
keyboard focus, configured limits, read buffer waits, idle cooldowns, stale
readings, refresh failure and recovery, and empty history. Snapshot failures
use one persistent inline alert. The pinned captures passed first-viewport, 44 px action, and
document overflow checks at 320, 390, 820, and 1536 px across both themes.
Controlled responses confirmed initial loading, no transfers requiring
attention, no observed waits, and recovery from a 503 at 390×844 dark and
1536×695 light.
Refresh, Accounts, the time range, and Transfers worked using the running demo's
unmodified responses at 1536×695 and 390×844. The published build was opened in
Chrome. Telegram waits use sample data and phone checks use browser emulation.

The earlier pre-preview full browser pass reported 534 of 535 tests passing.
The 320 px thumbnail failure reproduced against the published Overview build.
The first row was already visible with the workspace scrolled 120 px, so the
test never moved the thumbnail outside the observer's view. The test now
resets the file grid and workspace scroll positions before it checks thumbnail
retention and request count. All 52 cases in `test_browser_file_views.py`
passed, including the thumbnail check at 320, 390, and 1440 px.

The full Chromium rerun used a frozen copy of the published build and collected
537 cases. The parallel run passed 525. Eleven failures traced to HTTP 507
responses from upload and recovery routes while fixtures used WSL's `/tmp`.
The video-poster check counted an explorer thumbnail that finished loading
before the preview opened. All 12 cases passed in a single-worker rerun with
temporary files on the workspace disk. The poster test now waits for the
explorer thumbnail explicitly before monitoring requests from the preview.
All 17 video preview cases passed with that correction. These changes affect
tests only.

**Next action.** Wait for the owner's visual review of both comparisons before
accepting Overview or starting another redesign.

- [Local Performance Overview](http://127.0.0.1:18769/?view=performance)
- [1536×695 before and after](../cache/frontend-redesign/overview-acceptance/compare-1536x695.png)
- [390×844 before and after](../cache/frontend-redesign/overview-acceptance/compare-390x844.png)
- [Performance and history browser results](../cache/frontend-redesign/overview-acceptance/tests.log)
- [Performance unit results](../cache/frontend-redesign/overview-acceptance/unit.log)
- [Running demo checks](../cache/frontend-redesign/overview-acceptance/live.log)
- [Loading, empty, and unavailable state checks](../cache/frontend-redesign/overview-acceptance/states.log)
- [Preview recovery test results](../cache/frontend-redesign/preview-recovery/final/tests.log)
- [File view regression results](../cache/frontend-redesign/thumbnail-checkpoint/final.log)
- [Full Chromium verification summary](../cache/frontend-redesign/browser-frozen-pd8lq97t/verification.txt)
- [Single-worker recheck results](../cache/frontend-redesign/browser-frozen-pd8lq97t/rerun.log)
- [Video preview regression results](../cache/frontend-redesign/browser-frozen-pd8lq97t/poster.log)


### Whole Performance page review

The local dev demo serves this revision. The comparisons pair the previous
demo and this build in the dark theme with the same captured Performance, jobs
and settings responses. The requested laptop and phone sizes are CSS viewports.
Fetch timestamps reflect the time of each check.

| Workflow | 1536 × 695 laptop | 390 × 844 phone |
| --- | --- | --- |
| Overview | [Before and after](../cache/frontend-redesign/overview-feedback/compare-overview-1536.png) | [Before and after](../cache/frontend-redesign/overview-feedback/compare-overview-390.png) |
| Server resources | [Before and after](../cache/frontend-redesign/overview-feedback/compare-resources-1536.png) | [Before and after](../cache/frontend-redesign/overview-feedback/compare-resources-390.png) |
| Temporary upload storage | [Before and after](../cache/frontend-redesign/overview-feedback/compare-uploads-1536.png) | [Before and after](../cache/frontend-redesign/overview-feedback/compare-uploads-390.png) |
| Tuning | [Before and after](../cache/frontend-redesign/overview-feedback/compare-tuning-1536.png) | [Before and after](../cache/frontend-redesign/overview-feedback/compare-tuning-390.png) |
| Accounts | [Before and after](../cache/frontend-redesign/overview-feedback/compare-accounts-1536.png) | [Before and after](../cache/frontend-redesign/overview-feedback/compare-accounts-390.png) |

The changes are in the Performance panel and Overview components, their styles,
shared monitor styles and the panel caller in App. The performance tuning guide
and the browser tests for Performance, history and refresh behavior were updated.

Verification on this revision:

- The production build, TypeScript check and 31 Performance unit tests passed.
- [47 focused Chromium cases](../cache/frontend-redesign/overview-feedback/review-tests.log) passed, including incomplete samples, non-overlapping requests, history changes, chart keyboard and touch controls, and spacing under all four tabs.
- [All four tabs](../cache/frontend-redesign/overview-feedback/capture.log) rendered at 320, 390, 820 and 1536 pixels, with both themes exercised at 390. The chart overlay opened and cleared and Accounts received focus from Overview.
- [Docker demo](../cache/frontend-redesign/overview-feedback/docker-live.log) and [existing local demo](../cache/frontend-redesign/overview-feedback/live.log) passed automatic update, history selection and navigation checks at both acceptance sizes. The Docker fixture used synthetic telemetry and isolated fixture storage.
- [Loading, empty and failure checks](../cache/frontend-redesign/overview-feedback/states.log) verified initial loading, a healthy quiet state, a failed snapshot and automatic recovery on phone and laptop.

Next action is owner visual review of the [local demo](http://127.0.0.1:18769/?view=performance).
Physical-device and live Telegram measurements remain unverified for this revision.

## Current dev review pass

### Shared design and accessibility · FP-01, 03, 04, 16, 20, 24, 26, 30, 31, 33, 36, 40, 42 and 69

The PDF options button and fit controls used selectable text on the current
checkout. Control and toolbar selection rules now apply to portaled menus
and the existing preview controls. File text and editable fields keep
selection. The three new Chromium cases passed on laptop, tablet and phone
in both themes and used actual mouse input to select the PDF and DOCX text.
The previous build failed the control-style assertion.

17 additional Chromium cases passed on the tested bundle. They covered
forced-color selection and navigation cues, control focus and Escape,
notifications in both themes, the bot encryption control and help popup,
and preserved search and text selection. Prior sitewide scans and zoom
measurements remain historical evidence. The final sitewide pass still
needs to retest the combined frontend changes. Physical touch, screen reader
and browser zoom checks on this specific bundle remain unverified.

- [Three responsive selection cases](../cache/bug-review/frontend-shared/after/selection.log)
- [Shared control sweep](../cache/bug-review/frontend-shared/after/audit.log)
- [Laptop preview](../cache/bug-review/frontend-shared/after/control-text-selection-1536.png)
- [Phone notification](../cache/bug-review/frontend-shared/after/notification-modal-dark-390.png)

Next workflow is Shell and access, including the collapsible tools group.

### Shell and access · FP-08, 10, 11, 21, 35 and 68

Tools and settings now starts closed on file views and expands on direct
tool navigation. The disclosure retains focus when collapsing, and a
collapsed active group has a spoken current-page description. File routes
remain directly reachable. The existing sidebar scroller keeps the
wordmark and New in place while links and support actions move.

28 Chromium cases passed across the shell and access flows. They covered
wheel scrolling over Documentation, touch drawer scrolling, drawer focus
trapping and return, another-tab links, rejected login, session expiry,
logout and recovery-only access. Three final disclosure cases passed at
1536×695, 820×1180 and 390×844, including an emulated touch tap. All three
baseline cases failed because the old sidebar had no disclosure. Physical
touchpads and mobile software keyboards remain unverified on this bundle.

- [Shell and access sweep](../cache/bug-review/frontend-shell/after/audit.log)
- [Final disclosure checks](../cache/bug-review/frontend-shell/after/disclosure.log)
- [Laptop before](../cache/bug-review/frontend-shell/before/sidebar-tools-1536.png)
- [Laptop after](../cache/bug-review/frontend-shell/after/sidebar-tools-1536.png)
- [Phone after](../cache/bug-review/frontend-shell/after/sidebar-tools-390.png)

Next workflow is Explorer and file actions, including right-click, card
density and the List/Grid switch.

### Explorer and file actions · FP-64–67 and 73

The same options menu now opens via right-click, Shift+F10, or the existing
actions button. Escape returns focus to the initiating item. Right-click in
selection mode keeps the current bulk selection. Guest menus offer read-only
actions. A deferred scroll event could close a newly opened List menu, so the
menu now closes only when the pointer anchor actually moves or leaves the list.

Grid uses four columns at 1536 px and caps at five at 1920 px. The matched
laptop capture increased the first video thumbnail from 183.3 px to 287 px.
The sample filename fits on one line, and two columns remain at 390 px.
The List/Grid switch uses a soft blue active state and retains its forced
colors cue.

Date Added previously widened the sort trigger and reflowed the toolbar.
The trigger reserves the label width, and ordinary controls size to their
contents. The toolbar and visible view switch keep the same geometry through
Name, Date Added and File Size at 1536, 1024, 820 and 390 px. Chromium also
checked menu focus return and horizontal containment.

The broad Explorer run recorded 78 passes and the List context-menu failure.
Seven focused Chromium cases then passed on the updated bundle, including
the previously failing List case, Grid, read-only menus and responsive sort.

- [Before and after dimensions](../cache/bug-review/frontend-explorer/demo-measurements.json)
- [Laptop before](../cache/bug-review/frontend-explorer/before/demo-explorer-1536.png)
- [Laptop after](../cache/bug-review/frontend-explorer/after/demo-explorer-1536.png)
- [Broad Explorer run](../cache/bug-review/frontend-explorer/after/audit.log)
- [Final context and toolbar verification](../cache/bug-review/frontend-explorer/after/sort.log)

### Transfers and Preloads · FP-17, 32 and 52

23 Chromium cases passed on the current Explorer bundle. They covered
retained transfer history and filter reset, interrupted transfer recovery,
the collapsed transfer panel, single and multiple file torrent selection,
whole-file preload and cached-byte reuse, the Clear Cache button's layout,
and capacity failure with retry. No transfers or preload code change was
needed for those cases. Physical gestures and the final combined run remain
open. [Transfer and preload checks](../cache/bug-review/frontend-explorer/after/transfers/audit.log)

### Performance, Backups and sidebar follow-up · FP-75–77

Backups keeps its existing direct route. The page now covers database protection,
backup publication and recovery. Performance Overview shows files, folders,
Telegram-stored bytes and Trash totals above the charts. It uses the existing
live snapshot and links to Trash. The temporary upload storage section links
to Transfer limits. The wide chart layout stretches the network plot to the
combined CPU and RAM card height. At 1536×695 the matched demo measured
350.5 versus 382.1 px before, and 382.1 px for both cards after. At 390 px,
charts remain stacked. Sidebar row gaps increased to 4 px. Desktop rows are
40 px with Tools and settings collapsed and 36 px when expanded so all tools
remain within reach.

- [Laptop Overview](../cache/bug-review/frontend-previews/after/1536-overview.png)
- [Laptop chart comparison before](../cache/bug-review/frontend-previews/before/1536-charts.png) and [after](../cache/bug-review/frontend-previews/after/1536-charts.png)
- [Phone Overview](../cache/bug-review/frontend-previews/after/390-overview.png)

### Preview chrome and progress · FP-71, 72 and 74

The shared desktop toolbar measured 52 px, down from 68 px, across the seven
sampled previews. Single-row phone previews measured 52 px, EPUB and comics
101 px, and PDF retained its 101 px two-row layout with 44 px controls. The
video popup is gone. The player stays paused at its saved position and Home
on its timeline can seek back to the beginning.

A held PDF close write reproduced the stale read. The progress writer now
lets an in-tab reopen wait for the pending write to settle, and a replacement
write session waits for its predecessor. Focused Chromium checks held the
final save and verified page 10 for PDF and 2.5 seconds for video. The PDF
also restored page 10 after reload once its save completed. Owner progress
still uses the server and anonymous progress stays browser-local. Full page
navigation can still lose a pending write when its delivery fails.

- [Preview height measurements](../cache/bug-review/frontend-previews/after/measurements.json)
- [Laptop video](../cache/bug-review/frontend-previews/after/1536-movtext-subtitles.mp4.png) and [phone PDF](../cache/bug-review/frontend-previews/after/390-sample.pdf.png)
- [69 preview tests](../cache/bug-review/frontend-previews/after/audit.log)
- [Focused close and reopen checks](../cache/bug-review/frontend-final/after/progress-reopen.log)
- [823 frontend unit tests after request ordering follow-up](../cache/bug-review/frontend-final/after/frontend-unit-recheck.log)

### Shares and public embeds · FP-18, 28, 34, 38, 52, 62, 63 and 70

Shared Links now keeps password protection visible beside the item details,
even when the label and folder path need truncation. It also aligns item,
expiry, opens and action headings with their desktop row columns. The
responsive regression checks all four column starts, keeps the protection
status in the filename's accessible description, and checks for overflow
at 1440, 390 and 320 px. The protected-link prompt and guest read-only
behavior were exercised separately in BUG-14.

- [Laptop Shared Links](../cache/bug-review/frontend-share-finish/after/shares-concise-1440.png)
- [Phone Shared Links](../cache/bug-review/frontend-share-finish/after/shares-concise-390.png)

### Integrated verification and handoff

- [Eight current Preloads cases](../cache/bug-review/frontend-verified/after/preloads.log)
  covered cached-byte reuse, per-file eviction, original-file retention and
  safe confirmation focus at five viewport sizes, including 320×640 and 740×360.
- [Firefox and WebKit on the running demo](../cache/bug-review/frontend-verified/after/cross-browser.log)
  checked the PDF options popup, Escape and focus return, keyboard chart
  inspection, library placement and the Backups page at 1536×695 and 390×844.
- [Ten reader and file grouping cases](../cache/bug-review/frontend-verified/reader/recheck.log)
  verified EPUB and comic controls, layout and position persistence with the
  updated toolbar after amending the old background-color assertion.
- The local demo at [port 18772](http://127.0.0.1:18772/) serves the latest
  built asset index. Live Telegram intake, the hosted MKV, physical-device
  input and true cross-site HTTPS cookie behavior remain unverified. The
  historical backup burst cannot be attributed without its production logs.
  See the bug execution tracker for the setup and exact checks still needed.
- [54 Shared Links, reader and embed cases](../cache/bug-review/frontend-share-finish/after/audit.log)
  passed on the latest build, including the desktop heading alignment, phone
  wrapping, protection status, create/copy/edit/revoke and guest permissions.
- [Four library and Performance checks](../cache/bug-review/frontend-share-finish/after/library.log)
  passed at 1536, 820 and 390 px. They verified the chart geometry, Trash link,
  separation from Backups and server resources, and visible values updating
  with the automatic poll.
- [69 progress cases in isolated Docker](../cache/bug-review/frontend-verified/after/docker-progress.log)
  passed. A separate MongoDB 8 replica set and generated database read the
  saved page, page count and position from a fresh catalog instance. The
  test container and network were removed after the run.
- [Browser zoom check](../cache/bug-review/frontend-share-finish/after/zoom.log)
  set Chromium's actual page zoom to 100%, 200% and 400% on the running
  demo. Ten owner routes at each level had no control clipping. The check
  opened and cancelled the cache-clear dialog at 200%, then used the file
  preview and Transfers navigation at 400%.
- [Combined Chromium sweep](../cache/bug-review/frontend-share-finish/combined/audit.log)
  recorded 169 passes and one landscape check that reloaded before the
  upload handoff was acknowledged. The product correctly exposed the
  upload issue. The test now waits for the successful server handoff
  before checking the routine-upload banner behavior.
- [30 shell and mobile cases](../cache/bug-review/frontend-share-finish/mobile/recheck.log)
  passed after synchronizing the landscape reload test with the real server
  handoff. This includes the wider run's sole failure.
- The final production web build, scoped Ruff and `git diff --check` passed.
  All 14 planned bug IDs have local execution outcomes in [the bug tracker](bug-plan.md).
  The working checkout and demo are ready for owner review.

## FP-01 through FP-77 current demo audit

Every entry below has its own result. The read-only checks used the running
[demo at port 18772](http://127.0.0.1:18772/). Cases that needed writes,
failed responses or held requests ran in independent local browser fixtures
using the same built frontend. The original asset fingerprint is recorded in
[build.txt](../cache/bug-review/frontend-audit77/build.txt). When a source fix changes the build, the
affected checks are repeated on the updated demo and isolated fixture. Synthetic
Telegram data, emulated touch and browser accessibility checks are reported as
such.

Changes found in this pass. FP-20 and FP-24 exposed a duplicate name in the
preload switch. The patched switch exposes its short field name and separate
help in the demo and passed the isolated Settings save check. FP-18 and FP-53
also exposed two old tests that rejected any guest action element, including
the hidden trigger for the read-only context menu. Both now assert that guest
actions start closed and the owner actions stay hidden. Both cases passed.
[Updated demo fingerprint](../cache/bug-review/frontend-audit77/fix/build.sha) · [Settings regression](../cache/bug-review/frontend-audit77/fix/settings.log) · [Guest regressions](../cache/bug-review/frontend-audit77/repros/after-e2e.log)

[Exact fixture cases per ID](../cache/bug-review/frontend-audit77/verify.tsv) · [Demo interactions and captures](../cache/bug-review/frontend-audit77/demo/) · [Fresh fixture logs](../cache/bug-review/frontend-audit77/fixtures/) · [Targeted reruns](../cache/bug-review/frontend-audit77/repros/) · [Chromium page zoom](../cache/bug-review/frontend-audit77/demo/zoom.log) · [Firefox and WebKit](../cache/bug-review/frontend-audit77/demo/cross-browser.log) · [Frontend unit tests](../cache/bug-review/frontend-audit77/fix/unit.log)

| ID | Current result | Observation and scope | Fixture evidence |
| --- | --- | --- | --- |
| FP-01 | Demo and fixture verified | Opened and dismissed the touch drawer, opened a folder and rendered PDF at 320×640, 390×844 and 740×360. Physical devices are outside the demo. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-01.json). Physical phones were not available. | 6/6 passed |
| FP-02 | Demo and fixture verified | All four Performance tabs rendered. The transfer CTA was visible and View Accounts switched tabs and focused Accounts. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-02.json). The demo presents sample readings. | 6/6 passed |
| FP-03 | Demo and fixture verified | Light and dark owner views shared the action token, with four screens checked per theme and Overview captures. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-03.json). Owner visual preference has no binary test. | 5/5 passed |
| FP-04 | Demo and fixture verified | Encrypt bot uploads switch and info button centers align. At 740×360 the help opens within the viewport and Escape restores focus. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-04.json). Help opens and dismisses at the reported short touch viewport. | 1/1 passed |
| FP-05 | Demo and fixture verified | Backups separates database protection and recovery, shows the action and frankly labels the unconfirmed demo backup on desktop and phone. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-05.json). Backup transport ran against simulated Telegram. | 5/5 passed |
| FP-06 | Demo and fixture verified | Opened and closed image, audio, JSON, PDF, video, unknown, EPUB, CBZ and CBR by keyboard. Office, ZIP and write/error variants require the isolated fixture run. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-06.json). Office, ZIP and compatible playback are exercised with generated/sample files. Production MKV streaming is a separate gap. | 11/11 passed |
| FP-07 | Demo and fixture verified | All three category links open their own section under the single Settings title. Scoped saves and draft conflicts are fixture-only. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-07.json). Drafts, scoped writes and conflicts use an isolated writable fixture. | 6/6 passed |
| FP-08 | Demo and fixture verified | Direct file/activity links and active location work. Tools is collapsed by default and expands with Enter per FP-68, the later owner request. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-08.json). FP-68 superseded always-visible tools. | 3/3 passed |
| FP-09 | Demo and fixture verified | Fresh List, aligned named metadata headers, Grid switch and persisted Grid after reload on the populated sample folder. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-09.json). Checked fresh List, stored layout and grid controls. | 6/6 passed |
| FP-10 | Demo and fixture verified | Dark phone login reveals the field, rejects a wrong password, returns focus and succeeds on correction. Also repeated from the in-app browser. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-10.json). Demo credentials were synthetic. | 2/2 passed |
| FP-11 | Demo and fixture verified | At 1280×420 keyboard navigation to Documentation scrolls the sidebar and leaves the brand and root layout pinned. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-11.json). Checked wheel and pinned wordmark with navigation. | 2/2 passed |
| FP-12 | Demo and fixture verified | A 320 px touch tap on the sample file checkbox selects it and leaves the current folder open. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-12.json). The originally reported bottom-nav mis-hit did not reproduce in the tested layouts. | 9/9 passed |
| FP-13 | Demo and fixture verified | Mouse click selects and double-click opens. A phone tap opens directly. Long hold, scroll cancellation and guest parity use isolated fixture cases. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-13.json). Long press and swipe input were emulated. | 4/4 passed |
| FP-14 | Demo and fixture verified | A phone row exposes its full 100+ character filename in the accessible control and its Actions button stays visible. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-14.json). Names tested visually and through their accessible labels. | 8/8 passed |
| FP-15 | Demo and fixture verified | Regional and Connections forms render within the phone viewport. Stale errors and field correction need the writable fixture run. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-15.json). Validation used writable fixture state. | 2/2 passed |
| FP-16 | Demo and fixture verified | Opened and dismissed settings help using the keyboard with focus return. Dialog Tab cycling and validation use fixture cases. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-16.json). Keyboard and disclosure states exercised. | 4/4 passed |
| FP-17 | Demo and fixture verified | At 320 px the end of the history filter strip is reachable and Cancelled responds to Enter. Synthetic transfers supply long names and held work in fixtures. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-17.json). Transfer state supplied by fixture. | 3/3 passed |
| FP-18 | Demo and fixture verified | Shared Links opens the phone edit dialog with No expiry visible and Escape returns focus. Mutations and anonymous sessions use fixtures. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-18.json). Save, expiry and anonymous read-only checks use synthetic data. | 7/7 passed |
| FP-19 | Demo and fixture verified | At 320 px options fit the viewport and Escape closes options before the preview, restoring both focus targets. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-19.json). PDF page, zoom and focus retested. | 5/5 passed |
| FP-20 | Demo and fixture verified | Measured a 44 px drawer trigger and verified keyboard navigation focus return. Screen reader hardware is outside this run. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-20.json). Physical screen reader and device checks remain open. | 10/10 passed |
| FP-21 | Demo and fixture verified | Wheel over Documentation scrolls the sidebar at 1536×695 while the logo stays fixed. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-21.json). Phone touch was emulated. | 4/4 passed |
| FP-22 | Verified in fixture |  Real UI with a simulated Premium challenge. Wrong code and lost response refocus the code field. | 1/1 passed |
| FP-23 | Demo and fixture verified | Verified Preload has a button role, Escape returns focus to the action, and changing focus to the next file closes the menu. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-23.json). Keyboard focus and action roles exercised. | 6/6 passed |
| FP-24 | Demo and fixture verified | Calculated text/track contrast for bot and preload switches in both themes. Ratios 5.77:1, 5.77:1, 7.52:1, 7.52:1. Labels have 44 px targets. The preload switch now exposes its short accessible name and separate help. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-24.json). The demo measured text/track contrast. The isolated Settings save test also checks the new short preload name and separate help. | 3/3 passed |
| FP-25 | Demo and fixture verified | Visible Server resources definition lists contain label and value elements through valid direct or grouped markup. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-25.json). Valid definition-list markup sampled on the running Server resources tab. | 5/5 passed |
| FP-26 | Verified in fixture |  A controlled long notification was used to exercise the New menu and dismissal. | 6/6 passed |
| FP-27 | Demo and fixture verified | File row y-position remains stable to within 1 px when entering phone selection mode. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-27.json). Position stability checked down to narrow mobile widths. | 6/6 passed |
| FP-28 | Demo and fixture verified | Share expiry has a description reference and the JSON preview exposes a named/structured region. Fixture tests exercise video semantics and assistive names. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-28.json). Checked semantic names and descriptions, not a physical screen reader. | 2/2 passed |
| FP-29 | Verified in fixture |  Physical keyboards remain unverified. The fixture reduces the phone viewport and checks field and Save reachability. | 2/2 passed |
| FP-30 | Demo and fixture verified | Captured the populated laptop Grid with pinned, visible wordmark and no root horizontal overflow. FP-66 supersedes the older six-column density. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-30.json). FP-66 supersedes six-column density. | 12/12 passed |
| FP-31 | Demo and fixture verified | The rendered header uses TGDrive’s logo and local components. This is a design direction, with owner visual acceptance separate. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-31.json). Design guidance assessed by rendered sampling, with owner preference separate. | 8/8 passed |
| FP-32 | Demo and fixture verified | Clear Cache fits a 785×394 viewport, opens the confirmation and restores focus after Escape. Actual browser zoom is checked separately. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-32.json). Actual zoom run checks Clear Cache and focus return. | 5/5 passed |
| FP-33 | Demo and fixture verified | At the 400%-equivalent 392×197 viewport, routine sample uploads stay in the count and a folder remains keyboard-openable. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-33.json). Actual 400% zoom plus a fixture with active uploads. | 1/1 passed |
| FP-34 | Verified in fixture |  Fixture loads a second page with keyboard focus, cancellation, error and retry variants. | 6/6 passed |
| FP-35 | Demo and fixture verified | Both footer targets measure at least 44 px, and Tab moves from Documentation to GitHub. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-35.json). Documentation and GitHub are reachable and sized. | 4/4 passed |
| FP-36 | Demo and fixture verified | Forced-colors mode keeps the active side destination and Grid underlined while an inactive link has no underline. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-36.json). Forced-colors is the browser's emulation. | 2/2 passed |
| FP-37 | Demo and fixture verified | Grid uses a measured 16:9 thumbnail and no size/date on the sampled card. List still exposes the Date Added column. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-37.json). Grid and List intentionally present different metadata. | 11/11 passed |
| FP-38 | Demo and fixture verified | Owner sees the Link opens label and numeric counts. Counting and backup restore are verified separately in isolated fixtures. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-38.json). Fixture verifies counter semantics; backup portability is in BUG-14's isolated Docker record. | 2/2 passed |
| FP-39 | Demo and fixture verified | Library figures are grouped in Performance Overview per the newer FP-75, apart from server readings and Backups. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-39.json). FP-75 supersedes the old backup location. | 3/3 passed |
| FP-40 | Demo and fixture verified | Settings and Connections use the same page-title type in both themes on phone. Visual preference remains owner-reviewed. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-40.json). Design preference remains subject to owner review. | 1/1 passed |
| FP-41 | Demo and fixture verified | A single dark-mode click switches Grid, flips both announced pressed states and changes the selected background. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-41.json). One-click pressed state confirmed. | 6/6 passed |
| FP-42 | Demo and fixture verified | Escape dismisses the bot help dialog while its trigger holds focus. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-42.json). Performance and Settings help tested. | 3/3 passed |
| FP-43 | Demo and fixture verified | Measured compact desktop List/Grid buttons and a 44 px phone View and sort trigger with a labelled Grid option. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-43.json). FP-67 supersedes the heavy selection underline in the ordinary theme. | 4/4 passed |
| FP-44 | Demo and fixture verified | At laptop width the chart columns share their top edge. At 390 px CPU/RAM follow the network plot and the inspect control is available. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-44.json). FP-77 adds wide bottom alignment. | 7/7 passed |
| FP-45 | Demo and fixture verified | All-time totals render directly in Tuning, with Temporary upload storage visible in Server resources. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-45.json). Tuning and Server resources expose measurements directly. | 5/5 passed |
| FP-46 | Demo and fixture verified | Focusing and inspecting a chart at laptop and phone widths leaves the document scroll position unchanged. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-46.json). Chart focus, hover and keyboard inspection checked. | 1/1 passed |
| FP-47 | Demo and fixture verified | At 320 px sample.pdf occupies one line and its 44 px touch action opens the menu. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-47.json). Short and long filenames plus usable card actions checked. | 8/8 passed |
| FP-48 | Demo and fixture verified | A 1440 px touch layout keeps the first grid row in place within one pixel on entering selection. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-48.json). 1440 px touch was emulated. | 6/6 passed |
| FP-49 | Demo and fixture verified | Measured contained Performance tabs and current network rates at 390 px and captured the typography. Aesthetic review remains with the owner. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-49.json). Visible layout and values measured; visual preference is owner-reviewed. | 2/2 passed |
| FP-50 | Demo and fixture verified | The phone All types menu contains its Documents option, filters to a PDF and hides an audio file. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-50.json). Type picker filtering and layout checked. | 5/5 passed |
| FP-51 | Demo and fixture verified | At 320 px the Connections client section has a reachable Manage action with no horizontal overflow. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-51.json). Connections changes use fixture clients. | 2/2 passed |
| FP-52 | Demo and fixture verified | Shared Links at 320×640 and Performance Accounts at 740×360 keep scrolling inside the panel and do not enlarge the root document. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-52.json). Root overflow and internal scrolling checked. | 4/4 passed |
| FP-53 | Demo and fixture verified | At 740×360 both Download Original and Close are reachable in the unsupported-file preview. Error and retry use the isolated fixture. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-53.json). Retry and alternate download checked with fixture errors. | 5/5 passed |
| FP-54 | Demo and fixture verified | Matched dark laptop and phone captures show attention and Telegram waits before charts. View Accounts works from both sizes. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-54.json). Owner visual acceptance separate. | 4/4 passed |
| FP-55 | Demo and fixture verified | Matched four library labels, a real sample count and working Open Trash. FP-75 moved the panel above the charts. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-55.json). Inventory updates with the live snapshot. | 1/1 passed |
| FP-56 | Demo and fixture verified | Server network has no slider. ArrowLeft opens one reading overlay and Escape clears it. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-56.json). One inspector overlay, pointer, keyboard and touch. | 2/2 passed |
| FP-57 | Demo and fixture verified | Visible Server resources uses Verified free disk space and does not show the old Backing storage label. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-57.json). Checked both measured and unavailable state. | 2/2 passed |
| FP-58 | Demo and fixture verified | The live demo received an automatic Performance snapshot without a Refresh button. Failure and overlap paths are isolated fixture tests. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-58.json). Automatic refresh and failure recovery checked. | 2/2 passed |
| FP-59 | Demo and fixture verified | Server resources labels are rendered without proc_meminfo. The Linux fallback path is exercised separately. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-59.json). Owner-facing labels and scope fallbacks checked. | 3/3 passed |
| FP-60 | Demo and fixture verified | Walked and captured Overview, Server resources, Tuning and Accounts at tablet width. Each has its own first-level content headings. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-60.json). Owner visual acceptance separate. | 6/6 passed |
| FP-61 | Demo and fixture verified | Phone demo Tuning has automatic tuning values and visible size units. Missing and recovery outcomes use fixture cases. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-61.json). Populated, missing and stale tuning fixtures. | 2/2 passed |
| FP-62 | Demo and fixture verified | The edit password field is comparable in height to the label field, and keyboard reveal works at 1536, 820 and 390 px. Drafts were discarded. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-62.json). Actual save uses writable isolated fixtures. | 3/3 passed |
| FP-63 | Verified in fixture |  Guest prompt, invalid password and recovery in local contexts. Production HTTPS embed cookies remain unverified. | 4/4 passed |
| FP-64 | Demo and fixture verified | Right-click opens folder actions in List and Grid. Shift+F10 opens the file menu, and Escape restores focus. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-64.json). Guest context menu is read-only. | 3/3 passed |
| FP-65 | Demo and fixture verified | The right-click workflow selects the item and reuses TGDrive’s own action menu and blue design. This is recorded as design feedback. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-65.json). Design guidance and familiar interaction, with owner visual preference separate. | 2/2 passed |
| FP-66 | Demo and fixture verified | Measured four grid columns at 1536, five at 1920, and two at 820. The sample video name fits one line at both desktop widths. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-66.json). FP-78 updates the laptop density after this audit. | 4/4 passed |
| FP-67 | Demo and fixture verified | The small Grid button switches with Enter, shows pressed state and keeps focus in both themes. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-67.json). Both themes and forced colors sampled. | 4/4 passed |
| FP-68 | Demo and fixture verified | The tools group starts collapsed, opens for Performance, and identifies the active tool when collapsed. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-68.json). Desktop, tablet and phone disclosure tested. | 3/3 passed |
| FP-69 | Demo and fixture verified | Double-clicking PDF toolbar controls left no selection. Double-clicking PDF text selected the file content. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-69.json). File content selection, keyboard interactions and actual PDF mouse selection. | 4/4 passed |
| FP-70 | Demo and fixture verified | Desktop headings and columns match within 1 CSS px for Shared item, Link dates, Link opens and Actions. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-70.json). Column alignment, protection and actions on sample links. | 3/3 passed |
| FP-71 | Verified in fixture |  Writable isolated fixture holds final requests and checks the latest position after reopen and confirmed reload. | 3/3 passed |
| FP-72 | Verified in fixture |  Verified paused resume, absence of the popup and seeking back with the player timeline. | 1/1 passed |
| FP-73 | Demo and fixture verified | Name to Date Added leaves the toolbar rectangle unchanged and restores trigger focus at 1536, 1024, 820 and 390 px. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-73.json). Tests switch through all three sort keys at four widths. | 4/4 passed |
| FP-74 | Demo and fixture verified | Measured file preview headers on laptop and phone, with the 2-row PDF layout allowed on phone. Close and Download remain reachable. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-74.json). Compact headers and actual file controls, including EPUB and comics in fixtures. | 14/14 passed |
| FP-75 | Demo and fixture verified | The original backup URL resolves to Backups and its short sidebar label fits. TGDrive library sits above the charts and is absent from Backups. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-75.json). Legacy backup route keeps working and library appears above charts. | 3/3 passed |
| FP-76 | Demo and fixture verified | Collapsed sidebar rows measure 40 px with a visible 4 px gap on hover. Expanding tools leaves Backups keyboard-reachable. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-76.json). Sidebar gaps and reachable footer checked. | 5/5 passed |
| FP-77 | Demo and fixture verified | Laptop card heights measured 382.1 px and 382.1 px with matching bottom edges. The 390 px charts remain stacked. [Demo evidence](../cache/bug-review/frontend-audit77/demo/FP-77.json). Measured both chart columns in desktop and stacked order on phone. | 3/3 passed |

Evidence count by entry. 70 demo and fixture verified, 7 verified in fixture.

All 77 entries now have local verification evidence. The fresh Chromium
suite ran 590 cases. Of those, 588 passed on the first disk-backed run.
The two guest tests with obsolete assertions, plus the preload switch's
Settings save test, passed on the updated demo build in the
[focused rerun](../cache/bug-review/frontend-audit77/fix/recheck-current.log).
The initial RAM-backed attempt hit the fixture disk reserve before some
uploads could start. The disk-backed run passed the affected compatible
playback and demo-access cases.

Physical phones, physical keyboards and screen readers, live Telegram
delivery, the hosted MKV and deployed cross-site HTTPS credentials remain
outside this local audit. Owner visual review remains open.

### FP-78 Five cards per row

**Implemented.** The shared Explorer grid fits five columns at both 1536 and
1920 px, including sparse folders and the loading skeleton. Updated
[FileBrowser.css](../website/src/features/files/FileBrowser.css), the existing
[grid regression](../tests/browser/test_browser_file_views.py), and the
[usage guide](../docs/usage.md).

**Verified locally.** The running Chromium demo rendered five columns at
1536×695 and 1920×870, four at 1280×695, and two at 820×1180 and
390×844. The laptop sample filename wraps onto two full lines. Thumbnails
kept their 16:9 framing. Menus opened by click or tap, Escape restored focus,
and the page stayed within the viewport. The production frontend build
and [16 focused Chromium cases](../cache/bug-review/grid-five/after/pytest.log)
passed, including matching sparse and loading grids in both themes.

[1536 px before](../cache/bug-review/grid-five/before/grid-1536.png) ·
[1536 px after](../cache/bug-review/grid-five/after/grid-1536.png) ·
[390 px after](../cache/bug-review/grid-five/after/grid-390.png) ·
[Measurements](../cache/bug-review/grid-five/after/measurements.json)

**Next.** Owner visual review in the demo. Phone coverage used Chromium
touch emulation.

### FP-79 and FP-80 File focus and selection

**FP-79 implemented.** A baseline Chromium run reproduced the apparent
highlight with a focused filename and an empty browser text selection. The
Grid drew a filename outline alongside its selected-card styling. It also
moved on hover and shrank the thumbnail during selection. The filename's
keyboard focus now draws one outline around the card. Grid targets and
thumbnails keep their positions and dimensions.

**FP-80 implemented.** The UI SDK item selection model uses visible file order
in List and Grid. Shift-click replaces the selection with the anchored range.
Ctrl-click or Command-click toggles one item. Holding either toggle key with
Shift adds the range. The existing toolbar and checkboxes show the selection.
A plain desktop click reselects one item outside explicit Select mode. Escape
clears selection and returns focus to the item. Public links still use their
read-only file actions.

Changed [FileBrowser.tsx](../website/src/features/files/FileBrowser.tsx),
[FileBrowser.css](../website/src/features/files/FileBrowser.css),
[explorer tests](../tests/browser/test_browser_file_views.py),
[mixed-input tests](../tests/browser/test_browser_mobile_interactions.py), and
[the usage guide](../docs/usage.md).

**Verified locally.** The production frontend build passed. The running demo
passed range selection, individual toggles, and keyboard focus at 1536×695 dark
and 820×1180 light. At 390×844, touch checkboxes and Done worked without
horizontal overflow. The [activation regression](../cache/bug-review/file-multiselect/activation.log)
covered desktop double-click, Enter, and mobile tap. The [75-case Explorer and
phone run](../cache/bug-review/file-multiselect/final.log) passed 74 cases. The
remaining desktop mixed-input case had the previous Ctrl-click expectation,
and the [updated case passed](../cache/bug-review/file-multiselect/mixed-input.log).
After moving the outline inward to keep the first column visible on tablet,
[eight focused cases passed](../cache/bug-review/file-multiselect/polish/pytest.log),
including forced colors, keyboard focus in both themes, and both file views.

[Before](../cache/bug-review/file-multiselect/before.png) ·
[Laptop](../cache/bug-review/file-multiselect/after-1536.png) ·
[Tablet](../cache/bug-review/file-multiselect/after-820.png) ·
[Phone](../cache/bug-review/file-multiselect/after-390.png) ·
[Demo checks](../cache/bug-review/file-multiselect/demo.log)

**Next.** Owner visual review in the local demo. Command-key events and touch
were exercised in Chromium. A physical Mac and phone were outside this pass.
