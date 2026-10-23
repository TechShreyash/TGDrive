import type { MoviElement } from "movi-player/element/slim";

type PresentationPlayer = {
  shadowRoot: ShadowRoot | null;
  player?: { isSoftwareDecoding?: () => boolean } | null;
  buildSettingsRoot(): void;
  syncSettingsRootState(root: ShadowRoot): void;
  updateAudioTrackMenu(): void;
  updateSubtitleTrackMenu(): void;
  updateQualityMenu(): void;
  getMaxAllowedRate?(): number;
  openSettingsPage(page: string): void;
  closeSettingsPage(animate?: boolean): void;
  closeSettingsMenu(): void;
  _settingsPage: string | null;
  renderSettingsChoices(page: string): string;
  updateContextMenuContent(menu: HTMLElement): void;
  syncShortcutsPanel(): void;
  controlTipInfo(control: HTMLElement): { text: string; key: string } | null;
  showOSD(icon: string, text: string): void;
  contextMenuRoot(): ShadowRoot;
  isAnyMenuOpen(): boolean;
  isControlAvailable(control: string): boolean;
  updateAmbientUI(): void;
  syncTinyLayout(): void;
  showControls(): void;
  hideControls(): void;
  closeAllBottomMenus(): void;
  focus(options?: FocusOptions): void;
};

const toggleCopy = {
  ambient: {
    label: "Video glow",
    description: "Adds a soft glow using the video’s colors.",
  },
  stable: {
    label: "Reduce loud sounds",
    description: "Softens sudden changes in volume.",
  },
  crop: {
    label: "Crop black bars",
    description: "",
  },
  loop: {
    label: "Repeat video",
    description: "",
  },
} as const;

const fitCopy: Record<string, { label: string; description: string }> = {
  contain: { label: "Fit", description: "Shows the whole video." },
  cover: {
    label: "Fill",
    description: "Fills the player; edges may be cropped.",
  },
  fill: {
    label: "Stretch",
    description: "Fills the player; shapes may look wider or taller.",
  },
  zoom: {
    label: "Zoom",
    description: "Enlarges the video and crops the edges.",
  },
};

function setText(element: Element | null | undefined, text: string) {
  if (element && element.textContent !== text) element.textContent = text;
}

function describe(label: HTMLElement, description: string, id: string) {
  let hint = label.querySelector<HTMLElement>(".advanced-setting-description");
  if (!hint) {
    hint = document.createElement("span");
    hint.className = "advanced-setting-description";
    label.append(hint);
  }
  hint.id = id;
  setText(hint, description);
  label.closest("button")?.setAttribute("aria-describedby", id);
}

function clarifySettings(player: PresentationPlayer) {
  const root = player.shadowRoot;
  if (!root) return;
  for (const [toggle, copy] of Object.entries(toggleCopy)) {
    const row = root.querySelector<HTMLElement>(
      `.movi-settings-row[data-toggle="${toggle}"]`,
    );
    const label = row?.querySelector<HTMLElement>(".movi-settings-row-label");
    if (!row || !label) continue;
    setText(label, copy.label);
    row.setAttribute("aria-label", copy.label);
    if (copy.description)
      describe(label, copy.description, `advanced-setting-${toggle}`);
  }
  setText(
    root.querySelector(
      '.movi-settings-row[data-page="aspect"] .movi-settings-row-label',
    ),
    "Video fit",
  );
  setText(
    root.querySelector(".movi-settings-section-label-secondary"),
    "Picture and sound",
  );
}

function compactPlayer(player: PresentationPlayer) {
  const width = player.shadowRoot?.host.clientWidth || 0;
  return width > 0 && width <= 720;
}

