# TGDrive product brief

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Drive owners** set up TGDrive, keep their files in their own Telegram
  channel, and use the drive from desktop and phone browsers. They may know
  little about Telegram bots, MongoDB, or recovery.
- **Share viewers** open a file, folder, or embed from a link. They should be
  able to identify the content and find the available view or download action
  without knowing how TGDrive works.

## Product Purpose

Make files stored in Telegram easy to find, open, organize, and share. Keep
those actions quick even when transfers or other background work are running.

## Positioning

TGDrive is a self-hosted file drive. Put the current folder, its files, and
their actions first. Keep service administration easy to reach when someone
needs it.

## Operating Context

- **Files.** Keep the folder path, search, and New easy to find. Make the active
  List or Grid choice obvious. Keep long filenames from displacing file actions.
  Use 16:9 Grid previews with the filename and actions. Put dates and sizes in List.
- **Transfers.** Show the filename, state, and next action. Make failures easy
  to find and explain what the user can do about them. Keep transfer notices
  clear of file controls and phone navigation.
- **Performance.** Show what needs attention, where work is waiting when there
  is evidence, and where to go next. Keep drive inventory distinct from server
  RAM, disk, and cache. Put detailed readings beside the thing they explain.
- **Previews and shares.** Give the file most of the space. Keep Close and the
  relevant playback, reading, download, or retry controls reachable. Show share
  viewers only the actions their link allows.
- **Settings, connections, and backups.** Group controls by what the owner is
  trying to do. For a connection or recovery problem, explain what happened and
  give the next useful action.

## Capabilities and Constraints

- TGDrive supports light, dark, and system appearance. Check text, icons, and
  active states in both themes. Pair color with a label, shape, or another
  visible cue where it carries meaning.
- Optimize desktop for scanning. Keep phone navigation and touch targets
  usable, with room for menus, long filenames, and error messages.
- Telegram imposes file size and availability limits. Describe the limits
  honestly and protect access to private files, credentials, and recovery data.

## Brand Commitments

- Keep blue as the primary accent. Use shared type, spacing, and control styles
  so screens feel like one app. Use the Settings page as the reference for
  readable density and grouping.
- Write short, direct labels. For errors, say what happened and what to do next.

## Evidence on Hand

- Inspect the current local demo. The [showcase](docs/showcase.md) provides
  older reference images that may differ from the current checkout.
- For substantial UI changes, compare before and after with the same data and
  theme. Include the reported viewport when available. Use 1536×695 for the
  laptop baseline and 390×844 for a phone baseline, then exercise the affected
  action with the relevant input method.

## Product Principles

1. Protect files and make the consequences of sensitive actions clear.
2. Give users the information they need to decide what to do next.
3. Judge a change by how the actual workflow looks and works in the browser.
