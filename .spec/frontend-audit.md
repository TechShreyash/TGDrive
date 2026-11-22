# TGDrive frontend audit and implementation plan

Audit baseline from 22 November 2026 on the local `dev` worktree. This records
observed behavior, the ordered fixes, and the checks needed to close each item.
See [the frontend tracker](frontend-polish.md) for the original requests and
[the improvement plan](improvement-plan.md) for the wider backlog.

## Baseline evidence

- The live Docker instance ran the current TGDrive frontend and Python
  backend against a private MongoDB replica set, a separate database, and a
  dedicated Telegram test channel. The disconnected recovery case used a
  separate fixture with Telegram deliberately unavailable.
- Chromium captured My Files, Recent, Starred, Trash, Transfers, Shared Links,
  Performance, Preloads, Settings, Connections, and Backups at 320×640,
  390×844, 820×1180, and 1536×695 in light and dark themes. All 88 combinations
  opened without document overflow or a recorded uncaught page error. A
  contained Explorer toolbar still clipped a control at 820px.
- Created a folder, uploaded a text file through Telegram, previewed its
  contents, and downloaded identical bytes. Trash and Restore returned the
  file to its original folder. At 1536px, Grid had five columns. Shift-click
  selected a range, Ctrl-click toggled one entry, Escape cleared selection
  without selecting browser text, and right-click opened List and Grid menus.
- Opened a file link as a guest, revoked it, and verified the link lost
  access. At 320px, a protected folder rejected the wrong password and opened
  with the correct one, with owner controls hidden.
- A 12-page PDF reopened at page 7 in a fresh phone-sized context. An MP4
  bookmark saved at 3.214 seconds reopened paused at 3.135 seconds. The MKV
  and software-decoded WMV samples rendered changing frames. Compatible
  playback on the unsupported-codec sample rendered its first frame and
  stayed paused until a second Play action.
- Invalid timezone focused the field. The corrected value persisted after
  reload and the original setting was restored. **Back Up Now** published a
  confirmed Telegram backup. The downloaded archive authenticated and
  contained catalog and database data.
- The production frontend build and 823 frontend tests passed. Of 594
  Chromium fixture tests, 593 passed initially. The resumable-upload case
  timed out waiting for an enabled transfer action in a concurrent run and
  passed in isolation. Chromium, Firefox, and WebKit passed live phone-width
  login and drawer focus return. WebKit reopened the PDF at page 7.

Local artifacts are stored in
[frontend-runtime-audit-1122](../cache/frontend-runtime-audit-1122/).
The [820px Explorer](../cache/frontend-runtime-audit-1122/files-820.png),
[phone owner pages](../cache/frontend-runtime-audit-1122/views/index-390-dark.png),
and [recovery screen](../cache/frontend-runtime-audit-1122/recovery.png)
show the conditions described below. Cache artifacts are local and ignored
by Git.

## Ordered implementation

The initial batch is UI-01 followed by UI-02. Work one item at a time. Update
the evidence and next action after each item. Preserve the existing Settings
design, Performance reading order, five-column desktop Grid, and selection
behavior.