/** Keep secondary actions behind the same Settings button on small players. */
function updateCompactActions(player: PresentationPlayer) {
  const shadow = player.shadowRoot;
  const root = shadow?.querySelector(".movi-settings-root");
  if (!shadow || !root) return;
  const compact = compactPlayer(player);
  const actions = [
    ["pip", "Picture in picture", ".movi-pip-btn"],
    ["chapters", "Chapters", 'button[data-custom-control="tgdrive-chapters"]'],
  ];
  for (const [action, label, selector] of actions) {
    const control = shadow.querySelector<HTMLButtonElement>(selector);
    let row = root.querySelector<HTMLButtonElement>(
      `[data-advanced-action="${action}"]`,
    );
    if (
      !compact ||
      !control ||
      control.disabled ||
      control.style.display === "none"
    ) {
      row?.remove();
      continue;
    }
    if (row) continue;
    row = document.createElement("button");
    row.type = "button";
    row.className = "movi-settings-row";
    row.dataset.advancedAction = action;
    row.setAttribute("aria-label", label);
    const icon = control.querySelector("svg")?.cloneNode(true) as
      SVGElement | undefined;
    if (icon) {
      icon.classList.add("movi-settings-icon");
      icon.setAttribute("aria-hidden", "true");
      row.append(icon);
    }
    const text = document.createElement("span");
    text.className = "movi-settings-row-label";
    text.textContent = label;
    row.append(text);
    row.addEventListener("click", (event) => {
      event.stopPropagation();
      player.closeSettingsMenu();
      player.focus({ preventScroll: true });
      control.click();
    });
    root.insertBefore(
      row,
      root.querySelector(".movi-settings-section-label-secondary"),
    );
  }
}

export function refreshVideoActions(player: MoviElement) {
  updateCompactActions(player as unknown as PresentationPlayer);
}

const nativeChoiceClasses = [
  "movi-speed-item",
  "movi-audio-track-item",
  "movi-subtitle-track-item",
  "movi-quality-item",
];
const nativeChoiceSelector = nativeChoiceClasses
  .map((name) => "." + name)
  .join(",");
const settingsControlSelector =
  'button:not(:disabled),[role="button"][tabindex="0"]';

function nativeChoiceKey(choice: HTMLElement) {
  return JSON.stringify([
    nativeChoiceClasses.find((name) => choice.classList.contains(name)),
    choice.dataset.speed,
    choice.dataset.trackId,
    choice.dataset.audioLang,
    choice.dataset.subtitleLang,
    choice.dataset.subtitlePick,
    choice.dataset.src,
  ]);
}

function focusedNativeChoice(player: PresentationPlayer) {
  const focused = player.shadowRoot?.activeElement;
  return focused instanceof HTMLElement &&
    focused.matches(nativeChoiceSelector) &&
    focused.closest(".movi-settings-page-body")
    ? nativeChoiceKey(focused)
    : undefined;
}

/** Preserve Movi's click handlers while making its native div choices operable. */
function updateNativeChoices(player: PresentationPlayer, focusedKey?: string) {
  const shadow = player.shadowRoot;
  if (!shadow) return;
  const maximumRate = player.getMaxAllowedRate?.();
  for (const choice of shadow.querySelectorAll<HTMLElement>(
    nativeChoiceSelector,
  )) {
    const unavailable =
      choice.style.pointerEvents === "none" ||
      (choice.dataset.speed !== undefined &&
        maximumRate !== undefined &&
        Number(choice.dataset.speed) > maximumRate);
    const selected = choice.matches(
      ".movi-speed-active,.movi-audio-track-active,.movi-subtitle-track-active,.movi-quality-active",
    );
    choice.setAttribute("role", "button");
    choice.tabIndex = unavailable ? -1 : 0;
    choice.setAttribute("aria-disabled", String(unavailable));
    if (choice.dataset.subtitlePick) choice.removeAttribute("aria-pressed");
    else choice.setAttribute("aria-pressed", String(selected));
  }
  if (!focusedKey || !shadow.querySelector(".movi-settings-menu.is-open"))
    return;
  const choices = Array.from(
    shadow.querySelectorAll<HTMLElement>(
      '.movi-settings-page-body [role="button"][tabindex="0"]',
    ),
  ).filter((choice) => choice.getClientRects().length);
  (
    choices.find((choice) => nativeChoiceKey(choice) === focusedKey) ||
    choices.find((choice) => choice.getAttribute("aria-pressed") === "true") ||
    choices[0]
  )?.focus({ preventScroll: true });
}

