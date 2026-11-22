---
name: ui-sdk
description: Use for explicit UI SDK work or a TGDrive interaction that existing Mantine controls do not cover. Covers composing unstyled components, implementing custom behaviors, and verifying keyboard, focus, and accessibility behavior.
---

# Build with ui-sdk

ui-sdk is a library of UI behaviors: keyboard navigation, focus, selection, dismissal, popups, drag and drop and the ARIA they need. Its components are built from those behaviors and render no styles of their own, in React, Vue, Svelte, Solid and web components. `@ui-sdk/core/web.css` gives them an optional default look. When a component is missing, you build it from the same behaviors in `@ui-sdk/core`, and it gets the same keyboard, focus, server rendering and right to left support.

## Read first

TGDrive uses the React adapter. From the repo root, read
`website/node_modules/@ui-sdk/react/docs/llms.txt`. If frontend dependencies are
missing, use the [locked installation](../../../docs/development.md#install-the-development-environment).

The docs for the installed version ship in each package, as Markdown:

- `node_modules/@ui-sdk/<framework>/docs/llms.txt` is the index. Read it first. `<framework>` is `react`, `vue`, `svelte`, `solid` or `web`.
- Then read only the pages your task needs, like `docs/components/select.md`. Don't read the whole `docs/` folder; it's large.
- An interface built from our components doesn't need `docs/core.md`. Only path 2 below does, and even then you search it by heading: it's over 300 KB. `docs/rendering.md` sits next to it in the same `docs/` folder.
- Don't read compiled JavaScript in `dist/*.js`, even to debug. The docs and the `.d.ts` types are the API; when they don't answer, write a small test that tries it.

The same docs are online at https://ui-sdk.dev/docs. Prefer the installed copy: it matches the version in the project.

A page with no build step, like an Artifact, installs nothing: it loads `https://ui-sdk.dev/web.js` and `web.css` with two tags. Read https://ui-sdk.dev/web.md instead of `llms.txt`. It's the web components docs in one file, for that page, and [references/web-components.md](references/web-components.md#without-a-bundler) has the rest.

Otherwise, if no `@ui-sdk` package is installed yet, install `@ui-sdk/core` and the framework's package with the project's package manager. In a Nuxt 4 or newer app using `@ui-sdk/vue`, also add `"@ui-sdk/vue"` to `modules` in `nuxt.config.ts`.

## Pick your path

1. **Build an interface from our components**, like a settings page, a form or a command palette: read [references/build-interface.md](references/build-interface.md).
2. **Build a component or behavior ui-sdk doesn't ship**, or extend one of ours: read [references/create-component.md](references/create-component.md).

Most interfaces only need path 1. When one part of it needs a component we don't have, follow path 2 for that part.

For a requested agent integration, read [Agent integration](references/agent-ready.md).
For verification, read [Testing](references/testing.md) and follow the
[owner approval rules](../../../AGENTS.md#owner-approval-rules) before running checks.

## Rules for every path

- **Import from entries.** Each component has its own entry, like `@ui-sdk/react/select`. Import components from there, not from a package root. Never import `@ui-sdk/core/internal/*`.
- **One shape everywhere.** A root and its parts, nested as the component page's Composition shows. Change a part's element with `render`, never an `as` prop.
- **Default or controlled.** `defaultValue` to start, or `value` with `onValueChange(value, details)` to control it (each framework's idiom: `v-model` in Vue, `bind:value` in Svelte, properties and events in web components). `details` has `reason`, `source` and `cancel()`.
- **Name every control.** Use the component's label part (`FieldLabel`, `SelectLabel`, `DialogTitle`), or `aria-label` when there's no visible text. People using screen readers and agents both find controls by name.
- **Style from state.** Target `data-slot` and state attributes like `data-open` or `data-pressed`, and the CSS variables each component lists. Never inline styles for state, and never generated ids. When the app uses `web.css`, read [references/styles.md](references/styles.md) first: it draws icons and sets sizes you can't see in the markup.
- **Declare the ui-sdk layer.** Put `@layer ui-sdk;` at the top of the global stylesheet, above `@import "tailwindcss"` if there is one.
- **Check color.** Text needs 4.5:1 against what's behind it, and control edges, focus rings and state fills 3:1. jsdom can't see color, so check it in a real browser.
- **Key lists by value or id**, never by index.
- **Right to left needs no provider.** Components read the direction from the page.
- **Comments are rare.** Write one only when a reader would get the code wrong without it, like a browser quirk or a trade-off, in one or two lines. Never comment what the code does, what a value is, or which section starts here, and no JSDoc blocks on app code.
- **Don't hand-roll what we ship.** Before you write any `role=` attribute, look for the component in the table in [references/build-interface.md](references/build-interface.md). A `div` with `role="listbox"`, `"menu"`, `"dialog"`, `"tablist"`, `"separator"`, `"switch"`, `"slider"`, `"radiogroup"`, `"toolbar"` or `"tree"` next to an installed ui-sdk package is almost always a bug.

## When you're done

Use TGDrive's [validation commands](../../../docs/development.md#validation-commands)
for the affected behavior. Report the checks that ran and any unverified states.