| ID | Status | Change | Acceptance |
| --- | --- | --- | --- |
| UI-01 | Implemented, focused checks passed | **Play compatible version** returns an HTTP 200 segment and renders its first frame, then remains paused at 0:00. Carry explicit play intent through the source change. If the browser needs another gesture, surface one clearly named Play action. Keep ordinary bookmark reopening paused. | Click once on the dedicated-channel WMV. Assert advancing time and changing frames. Hold and release a refill, check audio waits for video, and verify explicit Pause is honored. |
| UI-02 | Implemented, focused checks passed | At 820px the sidebar consumed 248px, search measured 146px, the heading wrapped, and the Other filter was clipped. Switch to the compact Type control based on available space, prioritize filenames over secondary metadata, and reuse the existing drawer where the full sidebar crowds the workspace. | At 768, 820, and 1024px, hit-test every filter and reach it with a keyboard. Check Name → Date Added and List → Grid toolbar stability. Recheck five columns at 1536px and touch selection at 390px. |
| UI-03 | Implemented, focused checks passed | Backups calls local MongoDB **Cloud Database**. When Telegram is unreachable, recovery can imply a backup was found and show 0 / 0 files, bytes, and parts. Connections repeats the global disconnect banner above its diagnosis. Label MongoDB plainly, distinguish discovery from confirmed restoration, show totals when known, and give each problem one next action. | Compare healthy, unreachable, unconfirmed-pin, confirmed-backup, and restoring states. Verify which data is protected, which actions are paused, and what to do next at phone and desktop widths. |
| UI-04 | Implemented, measured locally | The direct Docker server sent 1.11 MB JavaScript and 555 KB CSS uncompressed for two entry assets before login. Start by loading Performance, Connections, and Backups on first navigation. Measure deployment compression separately. | Measure matching cold login and My Files runs under the same network profile. Verify no admin chunks before navigation, correct direct links and reloads, and usable retry after a failed chunk. |
| UI-05 | Implemented, focused checks passed | A single page of phone transfer history showed a full pagination row. Retain the count and show paging controls when another page exists. Give guests with a revoked or expired link a way to understand that they need a current link. Keep retry for transient failures. | Exercise zero, one, and multiple pages at 320px. Check active, expired, revoked, protected, and temporarily unreachable guest links without exposing private file data. |
| UI-06 | Implemented, reviewed and verified locally | Use Settings as the spacing and field-feedback reference. Give the file priority in preview layouts. Keep Performance focused on attention, measured waits, and next actions, with library totals distinct from server capacity. Reuse Mantine and the existing UI SDK interactions. | Check long names and readings, focus return, touch targets, plus loading, empty, and error states in both themes. Close and reopen PDF and video in a fresh context. Check the rendered video position against its stored bookmark and measure frame delivery separately. |
| UI-07 | First-run failures recorded | Assert settled user-visible states instead of sampling immediate DOM counts or colors. Update the superseded four-column assertion. Diagnose the transfer-action timeout under parallel load. Guard tablet filter hit targets and advancing video frames with focused tests. | Repeat each affected test, then run the integrated suite. Record first-run failures and reruns separately. Preserve per-entry evidence and open gaps. |

Use `npm run build`, `npm test`, and the affected Playwright workflows before
the integrated browser suite. For updates to reader behavior, also use the
dedicated Telegram test channel and document browser and codec. Keep the
fixture results distinct from the live workflow.

## Implementation evidence

### UI-01 · Compatible video playback

The outgoing player recorded a paused state during cleanup and replaced the
intent set by **Play compatible version**. The replacement now consumes the
explicit play intent once. Until the viewer has started watching, the start
position comes from saved progress or zero. The timestamp used to generate a
thumbnail cannot become the start position.

The new advancing-time and changing-frame regression failed on the audit
build and passed on the updated build. The dedicated-channel WMV produced
changing frames after one action at 1536×695 and 390×844. Explicit Pause held
at both sizes. The five compatible-playback fixture cases and three forced
MKV refill cases passed, including the user-pause case. The build and all 823
frontend tests passed. The [local logs](../cache/frontend-runtime-audit-1122/ui-01/)
record the focused runs. The full targeted playback run passed all 39
compatible-playback, refill, and bookmark cases.

### UI-02 · Tablet Explorer

File filters now switch to the existing Type selector when the file area is
narrow. The navigation drawer frees the workspace on smaller tablets. The
shorter tablet columns keep file sizes on one line and give filenames more
space. Existing browser layout preferences continue to apply.

The Explorer and mobile shell run passed 85 cases. After reviewing captured
screens, I widened the tablet title and kept sizes on one line. The final
build passed all 15 focused checks for filter hit targets, keyboard selection,
drawer focus return, toolbar stability, and grid density. See the [820px
Explorer](../cache/frontend-runtime-audit-1122/ui-02/final/tablet-files-820.png)
and [1024px Explorer](../cache/frontend-runtime-audit-1122/ui-02/final/tablet-files-1024.png).
The live audit container had stopped, so this pass used the built frontend and
browser fixture. Next is backup and connection status.