function clarifyFitChoices(player: PresentationPlayer) {
  const root = player.shadowRoot;
  if (!root) return;
  for (const choice of root.querySelectorAll<HTMLElement>(
    ".movi-settings-choice[data-fit]",
  )) {
    const copy = fitCopy[choice.dataset.fit || ""];
    const main = choice.querySelector<HTMLElement>(
      ".movi-settings-choice-main",
    );
    const label = main?.querySelector<HTMLElement>("span");
    if (!copy || !label) continue;
    setText(label, copy.label);
    label.classList.add("advanced-setting-choice-label");
    choice.setAttribute("aria-label", copy.label);
    describe(label, copy.description, `advanced-fit-${choice.dataset.fit}`);
  }
}

const contextLabels: Record<string, string> = {
  "ambient-toggle": "Video glow",
  "stable-audio-toggle": "Reduce loud sounds",
  "loop-toggle": "Repeat video",
  fit: "Video fit",
};

function clarifyContext(menu: ParentNode) {
  for (const [action, label] of Object.entries(contextLabels))
    setText(
      menu.querySelector(`[data-action="${action}"] .movi-context-menu-label`),
      label,
    );
}

const osdLabels: Record<string, string> = {
  "Ambient Mode On": "Video glow on",
  "Ambient Mode Off": "Video glow off",
  "Stable Volume On": "Reduce loud sounds on",
  "Stable Volume Off": "Reduce loud sounds off",
  "Loop On": "Repeat video on",
  "Loop Off": "Repeat video off",
};
const toggleMessages = new Set([
  ...Object.keys(osdLabels),
  "Black Bars Cropped",
  "Black Bars Kept",
  "HDR On",
  "HDR Off",
]);
const installed = new WeakSet<object>();

export type VideoQualityControl = {
  variants: { id: string; label: string }[];
  selected: string;
  onChange: (id: string) => void;
};
const videoQualities = new WeakMap<PresentationPlayer, VideoQualityControl>();
type QualityView = {
  list: HTMLElement;
  nativeList: HTMLElement | null;
  nativeHidden: HTMLElement["hidden"];
  signature: string;
};
const qualityViews = new WeakMap<PresentationPlayer, QualityView>();

function qualityOptions(control: VideoQualityControl) {
  const seen = new Set(["original"]);
  return [
    { id: "original", label: "Original" },
    ...control.variants.filter((variant) => {
      if (seen.has(variant.id)) return false;
      seen.add(variant.id);
      return true;
    }),
  ];
}

function updateQualityRow(player: PresentationPlayer) {
  const root = player.shadowRoot?.querySelector(".movi-settings-root");
  if (!root) return;
  const control = videoQualities.get(player);
  let row = root.querySelector<HTMLElement>('[data-page="quality"]');
  if (!control?.variants.length) {
    if (row?.classList.contains("is-static") || row?.dataset.advancedQuality)
      row.remove();
    return;
  }
  if (!(row instanceof HTMLButtonElement)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "movi-settings-row";
    button.dataset.page = "quality";
    if (row) {
      button.append(...Array.from(row.childNodes));
      row.replaceWith(button);
    } else {
      const icon = player.shadowRoot
        ?.querySelector(".movi-quality-btn svg")
        ?.cloneNode(true) as SVGElement | undefined;
      if (icon) {
        icon.classList.add("movi-settings-icon");
        button.append(icon);
      }
      const label = document.createElement("span");
      label.className = "movi-settings-row-label";
      label.textContent = "Quality";
      const value = document.createElement("span");
      value.className = "movi-settings-row-value";
      button.append(label, value);
      root.insertBefore(button, root.querySelector(".movi-settings-row"));
    }
    row = button;
  }
  row.dataset.advancedQuality = "true";
  row.removeAttribute("aria-disabled");
  row.setAttribute("aria-label", "Quality");
  if (!row.querySelector(".movi-settings-chevron")) {
    const chevron = root.querySelector(
      '.movi-settings-row[data-page="speed"] .movi-settings-chevron',
    );
    if (chevron) row.append(chevron.cloneNode(true));
  }
  setText(
    row.querySelector(".movi-settings-row-value"),
    qualityOptions(control).find((option) => option.id === control.selected)
      ?.label || "Original",
  );
}

