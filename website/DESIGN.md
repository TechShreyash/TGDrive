# TGDrive design system

TGDrive is an operational workspace for an owner organizing Telegram-backed files,
monitoring transfers and administering a self-hosted installation. Everyday tasks
must be understandable from the screen. The application has **one dark theme**;
there is no appearance selector or automatic system-theme branch.

## Component foundation

Use Mantine Core + Hooks, pinned together at 9.7.3, for shared form controls,
buttons, fieldsets, switches, tabs, alerts and panel surfaces. `src/DesignSystem.tsx`
owns library defaults; `src/style.css` owns semantic colors shared with the compact
file manager and custom player; `src/design-system.css` integrates component and
specialized surfaces. Do not add another component library or duplicate a primitive
when Mantine or an appropriate native browser control already provides it.
Native dialog behavior remains in the existing tested modal wrapper; the custom
media player and range/data protocols retain their separate functional contracts.

## Visual tokens

| Role | Value | Use |
| --- | --- | --- |
| Canvas | #15171c | Sidebar and application background |
| Surface | #1e2128 | Main working surface |
| Section | #252932 | Related form fields, charts and supporting sections |
| Raised | #2c303a | Menus and table headers |
| Text | #f1f3f7 | Labels and essential values |
| Secondary | #b7bdc8 | Help and supporting data |
| Boundary | #414753 | Distinct section outlines and control boundaries |
| Action | #9bb7ff | Interactive text and selected state |
| Primary fill | #4166df | Main save/create action, with white text |
| Healthy/ready | #69dfc5 | Success and cached readiness |
| Attention | #f6cc77 | Waiting/advanced caution |
| Failure | #ffb1b9 | Errors, with an icon and text |
| Resource accent | #baabff | CPU/resource chart distinctions |

Inter Variable is self-hosted. Use 14px default body, 12–13px supporting copy,
16px section headings and 24px page headings; use tabular numerals for measurements.
The spacing rhythm is 4/8/12/16/20/24/32px. Sections have a 12px radius, controls
8px, and compact rows keep the established file-manager density.

## Structure and behavior

- The workspace meets the window edges and sidebar directly. Keep content padding
  inside the workspace; do not add an inset frame or reserve a blank bottom action
  region. Mobile action clearance belongs inside scrolling content.

- Give related fields a bounded, subtly contrasting section with a descriptive
  legend; a wall of differently bold text is not sufficient grouping.
- Keep a page title once. Put a short intro and Configuration guide link beneath
  it; keep deep explanation in docs. Inputs keep clear labels and concise help.
- Put defaults beside values as small neutral badges. Keep safe defaults distinct
  from edited drafts, saved values and environment starting values.
- Save actions live with their form. Discard and restore are separate; restore
  previews values and requires Save. Invalid changes show a visible error.
- Performance gives each metric one home: traffic/totals, Telegram capacity/queue,
  or resources. Distinguish Now, historical readings and lifetime totals.
- Sidebar navigation scrolls as one column; Documentation/Logout follow the links
  without reserving fixed space. Labels remain single-line.
- Use status icons and words with semantic colors; never rely on color alone.
- Use 140–180ms focus/hover/section transitions, measured progress and actual
  loading states. Respect reduced motion; do not animate fake transfer progress.
- Keep keyboard focus visible, mobile targets at least 44px where feasible,
  compact file controls fixed, and only file rows scrolling in the file workspace.

## Verification

Check the real built UI at desktop 1440px and mobile 390px/320px, keyboard tabs,
form errors/default restore, disabled and busy actions, empty and long-content
states, and reduced motion. Maintain functional browser regressions while
replacing appearance tests for intentionally removed features. See
[frontend contracts](../docs/development.md#frontend-contracts).

The provider does not inject CSS variables or helper styles. All theme tokens are
bundled in the external stylesheet to respect `style-src-elem self`; never add
unsafe-inline stylesheets to work around a component integration. The body and
form-control font are explicit, not only inherited from `:root`.