### UI-03 · Backup and connection status

Backups labels the database **MongoDB catalog**. Recovery distinguishes an
unconfirmed startup channel check from a discovered archive or a running
restore. It provides a restart path after checking channel access and the
original encryption key, and keeps saved-file inspection available. Progress
shows available totals. Connections shows its own diagnosis once.

The replica API and error suites passed 45 cases. The backup and Performance
browser group passed 58 cases on the initial change. The final build passed
28 backup and connection-warning checks, with unconfirmed recovery captured
at [390px](../cache/frontend-runtime-audit-1122/ui-03-final/traces/recovery-unconfirmed-390.png)
and [1536px](../cache/frontend-runtime-audit-1122/ui-03-final/traces/recovery-unconfirmed-1536.png).
The unknown-pin and interrupted restore paths were tested with a fixture.
Next is loading owner tools on demand.

### UI-04 · Initial page load

Performance, Connections, Backups, and the recovery pane load when needed.
A failed page download shows **Reload Page** and preserves the current URL.

In three cache-disabled Chromium runs at 6 Mbps download and 40 ms latency,
median sign-in screen readiness fell from 2.47 to 2.29 seconds. Cold
authenticated My Files readiness fell from 2.49 to 2.32 seconds. The initial
asset bodies totaled 1.67 MB before and 1.53 MB after. Compressing those same
assets offline with gzip yielded 425 KB and 399 KB. Proxy compression on the
deployed instance was outside the local measurement.

The production build and 63 browser cases passed, including no owner-tool
chunks on initial login or My Files, deep-link reload, and recovery from a
blocked Connections chunk. See the [local measurements](../cache/frontend-runtime-audit-1122/ui-04/).
Next is transfer history and unavailable share links.

### UI-05 · Transfer history and guest links

Transfers keeps the task count for a single page and adds paging controls for
multiple pages. Public folder links and embeds now explain when to ask the
owner for a new link. A temporary load problem still offers **Try Again**.
The unavailable screen hides the item metadata and avoids displaying raw
server messages.

The production build passed. The combined share, embed, search and transfer
suite passed 61 cases initially. Two new browser assertions needed test
adjustments for the initial React replay and embedded item labels. Both
passed in a focused rerun that includes a temporary 503, a simulated expired
410, and actual revocation. The history test captured [a single page at
320px](../cache/frontend-runtime-audit-1122/ui-05/traces/transfer-pagination-single-mobile.png).
Next is preview and diagnostics polish.

### UI-06 · Readers and diagnostics

The Office viewer announces loading while its module downloads. An unavailable
shared preview or book explains how to get a new link. This includes an
illustration fetched after a prefetched chapter, and a rejected share request
leaves the owner session intact.

The Performance and owner-surface group passed 39 cases. The [phone
Overview](../cache/frontend-runtime-audit-1122/ui-06/other-traces/performance-overview-390-light.png)
shows transfer activity and Telegram waits before separate library totals
and charts. Preview tests exercised fit, focus return and saved positions.
The reader group passed 51 cases initially and exposed the prefetched
illustration path. After the fix, the revocation and transient-image cases
both passed. The build and 824 frontend unit tests passed. See the [phone PDF
capture](../cache/frontend-runtime-audit-1122/ui-06/reader-traces/pdf-fit-controls-390.png).
Next is the timing and integration check pass.

## What remains unverified

Physical iOS and Android inputs, assistive technology, sustained playback of
the originally reported production video, and a full disaster restore
remain unverified. Archive authentication and download were checked.

## Feedback evidence by ID

The browser walkthrough directly exercised 70 of the first 77 IDs. The fixture column lists the checks rerun against the same build. The seven rows without walkthrough output use fixture evidence, with live coverage noted where available.