function restoreQualityView(player: PresentationPlayer) {
  const view = qualityViews.get(player);
  if (!view) return;
  view.list.remove();
  if (view.nativeList) view.nativeList.hidden = view.nativeHidden;
  qualityViews.delete(player);
}

function updateQualityChoices(player: PresentationPlayer) {
  const control = videoQualities.get(player);
  const body = player.shadowRoot?.querySelector(".movi-settings-page-body");
  if (!control?.variants.length || !body) return;
  let view = qualityViews.get(player);
  if (!view) {
    const list = document.createElement("div");
    list.className = "advanced-quality-list";
    list.setAttribute("role", "group");
    list.setAttribute("aria-label", "Video quality");
    const nativeList = body.querySelector<HTMLElement>(".movi-quality-list");
    view = {
      list,
      nativeList,
      nativeHidden: nativeList?.hidden || false,
      signature: "",
    };
    if (nativeList) nativeList.hidden = true;
    body.append(list);
    qualityViews.set(player, view);
  }
  const options = qualityOptions(control);
  const signature = JSON.stringify(options);
  if (view.signature !== signature) {
    const focused = player.shadowRoot?.activeElement as HTMLElement | null;
    const focusedId = view.list.contains(focused)
      ? focused?.dataset.qualityId
      : undefined;
    view.list.replaceChildren();
    for (const option of options) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "movi-settings-choice advanced-quality-choice";
      button.dataset.qualityId = option.id;
      const label = document.createElement("span");
      label.textContent = option.label;
      const check = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "svg",
      );
      check.classList.add("movi-settings-check");
      check.setAttribute("viewBox", "0 0 24 24");
      check.setAttribute("aria-hidden", "true");
      check.setAttribute("fill", "none");
      check.setAttribute("stroke", "currentColor");
      check.setAttribute("stroke-width", "2");
      const path = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "path",
      );
      path.setAttribute("d", "m5 12 4 4L19 6");
      check.append(path);
      button.append(label, check);
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const latest = videoQualities.get(player);
        player.closeSettingsMenu();
        player.focus({ preventScroll: true });
        if (latest && latest.selected !== option.id) latest.onChange(option.id);
      });
      view.list.append(button);
    }
    view.signature = signature;
    if (focusedId)
      Array.from(view.list.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.dataset.qualityId === focusedId)
        ?.focus({ preventScroll: true });
  }
  for (const button of view.list.querySelectorAll<HTMLButtonElement>(
    "button",
  )) {
    const selected = button.dataset.qualityId === control.selected;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
    button
      .querySelector<SVGElement>("svg")
      ?.style.setProperty("visibility", selected ? "visible" : "hidden");
  }
}

/** Server variants stay in React; this only presents them in Movi's existing menu. */
export function setVideoQualityControl(
  player: MoviElement,
  control?: VideoQualityControl,
) {
  const presentation = player as unknown as PresentationPlayer;
  if (control?.variants.length) videoQualities.set(presentation, control);
  else videoQualities.delete(presentation);
  updateQualityRow(presentation);
  if (presentation._settingsPage === "quality") {
    if (control?.variants.length) updateQualityChoices(presentation);
    else presentation.closeSettingsPage(false);
  }
}

export function clearVideoQualityControl(player: MoviElement) {
  const presentation = player as unknown as PresentationPlayer;
  videoQualities.delete(presentation);
  restoreQualityView(presentation);
  updateQualityRow(presentation);
}

/** Small presentation adapter for the pinned Movi 0.4.1 settings surfaces.
 * Apply labels when those surfaces are built, never during frame updates.
 */
