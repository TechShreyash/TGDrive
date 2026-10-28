// Security and lifecycle fixes for the pinned Movi 0.4.1 slim bundle.
// Keep exact input strings so upgrades fail closed for review.
export const HARDENING_PATCHES = [
    [
        "  setupMediaSession() {\n",
        "  _tgGlobalListeners = [];\n  _tgGlobalsDetached = true;\n  _tgObservers = [];\n  _tgSizeObserver = null;\n  _tgSizeFallback = null;\n  static _tgMediaActions = new Map();\n  static _tgMediaOwner = null;\n  escapeMarkup(value) {\n    return String(value ?? \"\").replace(/[&<>\"']/g, (character) => ({\n      \"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\", \"'\": \"&#39;\"\n    })[character]);\n  }\n  listenGlobal(target, type, handler, options) {\n    if (this._tgGlobalsDetached && this._hasConnected) return;\n    const capture = typeof options === \"boolean\" ? options : !!options?.capture;\n    if (this._tgGlobalListeners.some((entry) => entry.target === target && entry.type === type && entry.handler === handler && entry.capture === capture)) return;\n    this._tgGlobalListeners.push({target, type, handler, options, capture});\n    if (!this._tgGlobalsDetached) target.addEventListener(type, handler, options);\n  }\n  unlistenGlobal(target, type, handler, options) {\n    const capture = typeof options === \"boolean\" ? options : !!options?.capture;\n    target.removeEventListener(type, handler, options);\n    this._tgGlobalListeners = this._tgGlobalListeners.filter((entry) => !(entry.target === target && entry.type === type && entry.handler === handler && entry.capture === capture));\n  }\n  watchObserver(observer, target, options) {\n    this._tgObservers.push({observer, target, options});\n    if (!this._tgGlobalsDetached) observer.observe(target, options);\n  }\n  attachGlobalListeners() {\n    if (!this._tgGlobalsDetached) return;\n    this._tgGlobalsDetached = false;\n    for (const entry of this._tgGlobalListeners) entry.target.addEventListener(entry.type, entry.handler, entry.options);\n    for (const entry of this._tgObservers) entry.observer.observe(entry.target, entry.options);\n  }\n  detachGlobalListeners() {\n    this._tgGlobalsDetached = true;\n    for (const entry of this._tgGlobalListeners) entry.target.removeEventListener(entry.type, entry.handler, entry.options);\n    this._tgGlobalListeners = this._tgGlobalListeners.filter((entry) => !(entry.target === window && (entry.type.startsWith(\"pointer\") || entry.handler === this._onPseudoFsResize)));\n    for (const entry of this._tgObservers) entry.observer.disconnect();\n    this.isDragging = false;\n    this.isTouchDragging = false;\n    this._subtitleDrag = null;\n  }\n  setMediaSessionAction(action, handler) {\n    if (handler === null && MoviElement._tgMediaActions.get(action) !== this) return;\n    try {\n      navigator.mediaSession.setActionHandler(action, handler);\n      if (handler === null) MoviElement._tgMediaActions.delete(action);\n      else MoviElement._tgMediaActions.set(action, this);\n    } catch {}\n  }\n  claimMediaSession() {\n    if (MoviElement._tgMediaOwner !== this) {\n      MoviElement._tgMediaOwner?.releaseMediaSession();\n      MoviElement._tgMediaOwner = this;\n      this._mediaSessionReady = false;\n    }\n  }\n  releaseMediaSession() {\n    if (\"mediaSession\" in navigator) {\n      for (const [action, owner] of MoviElement._tgMediaActions) {\n        if (owner === this) this.setMediaSessionAction(action, null);\n      }\n      if (MoviElement._tgMediaOwner === this) {\n        navigator.mediaSession.playbackState = \"none\";\n        navigator.mediaSession.metadata = null;\n        try { navigator.mediaSession.setPositionState?.(); } catch {}\n        MoviElement._tgMediaOwner = null;\n      }\n    }\n    this._mediaSessionReady = false;\n  }\n  setupMediaSession() {\n"
    ],
    [
        "             data-track-id=\"${A3.id}\">\n          ${D2}\n          <span class=\"movi-audio-track-label\">${g3}</span>\n          ${C3 ? `<span class=\"movi-audio-track-info\">${C3}</span>` : \"\"}\n          ${w2}\n        </div>`;\n    }).join(\"\"), h2 += C2.map((A3) => `\n        <div class=\"movi-audio-track-item ${A3.active ? \"movi-audio-track-active\" : \"\"}\"\n             data-audio-lang=\"${A3.lang}\">\n          ${D2}\n          <span class=\"movi-audio-track-label\">${A3.label}</span>\n          <span class=\"movi-audio-track-info\">${A3.lang.toUpperCase()}</span>\n",
        "             data-track-id=\"${this.escapeMarkup(A3.id)}\">\n          ${D2}\n          <span class=\"movi-audio-track-label\">${this.escapeMarkup(g3)}</span>\n          ${C3 ? `<span class=\"movi-audio-track-info\">${this.escapeMarkup(C3)}</span>` : \"\"}\n          ${w2}\n        </div>`;\n    }).join(\"\"), h2 += C2.map((A3) => `\n        <div class=\"movi-audio-track-item ${A3.active ? \"movi-audio-track-active\" : \"\"}\"\n             data-audio-lang=\"${this.escapeMarkup(A3.lang)}\">\n          ${D2}\n          <span class=\"movi-audio-track-label\">${this.escapeMarkup(A3.label)}</span>\n          <span class=\"movi-audio-track-info\">${this.escapeMarkup(A3.lang.toUpperCase())}</span>\n"
    ],
    [
        "             data-track-id=\"${A3.id}\">\n          ${c2}\n          <span class=\"movi-subtitle-track-label\">${g3}</span>\n          ${C3 ? `<span class=\"movi-subtitle-track-info\">${C3}</span>` : \"\"}\n          ${G2}\n        </div>\n      `;\n    }).join(\"\"), F2 += Q2.map((A3) => `\n        <div class=\"movi-subtitle-track-item ${A3.active ? \"movi-subtitle-track-active\" : \"\"}\"\n             data-subtitle-lang=\"${A3.lang}\">\n          ${c2}\n          <span class=\"movi-subtitle-track-label\">${A3.label}</span>\n          ${A3.pending ? MoviElement.SUBTITLE_PENDING : `<span class=\"movi-subtitle-track-info\">${A3.lang.toUpperCase()}</span>`}\n",
        "             data-track-id=\"${this.escapeMarkup(A3.id)}\">\n          ${c2}\n          <span class=\"movi-subtitle-track-label\">${this.escapeMarkup(g3)}</span>\n          ${C3 ? `<span class=\"movi-subtitle-track-info\">${this.escapeMarkup(C3)}</span>` : \"\"}\n          ${G2}\n        </div>\n      `;\n    }).join(\"\"), F2 += Q2.map((A3) => `\n        <div class=\"movi-subtitle-track-item ${A3.active ? \"movi-subtitle-track-active\" : \"\"}\"\n             data-subtitle-lang=\"${this.escapeMarkup(A3.lang)}\">\n          ${c2}\n          <span class=\"movi-subtitle-track-label\">${this.escapeMarkup(A3.label)}</span>\n          ${A3.pending ? MoviElement.SUBTITLE_PENDING : `<span class=\"movi-subtitle-track-info\">${this.escapeMarkup(A3.lang.toUpperCase())}</span>`}\n"
    ],
    [
        "        return `<div class=\"movi-context-menu-item${I3 ? \" movi-context-menu-active\" : \"\"}\" data-audio-track-id=\"${A4.id}\">${g3}${Q3}</div>`;\n      }).join(\"\")), s2.length > 0 && (A3 += s2.map((A4) => `<div class=\"movi-context-menu-item${A4.active ? \" movi-context-menu-active\" : \"\"}\" data-audio-lang=\"${A4.lang}\">${A4.label} (${A4.lang.toUpperCase()})</div>`).join(\"\")), t2.innerHTML = A3, this.setupSubmenuHover(E2, t2);\n    } else Q2 && (Q2.style.display = \"none\"), E2 && (E2.style.display = \"none\"), t2 && (t2.style.display = \"none\");\n    this.updateAudioOutputMenu();\n    const h2 = this.player.getSubtitleTracks(), D2 = this.player.trackManager.getActiveSubtitleTrack(), w2 = this.player.getSubtitleLangs(), R2 = w2.some((A3) => A3.active), y2 = A2.querySelector(\".movi-context-menu-divider-subtitle\"), c2 = A2.querySelector(\".movi-context-menu-item-subtitle\"), M2 = this.contextMenuRoot().querySelector(\".movi-context-menu-submenu-subtitle\");\n    if ((h2.length > 0 || w2.length > 0) && y2 && c2 && M2) {\n      y2.style.display = \"block\", c2.style.display = \"flex\", M2.style.removeProperty(\"display\");\n      const A3 = c2.querySelector(\"svg:not(.movi-context-menu-subtitle-filled)\"), I3 = c2.querySelector(\".movi-context-menu-subtitle-filled\");\n      null !== D2 || R2 ? (A3 && (A3.style.display = \"none\"), I3 && (I3.style.display = \"block\")) : (A3 && (A3.style.display = \"block\"), I3 && (I3.style.display = \"none\"));\n      const g3 = !D2 && !R2;\n      let C3 = \"\";\n      (window.innerWidth <= 1024 || window.matchMedia(\"(pointer: coarse)\").matches) && (C3 += `<div class=\"movi-context-menu-item movi-context-menu-back\" data-action=\"back\">\n          ${io(\"chevronBack\", \"movi-context-menu-icon\")}\n          <span class=\"movi-context-menu-label\">Back</span>\n        </div>`), C3 += `<div class=\"movi-context-menu-item${g3 ? \" movi-context-menu-active\" : \"\"}\" data-subtitle-track-id=\"-1\">Off</div>`, C3 += h2.map((A4) => {\n        const I4 = D2?.id === A4.id, g4 = A4.label || A4.language || `Subtitle ${A4.id}`, C4 = [];\n        if (A4.language && !A4.label) {\n          const I5 = A4.language.length >= 2 ? A4.language.substring(0, 3).toUpperCase() : A4.language.toUpperCase();\n          C4.push(I5);\n        }\n        const B3 = C4.length > 0 ? ` (${C4.join(\" • \")})` : \"\";\n        return `<div class=\"movi-context-menu-item${I4 ? \" movi-context-menu-active\" : \"\"}\" data-subtitle-track-id=\"${A4.id}\">${g4}${B3}</div>`;\n      }).join(\"\"), C3 += w2.map((A4) => {\n        const I4 = A4.active ? \" movi-context-menu-active\" : \"\", g4 = A4.pending ? MoviElement.SUBTITLE_PENDING : ` (${A4.lang.toUpperCase()})`;\n        return `<div class=\"movi-context-menu-item${I4}\" data-subtitle-lang=\"${A4.lang}\">${A4.label}${g4}</div>`;\n",
        "        return `<div class=\"movi-context-menu-item${I3 ? \" movi-context-menu-active\" : \"\"}\" data-audio-track-id=\"${this.escapeMarkup(A4.id)}\">${this.escapeMarkup(g3)}${this.escapeMarkup(Q3)}</div>`;\n      }).join(\"\")), s2.length > 0 && (A3 += s2.map((A4) => `<div class=\"movi-context-menu-item${A4.active ? \" movi-context-menu-active\" : \"\"}\" data-audio-lang=\"${this.escapeMarkup(A4.lang)}\">${this.escapeMarkup(A4.label)} (${this.escapeMarkup(A4.lang.toUpperCase())})</div>`).join(\"\")), t2.innerHTML = A3, this.setupSubmenuHover(E2, t2);\n    } else Q2 && (Q2.style.display = \"none\"), E2 && (E2.style.display = \"none\"), t2 && (t2.style.display = \"none\");\n    this.updateAudioOutputMenu();\n    const h2 = this.player.getSubtitleTracks(), D2 = this.player.trackManager.getActiveSubtitleTrack(), w2 = this.player.getSubtitleLangs(), R2 = w2.some((A3) => A3.active), y2 = A2.querySelector(\".movi-context-menu-divider-subtitle\"), c2 = A2.querySelector(\".movi-context-menu-item-subtitle\"), M2 = this.contextMenuRoot().querySelector(\".movi-context-menu-submenu-subtitle\");\n    if ((h2.length > 0 || w2.length > 0) && y2 && c2 && M2) {\n      y2.style.display = \"block\", c2.style.display = \"flex\", M2.style.removeProperty(\"display\");\n      const A3 = c2.querySelector(\"svg:not(.movi-context-menu-subtitle-filled)\"), I3 = c2.querySelector(\".movi-context-menu-subtitle-filled\");\n      null !== D2 || R2 ? (A3 && (A3.style.display = \"none\"), I3 && (I3.style.display = \"block\")) : (A3 && (A3.style.display = \"block\"), I3 && (I3.style.display = \"none\"));\n      const g3 = !D2 && !R2;\n      let C3 = \"\";\n      (window.innerWidth <= 1024 || window.matchMedia(\"(pointer: coarse)\").matches) && (C3 += `<div class=\"movi-context-menu-item movi-context-menu-back\" data-action=\"back\">\n          ${io(\"chevronBack\", \"movi-context-menu-icon\")}\n          <span class=\"movi-context-menu-label\">Back</span>\n        </div>`), C3 += `<div class=\"movi-context-menu-item${g3 ? \" movi-context-menu-active\" : \"\"}\" data-subtitle-track-id=\"-1\">Off</div>`, C3 += h2.map((A4) => {\n        const I4 = D2?.id === A4.id, g4 = A4.label || A4.language || `Subtitle ${A4.id}`, C4 = [];\n        if (A4.language && !A4.label) {\n          const I5 = A4.language.length >= 2 ? A4.language.substring(0, 3).toUpperCase() : A4.language.toUpperCase();\n          C4.push(I5);\n        }\n        const B3 = C4.length > 0 ? ` (${C4.join(\" • \")})` : \"\";\n        return `<div class=\"movi-context-menu-item${I4 ? \" movi-context-menu-active\" : \"\"}\" data-subtitle-track-id=\"${this.escapeMarkup(A4.id)}\">${this.escapeMarkup(g4)}${this.escapeMarkup(B3)}</div>`;\n      }).join(\"\"), C3 += w2.map((A4) => {\n        const I4 = A4.active ? \" movi-context-menu-active\" : \"\", g4 = A4.pending ? MoviElement.SUBTITLE_PENDING : ` (${this.escapeMarkup(A4.lang.toUpperCase())})`;\n        return `<div class=\"movi-context-menu-item${I4}\" data-subtitle-lang=\"${this.escapeMarkup(A4.lang)}\">${this.escapeMarkup(A4.label)}${g4}</div>`;\n"
    ],
    [
        "    this.applySubtitleSettings(), \"undefined\" != typeof ResizeObserver ? new ResizeObserver(() => {\n      this._previewCapsCache = null, h2(), this.syncTinyLayout(), this.updateCanvasSize(), this.refreshChapterGaps();\n    }).observe(this) : window.addEventListener(\"resize\", () => {\n      this._previewCapsCache = null, h2(), this.syncTinyLayout(), this.updateCanvasSize();\n    }), ",
        "    this.applySubtitleSettings();\n    if (!this._tgSizeObserver && !this._tgSizeFallback) {\n      if (typeof ResizeObserver !== \"undefined\") {\n        this._tgSizeObserver = new ResizeObserver(() => {\n          this._previewCapsCache = null; h2(); this.syncTinyLayout(); this.updateCanvasSize(); this.refreshChapterGaps();\n        });\n        this.watchObserver(this._tgSizeObserver, this);\n      } else {\n        this._tgSizeFallback = () => { this._previewCapsCache = null; h2(); this.syncTinyLayout(); this.updateCanvasSize(); };\n        this.listenGlobal(window, \"resize\", this._tgSizeFallback);\n      }\n    }\n    "
    ],
    [
        "document.addEventListener(\"mousemove\", a",
        "this.listenGlobal(document, \"mousemove\", a"
    ],
    [
        "document.addEventListener(\"mouseu",
        "this.listenGlobal(document, \"mouseu"
    ],
    [
        "document.addEventListener(\"touchm",
        "this.listenGlobal(document, \"touchm"
    ],
    [
        "document.addEventListener(\"touche",
        "this.listenGlobal(document, \"touche"
    ],
    [
        "document.removeEventListener(\"cl",
        "this.unlistenGlobal(document, \"cl"
    ],
    [
        "document.addEventListener(\"click\", A",
        "this.listenGlobal(document, \"click\", A"
    ],
    [
        "document.addEventListener(\"click\", (A3) => {\n      X",
        "this.listenGlobal(document, \"click\", (A3) => {\n      X"
    ],
    [
        "document.addEventListener(\"click\", (A3) => {\n      if (!_",
        "this.listenGlobal(document, \"click\", (A3) => {\n      if (!_"
    ],
    [
        "document.addEventListener(\"click\", (A3) => {\n      c",
        "this.listenGlobal(document, \"click\", (A3) => {\n      c"
    ],
    [
        "document.addEventListener(\"click\", (A3) => {\n      if (a",
        "this.listenGlobal(document, \"click\", (A3) => {\n      if (a"
    ],
    [
        "document.addEventListener(\"fu",
        "this.listenGlobal(document, \"fu"
    ],
    [
        "document.addEventListener(\"pointerdown\", this._o",
        "this.listenGlobal(document, \"pointerdown\", this._o"
    ],
    [
        "document.addEventListener(\"co",
        "this.listenGlobal(document, \"co"
    ],
    [
        "document.addEventListener(\"click\", (A3) => {\n      if (!t",
        "this.listenGlobal(document, \"click\", (A3) => {\n      if (!t"
    ],
    [
        "window.addEventListener(\"resize\", t",
        "this.listenGlobal(window, \"resize\", t"
    ],
    [
        "window.addEventListener(\"or",
        "this.listenGlobal(window, \"or"
    ],
    [
        "window.removeEventListener(\"resize\", this._onPseudoFsResize), window.removeEventListener(\"orientationchange\", this._onPseudoFsResize), d",
        "this.unlistenGlobal(window, \"resize\", this._onPseudoFsResize), window.removeEventListener(\"orientationchange\", this._onPseudoFsResize), d"
    ],
    [
        "window.removeEventListener(\"orientationchange\", this._onPseudoFsResize), d",
        "this.unlistenGlobal(window, \"orientationchange\", this._onPseudoFsResize), d"
    ],
    [
        "window.removeEventListener(\"pointerm",
        "this.unlistenGlobal(window, \"pointerm"
    ],
    [
        "window.removeEventListener(\"pointeru",
        "this.unlistenGlobal(window, \"pointeru"
    ],
    [
        "window.removeEventListener(\"pointerc",
        "this.unlistenGlobal(window, \"pointerc"
    ],
    [
        "window.addEventListener(\"pointerm",
        "this.listenGlobal(window, \"pointerm"
    ],
    [
        "window.addEventListener(\"pointeru",
        "this.listenGlobal(window, \"pointeru"
    ],
    [
        "window.addEventListener(\"pointerc",
        "this.listenGlobal(window, \"pointerc"
    ],
    [
        "document.removeEventListener(\"keydown\", this._cuesPanelEscHandler), this._cuesPanelEscHandler = (",
        "this.unlistenGlobal(document, \"keydown\", this._cuesPanelEscHandler), this._cuesPanelEscHandler = ("
    ],
    [
        "document.addEventListener(\"keydown\", t",
        "this.listenGlobal(document, \"keydown\", t"
    ],
    [
        "document.removeEventListener(\"keydown\", this._cuesPanelEscHandler), this._cuesPanelEscHandler = n",
        "this.unlistenGlobal(document, \"keydown\", this._cuesPanelEscHandler), this._cuesPanelEscHandler = n"
    ],
    [
        "document.addEventListener(\"pointerdown\", this._s",
        "this.listenGlobal(document, \"pointerdown\", this._s"
    ],
    [
        "document.addEventListener(\"visibilitychange\", this._",
        "this.listenGlobal(document, \"visibilitychange\", this._"
    ],
    [
        "document.addEventListener(\"fr",
        "this.listenGlobal(document, \"fr"
    ],
    [
        "window.addEventListener(\"pa",
        "this.listenGlobal(window, \"pa"
    ],
    [
        "  connectedCallback() {\n    this._hasConnected = true,",
        "  connectedCallback() {\n    this.attachGlobalListeners();\n    this._hasConnected = true,"
    ],
    [
        "  disconnectedCallback() {\n    this._sourceAbort.abort();",
        "  disconnectedCallback() {\n    this.detachGlobalListeners();\n    this.stopStutterMonitor();\n    this.resetStutterHint();\n    this.stopQoeHeartbeat();\n    this.releaseMediaSession();\n    this._linkProbeAbort?.abort();\n    this._linkProbeAbort = null;\n    this._sourceAbort.abort();"
    ],
    [
        "this._captionObserver.observe(this.subtitleOverlay, { childList: true, characterData: true, subtree: true })",
        "this.watchObserver(this._captionObserver, this.subtitleOverlay, { childList: true, characterData: true, subtree: true })"
    ],
    [
        "new MutationObserver(() => this.updateTimelineArrows()).observe(w2, { childList: true })",
        "this.watchObserver(new MutationObserver(() => this.updateTimelineArrows()), w2, { childList: true })"
    ],
    [
        "new ResizeObserver(() => this.updateTimelineArrows()).observe(w2)",
        "this.watchObserver(new ResizeObserver(() => this.updateTimelineArrows()), w2)"
    ],
    [
        "new MutationObserver(A3).observe(C2, { attributes: true, attributeFilter: [\"class\"] })",
        "this.watchObserver(new MutationObserver(A3), C2, { attributes: true, attributeFilter: [\"class\"] })"
    ],
    [
        "  setupMediaSession() {\n    if (this._mediaSessionReady) return;",
        "  setupMediaSession() {\n    if (!this.isConnected) return;\n    this.claimMediaSession();\n    if (this._mediaSessionReady) return;"
    ],
    [
        "A2.setActionHandler(I3, g2);",
        "this.setMediaSessionAction(I3, g2);"
    ],
    [
        "A2.setActionHandler(I3, g3);",
        "this.setMediaSessionAction(I3, g3);"
    ],
    [
        "  updateMediaSession() {\n    if (!(\"mediaSession\" in navigator)) return;",
        "  updateMediaSession() {\n    if (!this.isConnected || !(\"mediaSession\" in navigator)) return;"
    ],
    [
        "  updateMediaSessionPosition() {\n    if (!(\"mediaSession\" in navigator)) return;",
        "  updateMediaSessionPosition() {\n    if (!this.isConnected || MoviElement._tgMediaOwner !== this || !(\"mediaSession\" in navigator)) return;"
    ],
    [
        "  updatePlaylistMediaSession() {\n    if (!(\"mediaSession\" in navigator)) return;",
        "  updatePlaylistMediaSession() {\n    if (!this.isConnected || MoviElement._tgMediaOwner !== this || !(\"mediaSession\" in navigator)) return;"
    ],
    [
        "this._autoplayPendingVisible = false, \"mediaSession\" in navigator) {",
        "this._autoplayPendingVisible = false, \"mediaSession\" in navigator && MoviElement._tgMediaOwner === this) {"
    ],
    [
        "    if (this._mediaSessionLastPos = -1, this._mediaSessionArtworkUrl = null, this.stopStutterMonitor(),",
        "    this.releaseMediaSession();\n    if (this._mediaSessionLastPos = -1, this._mediaSessionArtworkUrl = null, this.stopStutterMonitor(),"
    ],
    [
        "  async read(A2, I2) {\n    if (this.size > 0 && A2 >= this.size) return new ArrayBuffer(0);",
        "  waitForThumbnailWake(delay) {\n    const signal = this.lifetimeAbort.signal;\n    if (signal.aborted) return Promise.resolve(false);\n    if (delay === undefined && self.navigator?.onLine !== false) return Promise.resolve(true);\n    return new Promise((resolve) => {\n      let timer;\n      const finish = () => {\n        clearTimeout(timer);\n        self.removeEventListener(\"online\", finish);\n        signal.removeEventListener(\"abort\", finish);\n        resolve(!signal.aborted);\n      };\n      signal.addEventListener(\"abort\", finish, {once: true});\n      if (delay === undefined) self.addEventListener(\"online\", finish, {once: true});\n      else timer = setTimeout(finish, delay);\n    });\n  }\n  async read(A2, I2) {\n    if (this.lifetimeAbort.signal.aborted) return new ArrayBuffer(0);\n    if (this.size > 0 && A2 >= this.size) return new ArrayBuffer(0);"
    ],
    [
        "      \"undefined\" != typeof self && self.navigator && !self.navigator.onLine && (g.warn(rA, \"Network offline, waiting for connection...\"), await new Promise((A3) => {\n        const I3 = () => {\n          self.removeEventListener(\"online\", I3), A3();\n        };\n        self.addEventListener(\"online\", I3);\n      }), g.info(rA, \"Network online, resuming...\"), B3 = 0);",
        "      if (\"undefined\" != typeof self && self.navigator && !self.navigator.onLine) {\n        if (!await this.waitForThumbnailWake()) return new ArrayBuffer(0);\n        g.info(rA, \"Network online, resuming...\"); B3 = 0;\n      }\n      if (this.lifetimeAbort.signal.aborted) return new ArrayBuffer(0);"
    ],
    [
        "        await new Promise((A3) => setTimeout(A3, Q3));",
        "        if (!await this.waitForThumbnailWake(Q3)) return new ArrayBuffer(0);"
    ],
    [
        "}), this._mediaSessionReady = true;",
        "}), this._mediaSessionReady = true, this.updatePlaylistMediaSession();"
    ],
    [
        "<span class=\"movi-nerd-stats-key\">${A3}</span>\n        <span class=\"movi-nerd-stats-value\">${I3}</span>",
        "<span class=\"movi-nerd-stats-key\">${this.escapeMarkup(A3)}</span>\n        <span class=\"movi-nerd-stats-value\">${this.escapeMarkup(I3)}</span>"
    ]
];