| ID | Fresh walkthrough or focused observation | Fixture checks and result |
| --- | --- | --- |
| FP-01 | Opened and dismissed the touch drawer, opened a folder and rendered PDF at 320×640, 390×844 and 740×360. Physical devices are outside the demo. | `test_phone_browsing_drawer_and_upload_use_separate_surfaces` (passed), `test_phone_preview_pdf_options_unsupported_and_retry_fit_viewport` (passed) |
| FP-02 | All four Performance tabs rendered. The transfer CTA was visible and View Accounts switched tabs and focused Accounts. | `test_performance_views_keep_layout_and_measured_activity` (passed), `test_overview_shows_stream_waits_but_omits_idle_cooldowns` (passed) |
| FP-03 | Light and dark owner views shared the action token, with four screens checked per theme and Overview captures. | `test_light_dark_theme_preference_persists_and_preserves_sort_keyboard` (passed), `test_brand_asset_and_sidebar_footer_stay_clear_in_both_themes` (passed) |
| FP-04 | Encrypt bot uploads switch and info button centers align. At 740×360 the help opens within the viewport and Escape restores focus. | `test_bot_encryption_help_fits_short_touch_viewport` (passed) |
| FP-05 | Backups separates database protection and recovery, shows the action and frankly labels the unconfirmed demo backup on desktop and phone. | `test_database_protection_does_not_claim_a_telegram_backup` (passed), `test_channel_backup_setup_progress_pause_errors_and_private_exports` (passed) |
| FP-06 | Opened and closed image, audio, JSON, PDF, video, unknown, EPUB, CBZ and CBR by keyboard. Office, ZIP and write/error variants require the isolated fixture run. | `test_browser_office_preview.py` (passed), `test_browser_zip_preview.py` (passed), `test_real_unsupported_video_compatible_switch_seek_and_original_progress` (passed) |
| FP-07 | All three category links open their own section under the single Settings title. Scoped saves and draft conflicts are fixture-only. | `test_admin_navigation_separates_categories_preserves_drafts_and_supports_keyboard` (passed), `test_save_only_current_category_preserves_unrelated_invalid_draft` (passed), `test_settings_save_converts_units_guards_revision_and_recovers_conflict` (passed) |
| FP-08 | Direct file/activity links and active location work. Tools is collapsed by default and expands with Enter per FP-68, the later owner request. | `test_tools_and_settings_collapses_during_file_browsing` (passed) |
| FP-09 | Fresh List, aligned named metadata headers, Grid switch and persisted Grid after reload on the populated sample folder. | `test_new_browsers_open_files_in_list_with_type_filters` (passed), `test_grid_views_preserve_selection_actions_and_bounded_thumbnails` (passed) |
| FP-10 | Dark phone login reveals the field, rejects a wrong password, returns focus and succeeds on correction. Also repeated from the in-app browser. | `test_login_failure_returns_to_password_for_correction` (passed) |
| FP-11 | At 1280×420 keyboard navigation to Documentation scrolls the sidebar and leaves the brand and root layout pinned. | `test_desktop_sidebar_scrolls_links_and_footer_with_brand_pinned` (passed) |
| FP-12 | A 320 px touch tap on the sample file checkbox selects it and leaves the current folder open. | `test_last_mobile_card_actions_clear_header_new_button` (passed), `test_single_tap_and_selection_keep_file_positions_stable` (passed) |
| FP-13 | Mouse click selects and double-click opens. A phone tap opens directly. Long hold, scroll cancellation and guest parity use isolated fixture cases. | `test_mouse_double_click_and_mobile_tap_preserve_focus` (passed), `test_touch_hold_selects_once_and_cancels_during_scroll` (passed), `test_guest_context_menu_exposes_only_read_only_actions` (passed) |
| FP-14 | A phone row exposes its full 100+ character filename in the accessible control and its Actions button stays visible. | `test_long_file_cards_and_compact_list_thumbnails` (passed) |
| FP-15 | Regional and Connections forms render within the phone viewport. Stale errors and field correction need the writable fixture run. | `test_timezone_validation_focuses_and_describes_the_field` (passed), `test_connection_validation_clears_after_credential_correction` (passed) |
| FP-16 | Opened and dismissed settings help using the keyboard with focus return. Dialog Tab cycling and validation use fixture cases. | `test_keyboard_dialog_focus_and_unicode_search` (passed), `test_admin_navigation_separates_categories_preserves_drafts_and_supports_keyboard` (passed) |
| FP-17 | At 320 px the end of the history filter strip is reachable and Cancelled responds to Enter. Synthetic transfers supply long names and held work in fixtures. | `test_transfer_columns_handle_long_names_paths_and_narrow_screens` (passed), `test_transfer_history_filters_cleared_records_and_empty_state` (passed), `test_return_to_transfers_retains_current_page_without_hidden_polling` (passed) |
| FP-18 | Shared Links opens the phone edit dialog with No expiry visible and Escape returns focus. Mutations and anonymous sessions use fixtures. | `test_public_share_stays_readonly_across_move_and_revocation` (passed), `test_independent_share_ui_and_public_reader_revocation` (passed), `test_share_mutation_supersedes_pending_manual_refresh` (passed), `test_shared_links_concise_expiry_keeps_exact_timezone` (passed) |
| FP-19 | At 320 px options fit the viewport and Escape closes options before the preview, restoring both focus targets. | `test_phone_pdf_controls_keep_pages_and_keyboard_actions_reachable` (passed), `test_pdf_real_fit_width_page_and_fullscreen` (passed) |
| FP-20 | Measured a 44 px drawer trigger and verified keyboard navigation focus return. Screen reader hardware is outside this run. | `test_cache_settings_groups_and_scoped_save` (passed), `test_forced_colors_distinguishes_active_navigation_and_selection` (passed), `test_keyboard_dialog_focus_and_unicode_search` (passed), `test_notifications_are_single_dismissible_and_usable_in_modals` (passed) |
| FP-21 | Wheel over Documentation scrolls the sidebar at 1536×695 while the logo stays fixed. | `test_desktop_sidebar_scrolls_links_and_footer_with_brand_pinned` (passed), `test_short_phone_drawer_keeps_close_visible_while_reaching_footer` (passed) |
| FP-22 | Fixture coverage in this pass. | `test_settings_guided_premium_login_cancel_code_and_two_step` (passed) |
| FP-23 | Verified Preload has a button role, Escape returns focus to the action, and changing focus to the next file closes the menu. | `test_file_options_menu_density_touch_targets_and_focus` (passed), `test_row_options_close_when_scrolled_behind_the_file_toolbar` (passed) |
| FP-24 | The enabled bot switch has an announced checked state and measured 44 px target in light and dark. Contrast and cache variants are covered separately. | `test_bot_encryption_switch_saves_truthfully_and_help_returns_focus` (passed), `test_cache_settings_groups_and_scoped_save` (passed) |
| FP-25 | Visible Server resources definition lists contain label and value elements through valid direct or grouped markup. | `test_performance_views_keep_layout_and_measured_activity` (passed) |
| FP-26 | Fixture coverage in this pass. | `test_notifications_are_single_dismissible_and_usable_in_modals` (passed) |
| FP-27 | File row y-position remains stable to within 1 px when entering phone selection mode. | `test_single_tap_and_selection_keep_file_positions_stable` (passed) |
| FP-28 | The description reference was present. The JSON region appeared after loading on follow-up. The original immediate-count assertion failed. | `test_debounced_search_restores_origin_and_document_text_stays_selectable` (passed), `test_shared_links_concise_expiry_keeps_exact_timezone` (passed) |
| FP-29 | Fixture coverage in this pass. | `test_timezone_stays_reachable_when_keyboard_reduces_viewport` (passed) |
| FP-30 | Captured the populated laptop Grid with pinned, visible wordmark and no root horizontal overflow. FP-66 supersedes the older six-column density. | `test_root_mixed_cards_keep_compact_desktop_bounds` (passed), `test_grid_fills_available_width_without_stretching_sparse_folders` (passed) |
| FP-31 | The rendered header uses TGDrive’s logo and local components. This is a design direction, with owner visual acceptance separate. | `test_root_mixed_cards_keep_compact_desktop_bounds` (passed) |
| FP-32 | Clear Cache fits a 785×394 viewport, opens the confirmation and restores focus after Escape. Actual browser zoom is checked separately. | `test_file_menu_preloads_whole_file_and_reuses_cached_bytes` (passed) |
| FP-33 | At the 400%-equivalent 392×197 viewport, routine sample uploads stay in the count and a folder remains keyboard-openable. | `test_upload_count_keeps_files_reachable_in_a_very_short_viewport` (passed) |
| FP-34 | Fixture coverage in this pass. | `test_embed_show_more_keeps_keyboard_position` (passed) |
| FP-35 | Both footer targets measure at least 44 px, and Tab moves from Documentation to GitHub. | `test_brand_asset_and_sidebar_footer_stay_clear_in_both_themes` (passed) |
| FP-36 | Forced-colors mode keeps the active side destination and Grid underlined while an inactive link has no underline. | `test_forced_colors_distinguishes_active_navigation_and_selection` (passed) |
| FP-37 | Grid uses a measured 16:9 thumbnail and no size/date on the sampled card. List still exposes the Date Added column. | `test_grid_views_preserve_selection_actions_and_bounded_thumbnails` (passed), `test_long_file_cards_and_compact_list_thumbnails` (passed) |
| FP-38 | Owner sees the Link opens label and numeric counts. Counting and backup restore are verified separately in isolated fixtures. | `test_link_opens_count_successful_entries_only` (passed), `test_root_folder_share_can_display_and_count_an_open` (passed) |
| FP-39 | Library figures are grouped in Performance Overview per the newer FP-75, apart from server readings and Backups. | `test_overview_library_and_server_resources_are_separate` (passed) |
| FP-40 | Settings and Connections use the same page-title type in both themes on phone. Visual preference remains owner-reviewed. | `test_ui_polish_batched_visual_states` (passed) |
| FP-41 | The first color sample was early. On follow-up, List and Grid swapped pressed states and settled to distinct dark-theme backgrounds. | `test_forced_colors_distinguishes_active_navigation_and_selection` (passed), `test_sort_labels_keep_file_toolbar_in_place` (passed) |
| FP-42 | Escape dismisses the bot help dialog while its trigger holds focus. | `test_performance_help_supports_keyboard_escape_and_focus_return` (passed), `test_bot_encryption_switch_saves_truthfully_and_help_returns_focus` (passed) |
| FP-43 | Measured compact desktop List/Grid buttons and a 44 px phone View and sort trigger with a labelled Grid option. | `test_right_click_and_keyboard_use_the_same_item_actions` (passed), `test_mobile_header_stays_compact_and_search_and_view_controls_work` (passed) |
| FP-44 | At laptop width the chart columns share their top edge. At 390 px CPU/RAM follow the network plot and the inspect control is available. | `test_performance_views_keep_layout_and_measured_activity` (passed), `test_isolated_network_samples_are_visible_without_bridging_gaps` (passed) |
| FP-45 | All-time totals render directly in Tuning, with Temporary upload storage visible in Server resources. | `test_performance_views_keep_layout_and_measured_activity` (passed) |
| FP-46 | Focusing and inspecting a chart at laptop and phone widths leaves the document scroll position unchanged. | `test_history_interval_hover_keyboard_poll_and_persisted_selection` (passed) |
| FP-47 | At 320 px sample.pdf occupies one line and its 44 px touch action opens the menu. | `test_long_file_cards_and_compact_list_thumbnails` (passed) |
| FP-48 | A 1440 px touch layout keeps the first grid row in place within one pixel on entering selection. | `test_single_tap_and_selection_keep_file_positions_stable` (passed) |
| FP-49 | Measured contained Performance tabs and current network rates at 390 px and captured the typography. Aesthetic review remains with the owner. | `test_live_speed_headlines_are_compact_without_rounding_to_false_zero` (passed), `test_resource_long_unknown_value_is_contained_at_320` (passed) |
| FP-50 | The phone All types menu contains its Documents option, filters to a PDF and hides an audio file. | `test_new_browsers_open_files_in_list_with_type_filters` (passed), `test_mobile_header_stays_compact_and_search_and_view_controls_work` (passed) |
| FP-51 | At 320 px the Connections client section has a reachable Manage action with no horizontal overflow. | `test_settings_connection_changes_are_write_only_verified_and_apply_live` (passed), `test_connection_validation_clears_after_credential_correction` (passed) |
| FP-52 | Shared Links at 320×640 and Performance Accounts at 740×360 keep scrolling inside the panel and do not enlarge the root document. | `test_phone_navigation_search_new_and_owner_controls_are_reachable` (passed), `test_performance_real_samples_nullable_eta_and_mobile_tables` (passed) |
| FP-53 | At 740×360 both Download Original and Close are reachable in the unsupported-file preview. Error and retry use the isolated fixture. | `test_phone_preview_pdf_options_unsupported_and_retry_fit_viewport` (passed), `test_preview_error_has_retry_and_keeps_download` (passed), `test_unsupported_details_are_compact_explicit_and_shared_readonly` (passed) |
| FP-54 | Matched dark laptop and phone captures show attention and Telegram waits before charts. View Accounts works from both sizes. | `test_overview_shows_stream_waits_but_omits_idle_cooldowns` (passed), `test_performance_missing_and_stale_samples_never_become_zero` (passed) |
| FP-55 | Matched four library labels, a real sample count and working Open Trash. FP-75 moved the panel above the charts. | `test_management_statistics_poll_only_while_resources_are_open` (passed) |
| FP-56 | Server network has no slider. ArrowLeft opens one reading overlay and Escape clears it. | `test_history_interval_hover_keyboard_poll_and_persisted_selection` (passed), `test_phone_tap_inspects_one_sample_and_tap_again_clears_it` (passed) |
| FP-57 | Visible Server resources uses Verified free disk space and does not show the old Backing storage label. | `test_primary_storage_uses_verified_envelope_not_virtual_filesystem` (passed) |
| FP-58 | The live demo received an automatic Performance snapshot without a Refresh button. Failure and overlap paths are isolated fixture tests. | `test_performance_failed_poll_preserves_context_and_recovers` (passed), `test_performance_poll_waits_for_the_pending_snapshot` (passed) |
| FP-59 | Server resources labels are rendered without proc_meminfo. The Linux fallback path is exercised separately. | `test_effective_memory_budget_is_not_a_container_hard_limit` (passed), `test_memory_scope_does_not_invent_a_container_limit` (passed) |
| FP-60 | Walked and captured Overview, Server resources, Tuning and Accounts at tablet width. Each has its own first-level content headings. | `test_performance_views_keep_layout_and_measured_activity` (passed), `test_resource_long_unknown_value_is_contained_at_320` (passed) |
| FP-61 | Phone demo Tuning has automatic tuning values and visible size units. Missing and recovery outcomes use fixture cases. | `test_showcase_tuning_reports_effective_values_and_retains_stale_reading` (passed) |
| FP-62 | The edit password field is comparable in height to the label field, and keyboard reveal works at 1536, 820 and 390 px. Drafts were discarded. | `test_share_password_edit_field_and_guest_unlock_layout` (passed) |
| FP-63 | Live 320px anonymous folder rejected the wrong password and opened with the correct one. | `test_share_password_edit_field_and_guest_unlock_layout` (passed), `test_protected_embed_fits_a_small_frame` (passed) |
| FP-64 | Right-click opens folder actions in List and Grid. Shift+F10 opens the file menu, and Escape restores focus. | `test_right_click_and_keyboard_use_the_same_item_actions` (passed), `test_guest_context_menu_exposes_only_read_only_actions` (passed) |
| FP-65 | The right-click workflow selects the item and reuses TGDrive’s own action menu and blue design. This is recorded as design feedback. | `test_right_click_and_keyboard_use_the_same_item_actions` (passed) |
| FP-66 | The older four-column assertion failed because the current Grid has five columns at 1536px. FP-78 supersedes the older expectation. | `test_grid_fills_available_width_without_stretching_sparse_folders` (passed) |
| FP-67 | The small Grid button switches with Enter, shows pressed state and keeps focus in both themes. | `test_right_click_and_keyboard_use_the_same_item_actions` (passed), `test_forced_colors_distinguishes_active_navigation_and_selection` (passed) |
| FP-68 | The tools group starts collapsed, opens for Performance, and identifies the active tool when collapsed. | `test_tools_and_settings_collapses_during_file_browsing` (passed) |
| FP-69 | Double-clicking PDF toolbar controls left no selection. Double-clicking PDF text selected the file content. | `test_preview_options_do_not_select_labels_and_file_text_remains_selectable` (passed), `test_debounced_search_restores_origin_and_document_text_stays_selectable` (passed) |
| FP-70 | Desktop headings and columns match within 1 CSS px for Shared item, Link dates, Link opens and Actions. | `test_shared_links_concise_expiry_keeps_exact_timezone` (passed), `test_same_item_links_keep_their_copy_edit_and_revoke_scope` (passed) |
| FP-71 | Live PDF restored page 7 and live MP4 reopened paused near 3.214 seconds in a fresh context. | `test_reopening_pdf_waits_for_its_last_page_to_save` (passed), `test_reopen_waits_for_pending_final_video_position` (passed), `test_owner_pdf_page_crosses_contexts_without_device_import` (passed) |
| FP-72 | Live MP4 reopened paused without the large resume action. Dedicated-channel compatible fallback required a second Play action. See UI-01. | `test_default_resume_survives_close_reload_and_waits_for_play` (passed) |
| FP-73 | Name to Date Added leaves the toolbar rectangle unchanged and restores trigger focus at 1536, 1024, 820 and 390 px. | `test_sort_labels_keep_file_toolbar_in_place` (passed) |
| FP-74 | Measured file preview headers on laptop and phone, with the 2-row PDF layout allowed on phone. Close and Download remain reachable. | `test_video_header_fullscreen_download_and_close_stay_reachable` (passed), `test_selective_reader_controls_and_mobile_layout` (passed), `test_phone_pdf_controls_keep_pages_and_keyboard_actions_reachable` (passed) |
| FP-75 | The original backup URL resolves to Backups and its short sidebar label fits. TGDrive library sits above the charts and is absent from Backups. | `test_overview_library_and_server_resources_are_separate` (passed) |
| FP-76 | Collapsed sidebar rows measure 40 px with a visible 4 px gap on hover. Expanding tools leaves Backups keyboard-reachable. | `test_sidebar_groups_tools_and_exposes_project_link` (passed), `test_desktop_sidebar_scrolls_links_and_footer_with_brand_pinned` (passed) |
| FP-77 | Laptop card heights measured 382.1 px and 382.1 px with matching bottom edges. The 390 px charts remain stacked. | `test_overview_library_and_server_resources_are_separate` (passed) |
| FP-78 | Five columns at 1536px in the live Grid. | Earlier four-column walkthrough assertion superseded. |
| FP-79 | Live Escape left browser text selection empty. | Current browser suite covers stable Grid geometry and a single visible keyboard focus outline. |
| FP-80 | Live Shift-click range, Ctrl-click toggle, Escape to clear, and right-click menus in both views. | Current browser suite covers selection behavior and focus return. |