export function installVideoPresentation(constructor: typeof MoviElement) {
  const prototype = constructor.prototype as unknown as PresentationPlayer;
  if (installed.has(prototype)) return;
  const buildSettings = prototype.buildSettingsRoot;
  const syncSettings = prototype.syncSettingsRootState;
  const openSettings = prototype.openSettingsPage;
  const closeSettings = prototype.closeSettingsPage;
  const renderChoices = prototype.renderSettingsChoices;
  const updateContext = prototype.updateContextMenuContent;
  const syncShortcuts = prototype.syncShortcutsPanel;
  const controlTip = prototype.controlTipInfo;
  const showOSD = prototype.showOSD;
  const controlAvailable = prototype.isControlAvailable;
  const updateAmbient = prototype.updateAmbientUI;
  const syncTiny = prototype.syncTinyLayout;
  const hideControls = prototype.hideControls;

  for (const method of [
    "updateAudioTrackMenu",
    "updateSubtitleTrackMenu",
    "updateQualityMenu",
  ] as const) {
    const update = prototype[method];
    prototype[method] = function () {
      const focused = focusedNativeChoice(this);
      update.call(this);
      updateNativeChoices(this, focused);
    };
  }

  prototype.syncTinyLayout = function () {
    syncTiny.call(this);
    if (compactPlayer(this)) {
      const shadow = this.shadowRoot;
      const right = shadow?.querySelector(".movi-controls-right");
      const fullscreen = shadow?.querySelector(".movi-fullscreen-btn");
      if (right && fullscreen && fullscreen.parentElement !== right)
        right.append(fullscreen);
      right?.classList.remove("expanded");
    }
    updateCompactActions(this);
  };
  prototype.hideControls = function () {
    const focused = this.shadowRoot?.activeElement;
    // Keyboard users must never lose the control they are operating.
    if (
      focused instanceof HTMLElement &&
      focused.matches(":focus-visible") &&
      focused.closest(".movi-controls-bar,.movi-top-controls") &&
      focused.getClientRects().length &&
      getComputedStyle(focused).visibility !== "hidden"
    )
      return;
    hideControls.call(this);
  };

  // Movi deliberately suspends glow colour sampling during software decoding.
  // Keep its settings, context menu and keyboard availability in agreement.
  prototype.isControlAvailable = function (control) {
    if (control === "ambient" && this.player?.isSoftwareDecoding?.())
      return false;
    return controlAvailable.call(this, control);
  };
  prototype.updateAmbientUI = function () {
    updateAmbient.call(this);
    if (this.player?.isSoftwareDecoding?.()) {
      const row = this.contextMenuRoot().querySelector<HTMLElement>(
        '[data-action="ambient-toggle"]',
      );
      if (row) row.style.display = "none";
    }
  };

  prototype.buildSettingsRoot = function () {
    buildSettings.call(this);
    clarifySettings(this);
    updateQualityRow(this);
    updateCompactActions(this);
  };
  prototype.syncSettingsRootState = function (root) {
    syncSettings.call(this, root);
    updateQualityRow(this);
  };
  prototype.openSettingsPage = function (page) {
    openSettings.call(this, page);
    updateNativeChoices(this);
    if (page === "quality" && videoQualities.get(this)?.variants.length)
      updateQualityChoices(this);
    if (page === "aspect") {
      setText(
        this.shadowRoot?.querySelector(".movi-settings-page-title"),
        "Video fit",
      );
      clarifyFitChoices(this);
    }
    // The native root row becomes hidden when a subpage opens. Keep focus in
    // the menu so keyboard navigation and Escape continue to reach the player.
    const choices = this.shadowRoot?.querySelectorAll<HTMLElement>(
      ".movi-settings-page-body :is(" + settingsControlSelector + ")",
    );
    const visible = Array.from(choices || []).filter(
      (button) => button.getClientRects().length,
    );
    (
      visible.find(
        (button) =>
          button.getAttribute("aria-pressed") === "true" ||
          button.classList.contains("is-active"),
      ) ||
      visible[0] ||
      this.shadowRoot?.querySelector<HTMLButtonElement>(".movi-settings-back")
    )?.focus({ preventScroll: true });
  };
  prototype.closeSettingsPage = function (animate) {
    const page = this._settingsPage;
    restoreQualityView(this);
    closeSettings.call(this, animate);
    if (page && animate !== false)
      this.shadowRoot
        ?.querySelector<HTMLButtonElement>(
          `.movi-settings-row[data-page="${page}"]`,
        )
        ?.focus({ preventScroll: true });
  };
  // Movi rebuilds an open fit page when the fit changes via keyboard.
  prototype.renderSettingsChoices = function (page) {
    const markup = renderChoices.call(this, page);
    if (page === "aspect") queueMicrotask(() => clarifyFitChoices(this));
    return markup;
  };
  prototype.updateContextMenuContent = function (menu) {
    updateContext.call(this, menu);
    clarifyContext(menu);
  };
  prototype.syncShortcutsPanel = function () {
    syncShortcuts.call(this);
    for (const [action, label] of Object.entries({
      ambient: "Video glow",
      stableaudio: "Reduce loud sounds",
      loop: "Repeat video",
      aspect: "Video fit",
    })) {
      const key = this.shadowRoot?.querySelector(
        `.movi-shortcut-row kbd[data-shortcut-action="${action}"]`,
      );
      setText(key?.parentElement?.querySelector("span"), label);
    }
  };
  prototype.controlTipInfo = function (control) {
    const result = controlTip.call(this, control);
    if (!result) return result;
    if (control.classList.contains("movi-stable-audio-btn"))
      return { ...result, text: "Reduce loud sounds" };
    if (control.classList.contains("movi-loop-btn"))
      return { ...result, text: "Repeat video" };
    if (control.classList.contains("movi-aspect-ratio-btn"))
      return { ...result, text: "Video fit" };
    return result;
  };
  prototype.showOSD = function (icon, text) {
    // A switch already shows its new state. Repeating it over the picture adds
    // noise and looks like a playback warning, especially on a short clip.
    if (toggleMessages.has(text) && this.isAnyMenuOpen()) return;
    showOSD.call(this, icon, osdLabels[text] || text);
  };
  installed.add(prototype);
}

