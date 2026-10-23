# TGDrive interface design

## Product and reference

TGDrive is a personal file manager backed by Telegram. Everyday file access,
uploads, playback and owner maintenance take precedence over visual decoration.
The deployed original at https://tgdrive.nexgenbots.us/ is the user-approved visual
reference. The user explicitly rejected the navy/indigo experiment and excessive
badges/prose. Preserve the existing Inter type, Lucide icons, neutral surfaces and
restrained blue actions. The latest request restores **light and dark themes**;
this supersedes the earlier single-theme direction.

## Structure

The file manager is one working surface adjoining the window and sidebar. Search,
title and useful actions share a compact toolbar. The list scrolls below fixed
controls. No rounded outer frame or empty bottom action strip.

Everyday destinations stay visible in the sidebar. Rare destinations belong to
the collapsible **Tools & settings** group, closed on everyday file routes and
opened for an active tool route. Monitor, Configuration and Management are
sibling categories within one indented scope, joined by a subtle 1px rule.
Their headings, icons and link spacing follow the same hierarchy throughout.
All contents, including Documentation and Log Out, scroll together. Keep labels
on one line, compact row density and the 232px desktop sidebar.

Documentation and a small GitHub icon link share a quiet support row. The GitHub
link uses the recognizable Octocat with an accessible name and 44px target;
it is not a promotional navigation destination or a persistent star request.

Group related analytics/settings within 12px rounded sections with one subtle
border. Do not nest decorative cards. Use 16px section padding, 16–20px section
gaps, and 8–12px between labels/help/controls. Keep at least 16px between the range
availability note and graph. Related chart details stay inside their graph section.

Performance: Overview owns traffic and lifetime totals; Accounts owns capacity and
queue; Resources owns CPU/RAM and storage. Avoid duplicated totals across tabs.
Show uncertainty and missing measurements honestly. Tooltip follows the pointer
and stays inside its plot; keyboard/touch sample inspection remains available.

## Brand mark

The approved identity is the refined interlocking **TG** monogram and TGDrive
wordmark. The T has a broad upper bar and curved stem; the forward-facing G has
an open right side and inward crossbar. Keep these openings and proportions
intact. The logo uses cobalt `#3157FF` with charcoal `#181B20` lettering on light
surfaces and light lettering on dark surfaces. This replaces the storage cube
and folder/paper-plane identities.

The transparent native SVG masters live in `public/static/brand/`:

- `tgdrive-logo.svg` is the horizontal lockup for light surfaces.
- `tgdrive-logo-dark.svg` is the horizontal lockup for dark surfaces.
- `tgdrive-icon.svg` is the padded monogram for compact placements and icons.

The existing `public/static/tgdrive-symbol.svg` and `public/static/tgdrive.svg`
URLs remain compatibility aliases for the current monogram.

The wordmark is authored as paths so it does not depend on installed fonts. Use
the shared `BrandLogo` component for app branding and `BrandMark` for standalone
decorative icons, and use the same SVG artwork
for documentation, favicons, app icons and the 1200 × 630 social preview. The
root README uses a `<picture>` element to select the appropriate lockup for
GitHub's light or dark appearance, with the light version as its fallback.

Preserve the SVG aspect ratio and transparent background. Use the full lockup
at 160px wide or larger; use the monogram for compact 16–48px placements. Keep
clear space around the artwork, including the icon master's built-in padding.
Do not recreate the wordmark with live text, stretch or crop the mark, close the
G aperture, add a cube or paper plane, or apply gradients, shadows or a badge.
Regenerate raster exports from the canonical SVGs with `scripts/brand-assets.py`
instead of maintaining separate drawings.

This is a logo-only identity change. Keep the existing Inter interface type,
semantic action colors, surfaces and controls; the cobalt logo does not replace
the interface palette below.

## Tokens

| Role | Light | Dark |
| --- | --- | --- |
| Canvas | #f5f7f9 | #1c1d1f |
| Workspace | #ffffff | #242527 |
| Related section | #f5f7f9 | #2a2c2f |
| Text | #20252b | #e8eaed |
| Secondary | #616b76 | #b0b4bc |
| Boundary | #dce1e6 | #414449 |
| Action | #0877b8 | #8acbff |

Use semantic tokens for both themes, not raw hex values in JSX. Inter is already
self-hosted: 14px controls/body, 12–13px useful secondary details, 16px section
headings, 24px page titles, tabular numerals for measurements. Retain native scale
and zoom. Target 4.5:1 normal text contrast; meaningful chart/control boundaries 3:1.

Mantine Core/Hooks 9.7.3 supply shared controls, fieldsets, tabs, alerts and surfaces.
Use the existing native modal wrapper and custom player. Theme variables are in
bundled external CSS because the existing CSP blocks injected style elements.
Do not weaken the CSP. Both Mantine and native components follow one preference.

## Content and states

Keep only immediate consequences beside configuration fields. No default badges,
duplicate labels, broad explanatory paragraphs or decorative status dots. The
configuration guide owns deep explanations; Restore defaults stays available.
Invalid values must retain drafts and show actionable errors. Owner saves apply
only to the visible category. Upload maximum, staging budget and disk reserve are
separate concepts, validated together and checked again at actual admission.

No pager for empty/single-page history. Keep navigation back from later pages.
Transfers lists Running, Queued, then Finished with outcome filters. Show safe demo
content locally so layouts can be reviewed with all meaningful states.

## Motion and review

Motion answers an action or reports actual progress. Use 120ms menu appearance,
140ms control state changes and existing truthful upload progress interpolation.
No per-tab entrance, staggered data rows, hover translation, bounce or reveal delay.
Respect reduced motion and never depend on animation completion for correctness.

Inspect populated Files, Transfers, Preloads, Performance and configuration in both
themes at desktop and small mobile sizes. Check keyboard focus, theme persistence,
search/filter state, chart pointer alignment, clipping and error/empty states. A
new palette alone is not a UX improvement.

This direction applies Impeccable, Anthropic frontend-design, Uncodixfy and UI/UX Pro
Max. User constraints override conflicting generic skill preferences: existing
blue actions and useful rounded section boundaries remain deliberate TGDrive traits.
