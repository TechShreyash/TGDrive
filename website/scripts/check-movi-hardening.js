import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { HARDENING_PATCHES } from "./movi-player-hardening.js";

let source = await readFile(
    process.env.TGDRIVE_MOVI_SOURCE ||
        new URL("../node_modules/movi-player/dist/element.slim.js", import.meta.url),
    "utf8",
);
for (const [before, after] of [...HARDENING_PATCHES].reverse())
    source = source.replace(after, before);
for (const [before, after] of HARDENING_PATCHES) {
    assert.equal(source.split(before).length, 2, before.slice(0, 100));
    source = source.replace(before, after);
}

function method(name, start = source.indexOf("class MoviElement extends HTMLElement")) {
    const begin = source.indexOf(`  ${name}(`, start);
    assert.ok(begin >= 0, name);
    const next = /^  (?:async )?(?:get |set )?[\w$]+\(/m.exec(source.slice(begin + 3));
    return source.slice(begin, begin + 3 + next.index);
}
class Target {
    listeners = [];
    addEventListener(type, callback, options) {
        const capture = typeof options === "boolean" ? options : !!options?.capture;
        if (!this.listeners.some((e) => e.type === type && e.callback === callback && e.capture === capture))
            this.listeners.push({ type, callback, capture });
    }
    removeEventListener(type, callback, options) {
        const capture = typeof options === "boolean" ? options : !!options?.capture;
        this.listeners = this.listeners.filter((e) => !(e.type === type && e.callback === callback && e.capture === capture));
    }
    dispatch(type) {
        for (const entry of [...this.listeners])
            if (entry.type === type) entry.callback({ type });
    }
}
const windowTarget = new Target();
windowTarget.innerWidth = 1200;
windowTarget.matchMedia = () => ({ matches: false });
const documentTarget = new Target();
const actions = new Map();
const navigatorStub = {
    mediaSession: {
        metadata: null,
        playbackState: "none",
        setActionHandler(action, callback) { actions.set(action, callback); },
        setPositionState() {},
    },
};
const helperStart = source.indexOf("  _tgGlobalListeners = [];");
const helperEnd = source.indexOf("  setupMediaSession() {", helperStart);
const Player = new Function("window", "document", "navigator", "io", "g", "Co", "go", "v", `
    return class MoviElement {
        ${source.slice(helperStart, helperEnd)}
        ${["formatAudioBadge", "formatSubtitleBadge", "updateAudioTrackMenu", "updateSubtitleTrackMenu", "updateContextMenuContent", "updateNerdStats", "setupMediaSession", "updatePlaylistMediaSession", "disconnectedCallback"].map((name) => method(name)).join("\n")}
    };
`)(windowTarget, documentTarget, navigatorStub, () => "<svg></svg>", { debug() {} }, "audit", "0.4.1", "slim");

function node() {
    return {
        innerHTML: "", style: { removeProperty() {} },
        classList: { toggle() {}, contains() { return false; } },
        querySelector() { return null; }, querySelectorAll() { return []; },
    };
}
function menus() {
    const player = new Player();
    const nodes = new Map();
    player.shadowRoot = {
        querySelector(selector) {
            if (/play-pause|subtitle-customize|subtitle-browse|svg|aspect|rotate-video/.test(selector)) return null;
            if (!nodes.has(selector)) nodes.set(selector, node());
            return nodes.get(selector);
        },
        querySelectorAll() { return []; },
    };
    const label = `<img src=/audit-probe onerror=alert(1)> & "日本語"`;
    const lang = `en"><img src=/lang-probe>`;
    const tracks = [{ id: 1, label, language: lang, codec: "aac", channels: 2 }];
    const external = [{ lang, label: `${label}.srt`, active: false }];
    player.player = {
        getAudioTracks: () => tracks, getAudioLangs: () => external,
        getSubtitleTracks: () => tracks, getSubtitleLangs: () => external,
        trackManager: { getActiveAudioTrack: () => tracks[0], getActiveSubtitleTrack: () => null },
        isNativeAudioActive: () => false, hasAudibleSource: () => true,
        getState: () => "paused", isFileSource: () => true,
    };
    for (const name of ["updateUnmuteOverlay", "refreshOpenSettingsSurfaces", "syncMobileExtras", "flashSubtitleListFade", "setupSubmenuHover", "updateAudioOutputMenu", "updateHDRVisibility"])
        player[name] = () => {};
    player.getActiveSubtitleKind = () => "text";
    player.contextMenuRoot = () => player.shadowRoot;
    return { player, nodes, label, lang, tracks };
}
test("real patched audio/subtitle menus escape labels, languages and badges", () => {
    const { player, nodes, label, lang, tracks } = menus();
    player.updateAudioTrackMenu();
    player.updateSubtitleTrackMenu();
    player.updateContextMenuContent(player.shadowRoot);
    for (const selector of [".movi-audio-track-list", ".movi-subtitle-track-list", ".movi-context-menu-submenu-audio", ".movi-context-menu-submenu-subtitle"]) {
        const html = nodes.get(selector).innerHTML;
        assert.ok(html.includes("&lt;img"), selector);
        assert.ok(!html.includes("<img"), selector);
        assert.ok(!html.includes(lang), selector);
        assert.ok(html.includes("&quot;"), selector);
        assert.ok(html.includes("日本語"), selector);
    }
    assert.equal(tracks[0].label, label);
    assert.equal(tracks[0].language, lang);
});

test("global listeners and observers detach, reconnect once, and respect explicit removal", () => {
    const player = new Player();
    const target = new Target();
    let calls = 0, observed = 0, disconnected = 0;
    const callback = () => calls++;
    player.listenGlobal(target, "click", callback, true);
    player.watchObserver({ observe() { observed++; }, disconnect() { disconnected++; } }, target);
    assert.equal(target.listeners.length, 0);
    assert.equal(observed, 0);
    player.attachGlobalListeners();
    player._hasConnected = true;
    for (let i = 0; i < 10; i++) {
        player.attachGlobalListeners();
        target.dispatch("click");
        player.detachGlobalListeners();
        assert.equal(target.listeners.length, 0);
        target.dispatch("click");
        player.attachGlobalListeners();
    }
    assert.equal(calls, 10);
    assert.equal(disconnected, 10);
    player.unlistenGlobal(target, "click", callback, true);
    player.detachGlobalListeners();
    player.attachGlobalListeners();
    target.dispatch("click");
    assert.equal(calls, 10);
    player.detachGlobalListeners();
    player.listenGlobal(target, "late", callback);
    assert.equal(player._tgGlobalListeners.length, 0);
});

test("disconnect stops repeating monitors and preserves another player's media actions", () => {
    const first = new Player(), second = new Player();
    for (const player of [first, second]) {
        player.isConnected = true;
        player.isControlDisabled = () => false;
        player.hasPrevious = false;
        player.hasNext = false;
    }
    first.setupMediaSession();
    const oldPlay = actions.get("play");
    second.setupMediaSession();
    const activePlay = actions.get("play");
    assert.notEqual(oldPlay, activePlay);
    navigatorStub.mediaSession.metadata = { title: "Second player" };
    let stutterStopped = 0, heartbeatStopped = 0, destroyed = 0;
    Object.assign(first, {
        _sourceAbort: new AbortController(), _roundingTimers: new Set(),
        canvas: new Target(), eventHandlers: new Set(), _pickedSubtitleUrls: [],
        stopStutterMonitor() { stutterStopped++; }, resetStutterHint() {},
        stopQoeHeartbeat() { heartbeatStopped++; }, stopResumeSaving() {},
        stopAmbientColorSampling() {}, setPipUiTimer() {}, cancelAutoAdvance() {},
        player: { destroy() { destroyed++; } },
    });
    first.disconnectedCallback();
    assert.equal(stutterStopped, 1);
    assert.equal(heartbeatStopped, 1);
    assert.equal(destroyed, 1);
    assert.equal(actions.get("play"), activePlay);
    assert.equal(navigatorStub.mediaSession.metadata.title, "Second player");
    second.releaseMediaSession();
    assert.equal(actions.get("play"), null);
    assert.equal(navigatorStub.mediaSession.metadata, null);
});

test("all element-owned document/window registrations use lifecycle cleanup", () => {
    const element = source.slice(source.indexOf("class MoviElement extends HTMLElement"));
    assert.doesNotMatch(element, /(?<![.$\w])(?:document|window)\.addEventListener\(/);
});

const thumbStart = source.indexOf("class ThumbnailHttpSource");
const selfTarget = new Target();
selfTarget.navigator = { onLine: false };
const Thumbnail = new Function("self", "g", "rA", "hA", "fetch", `return class {
    spendPreviewRequestBytes() { return true; }
    ${method("waitForThumbnailWake", thumbStart)}
    ${method("async read", thumbStart)}
}`)(selfTarget, { debug() {}, info() {}, warn() {}, error() {} }, "thumbnail", () => new AbortController(), async () => { throw new Error("transient network failure"); });
function thumbnail() {
    const source = new Thumbnail();
    Object.assign(source, {
        lifetimeAbort: new AbortController(), size: 100, buffer: null,
        fillEnd: 0, bufferEnd: 0, headers: {}, url: "/video", isInBuffer: () => false,
    });
    return source;
}
test("closing an offline thumbnail read releases its online listener immediately", async () => {
    selfTarget.navigator.onLine = false;
    const source = thumbnail();
    const pending = source.read(0, 1);
    assert.equal(selfTarget.listeners.length, 1);
    source.lifetimeAbort.abort();
    assert.equal((await pending).byteLength, 0);
    assert.equal(selfTarget.listeners.length, 0);
    assert.equal((await source.read(0, 1)).byteLength, 0);
});
test("closing during thumbnail backoff cancels the retry wait", async () => {
    selfTarget.navigator.onLine = true;
    const source = thumbnail();
    const pending = source.read(0, 1);
    await Promise.resolve();
    source.lifetimeAbort.abort();
    assert.equal((await pending).byteLength, 0);
});


test("stats render file-controlled language and diagnostic keys as inert text", () => {
    const player = new Player();
    const nodes = new Map();
    const root = { querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, node());
        return nodes.get(selector);
    } };
    Object.assign(player, {
        _nerdStatsVisible: true,
        offsetHeight: 400,
        networkSpeedHistory: [],
        drawNetworkGraph() {},
        player: {
            getStats: () => ({ Language: "<INPUT>", Subtitle: "srt (<INPUT>)", "<img src=/key>": 123 }),
            getNetworkSpeed: () => 0,
            isFileSource: () => false,
        },
    });
    player.updateNerdStats(root);
    const html = nodes.get(".movi-nerd-stats-body").innerHTML;
    assert.ok(html.includes("&lt;INPUT&gt;"));
    assert.ok(html.includes("&lt;img src=/key&gt;"));
    assert.ok(!/<input|<img/i.test(html));
    assert.ok(html.includes("123"));
});