/** Keep native button activation out of the player's global Space shortcut. */
export function attachVideoPresentation(player: MoviElement): () => void {
  const presentation = player as unknown as PresentationPlayer;
  let disposed = false;
  for (const [selector, label] of Object.entries({
    ".movi-stable-audio-btn": "Reduce loud sounds",
    ".movi-loop-btn": "Repeat video",
    ".movi-aspect-ratio-btn": "Video fit",
  }))
    player.shadowRoot
      ?.querySelector(selector)
      ?.setAttribute("aria-label", label);
  const guard = (event: KeyboardEvent) => {
    const openSettings = player.shadowRoot?.querySelector(
      ".movi-settings-menu.is-open",
    );
    const openMenu =
      openSettings ||
      player.shadowRoot?.querySelector(
        ".movi-speed-menu.is-open,.movi-audio-track-menu.is-open,.movi-subtitle-track-menu.is-open,.movi-quality-menu.is-open",
      );
    if (openMenu && (event.key === " " || event.key === "Enter")) {
      const choice = event
        .composedPath()
        .find(
          (node): node is HTMLElement =>
            node instanceof HTMLElement && node.matches(nativeChoiceSelector),
        );
      if (choice && openMenu.contains(choice)) {
        event.preventDefault();
        event.stopPropagation();
        if (
          choice.getAttribute("aria-disabled") !== "true" &&
          choice.style.pointerEvents !== "none"
        ) {
          choice.click();
          player.focus({ preventScroll: true });
        }
        return;
      }
    }
    if (
      openMenu &&
      ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)
    ) {
      const buttons = Array.from(
        openMenu.querySelectorAll<HTMLElement>(settingsControlSelector),
      ).filter(
        (button) =>
          button.getClientRects().length &&
          getComputedStyle(button).visibility !== "hidden",
      );
      if (buttons.length) {
        event.preventDefault();
        event.stopPropagation();
        const current = buttons.indexOf(
          player.shadowRoot?.activeElement as HTMLElement,
        );
        const target =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? buttons.length - 1
              : current < 0
                ? event.key === "ArrowDown"
                  ? 0
                  : buttons.length - 1
                : (current +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    buttons.length) %
                  buttons.length;
        buttons[target].focus({ preventScroll: true });
        buttons[target].scrollIntoView({ block: "nearest" });
      }
      return;
    }
    if (event.key === "Escape") {
      const settings = player.shadowRoot?.querySelector(
        ".movi-settings-menu.is-open",
      );
      if (settings || presentation.isAnyMenuOpen()) {
        event.preventDefault();
        event.stopPropagation();
        if (settings && presentation._settingsPage) {
          const page = presentation._settingsPage;
          presentation.closeSettingsPage();
          player.shadowRoot
            ?.querySelector<HTMLButtonElement>(
              `.movi-settings-row[data-page="${page}"]`,
            )
            ?.focus({ preventScroll: true });
        } else {
          presentation.closeAllBottomMenus();
          player.shadowRoot
            ?.querySelector<HTMLButtonElement>(".movi-settings-btn")
            ?.focus({ preventScroll: true });
        }
        return;
      }
    }
    if (
      (event.key === " " || event.key === "Enter") &&
      event.composedPath().some((node) => node instanceof HTMLButtonElement)
    )
      event.stopPropagation();
  };
  const retainPictureSampling = () => {
    const engine = player.player as unknown as {
      videoRenderer?: {
        enableAmbientMirror?: () => void;
        disableAmbientMirror?: () => void;
      };
    } | null;
    // The two Movi 0.4.1 switches share this renderer resource, but either
    // switch can release it while the other is still on. Reacquire the same
    // mirror without toggling the setting or resetting its current crop.
    if (player.cropbars || player.ambientMode)
      engine?.videoRenderer?.enableAmbientMirror?.();
    else engine?.videoRenderer?.disableAmbientMirror?.();
  };
  player.addEventListener("keydown", guard, true);
  const settings = player.shadowRoot?.querySelector<HTMLElement>(
    ".movi-settings-menu",
  );
  const settingsButton = player.shadowRoot?.querySelector(".movi-settings-btn");
  const syncPopup = () => {
    if (!settings) return;
    settings.id = "tgdrive-player-settings";
    settingsButton?.setAttribute("aria-controls", settings.id);
    settingsButton?.setAttribute(
      "aria-expanded",
      String(settings.classList.contains("is-open")),
    );
  };
  const menuObserver = new MutationObserver(syncPopup);
  if (settings)
    menuObserver.observe(settings, {
      attributes: true,
      attributeFilter: ["class"],
    });
  syncPopup();
  const resumeAutoHide = () =>
    queueMicrotask(() => {
      if (!disposed && player.isConnected) presentation.showControls();
    });
  player.addEventListener("focusin", resumeAutoHide);
  player.addEventListener("focusout", resumeAutoHide);
  const shadow = player.shadowRoot;
  shadow?.addEventListener("focusin", resumeAutoHide);
  shadow?.addEventListener("focusout", resumeAutoHide);
  const resize = new ResizeObserver(() => presentation.syncTinyLayout());
  resize.observe(player);
  const pictureEvents = ["ambientchange", "cropbarschange", "loadeddata"];
  for (const event of pictureEvents)
    player.addEventListener(event, retainPictureSampling);
  return () => {
    disposed = true;
    player.removeEventListener("keydown", guard, true);
    player.removeEventListener("focusin", resumeAutoHide);
    player.removeEventListener("focusout", resumeAutoHide);
    shadow?.removeEventListener("focusin", resumeAutoHide);
    shadow?.removeEventListener("focusout", resumeAutoHide);
    menuObserver.disconnect();
    resize.disconnect();
    for (const event of pictureEvents)
      player.removeEventListener(event, retainPictureSampling);
  };
}
