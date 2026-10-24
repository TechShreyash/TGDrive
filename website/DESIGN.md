# TGDrive interface design

[Product](PRODUCT.md) · [Frontend contracts](../docs/development.md#frontend-contracts) · [Docs home](../docs/README.md)

## Product and reference

TGDrive is a personal file manager backed by Telegram. Everyday file access,
uploads, playback and owner maintenance take precedence over visual decoration.
The deployed demo is the approved visual reference; use the
[demo link and public password](../docs/usage.md#try-the-public-demo) to sign in.
Preserve Inter, Lucide icons, neutral surfaces and restrained blue
actions. The user rejected the navy/indigo experiment and excessive badges/prose.
**Light and dark themes** supersede the earlier single-theme direction.

<details>
<summary>File-manager reference capture</summary>

![TGDrive file browser with its toolbar, sidebar and original demo files](../docs/assets/drive-overview.png)

See [List and Dark Grid captures](../docs/showcase.md#browse-and-organize) for
alternate views. Follow the [media guide](../docs/assets/README.md) when updating captures.

</details>

## Structure

One working surface adjoins the window and sidebar. Search, title and useful
actions share a compact toolbar; the list scrolls below fixed controls. No rounded
outer frame or empty bottom action strip.

Keep everyday destinations visible. **Tools & settings** folds rare destinations:
closed on file routes, open for an active tool. Monitor, Configuration and
Management are siblings in one indented scope with a subtle 1px rule and consistent
headings, icons and spacing. The 232px desktop sidebar uses compact, single-line
rows; all content, including Documentation and Log Out, scrolls together.

Documentation and an Octocat GitHub link share a quiet support row. Give the link
an accessible name and 44px target; avoid promotional navigation or star requests.

| Spacing | Rule |
| --- | --- |
| Related analytics/settings | One subtle border, 12px corners; no nested decorative cards |
| Section padding / gaps | 16px / 16–20px |
| Labels, help and controls | 8–12px |
| Range availability note → graph | At least 16px; chart details stay inside the graph section |

<details>
<summary>Performance and Preloads layouts</summary>

- **Performance:** Overview owns traffic/lifetime totals; Accounts owns capacity
  and queue; Resources owns CPU/RAM/storage. Do not duplicate totals. Show missing
  measurements and uncertainty. Tooltips follow the pointer inside the plot;
  retain keyboard/touch sample inspection.
- **Preloads:** one capacity panel pairs RAM/disk meters with used/limit values.
  Names, status and bytes anchor full-width segmented coverage bars; actions sit
  at the edge. Avoid tiny inline summaries or short fixed-width coverage marks.
  Use the available workspace width with the shared page gutters on desktop and
  phones, and keep About preloading folded.

</details>

## Brand mark

Use the interlocking **TG** monogram and TGDrive wordmark: a broad T bar with
curved stem, and a forward-facing G with open right side and inward crossbar.
Preserve their openings and proportions. Cobalt `#3157FF` pairs with charcoal
`#181B20` lettering on light surfaces and light lettering on dark. This replaces
the storage cube and folder/paper-plane identities.

The transparent [SVG masters](public/static/brand/) are canonical:

| Asset | Use |
| --- | --- |
| `tgdrive-logo.svg` | Horizontal lockup on light surfaces, ≥160px wide |
| `tgdrive-logo-dark.svg` | Horizontal lockup on dark surfaces, ≥160px wide |
| `tgdrive-icon.svg` | Padded monogram for compact 16–48px placements |

The existing `public/static/tgdrive-symbol.svg` and `public/static/tgdrive.svg`
URLs remain compatibility aliases for the current monogram.

Use `BrandLogo` for app branding and `BrandMark` for decorative icons. The same
artwork serves docs, favicons, app icons and the 1200 × 630 social preview. The
wordmark uses paths, independent of fonts. README's `<picture>` selects a GitHub
light/dark lockup with the light version as fallback.

Preserve aspect ratio, transparency and clear space, including icon padding.
Do not use live-text wordmarks, stretch/crop the mark, close the G aperture, add
cubes/planes or apply gradients, shadows or badges. Generate raster exports with
`scripts/brand-assets.py` from the SVGs.

Keep Inter, semantic action colors, surfaces and controls: logo cobalt does not
replace the interface palette.

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

Use semantic tokens, not raw hex in JSX. Self-hosted Inter uses 14px body/controls,
12–13px secondary details, 16px section headings and 24px titles; measurements use
tabular numerals. Retain native scale/zoom. Target 4.5:1 text contrast and 3:1 for
meaningful chart/control boundaries.

Mantine Core/Hooks 9.7.3 supply shared controls, fieldsets, tabs, alerts and surfaces;
retain the native modal wrapper and custom player. Mantine/native components share
one theme preference. Keep variables in bundled external CSS: CSP blocks injected
style elements. **Do not weaken CSP.**

## Content and states

- Keep immediate consequences beside fields; link deep explanations to the
  configuration guide. No default badges, repeated labels, broad prose or status dots.
- Keep Restore defaults available. Retain invalid drafts with actionable errors;
  owner saves affect only the visible category.
- Upload maximum, staging budget and disk reserve remain separate, validated
  together and checked again at admission.
- Explicit data refresh/reload controls show pending feedback and stay disabled until
  their fresh requests finish. Confirm actual success with the shared notice;
  failures and cancelled/stale work must not announce success. Polling stays silent.

Transfers orders Running → Queued → Finished, with outcome filters. Empty or
single-page history has no pager; retain a way back from later pages. Use safe
local demo content to review meaningful states.

## Motion and review

Motion answers an action or reports progress: 120ms menus, 140ms control changes
and truthful upload interpolation. No tab entrance, staggered rows, hover
translation, bounce or reveal delay. Respect reduced motion; correctness must
not depend on animation completion.

- [ ] Inspect populated Files, Transfers, Preloads, Performance and configuration.
- [ ] Review both themes on desktop and small mobile layouts.
- [ ] Check focus, theme persistence, search/filters, chart pointer alignment,
  clipping, errors and empty states. A palette change alone is not a UX improvement.

This direction applies Impeccable, Anthropic frontend-design, Uncodixfy and UI/UX Pro
Max. User constraints override conflicting generic skill preferences: existing
blue actions and useful rounded section boundaries remain deliberate TGDrive traits.
