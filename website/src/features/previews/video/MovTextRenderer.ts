import type {
    Packet,
    SubtitleRenderer,
    SubtitleTrack,
} from "movi-player/element/slim";
import rendererStyles from "./MovTextRenderer.css?inline";

export const MOVTEXT_LIMITS = {
    packetBytes: 128 * 1024,
    cueCharacters: 16 * 1024,
    queuedCues: 4096,
    queuedCharacters: 2 * 1024 * 1024,
    visibleCues: 8,
} as const;
type TimedPacket = Pick<
    Packet,
    "streamIndex" | "timestamp" | "duration" | "data"
>;
type Cue = { start: number; end: number; text: string };

export function isMovTextCodec(codec?: string): boolean {
    return codec === "mov_text" || codec === "tx3g";
}

/** 3GPP TS 26.245: big-endian byte count, then UTF-8 or BOM-marked UTF-16.
 * Trailing style/link/font boxes are deliberately ignored: display plain text only.
 */
export function decodeMovText(data: Uint8Array): string | null {
    if (data.byteLength < 2 || data.byteLength > MOVTEXT_LIMITS.packetBytes)
        return null;
    const size = data[0] * 256 + data[1];
    if (size > data.byteLength - 2) return null;
    if (!size) return "";
    const text = data.subarray(2, 2 + size);
    let encoding = "utf-8";
    let start = 0;
    if (size >= 2 && text[0] === 0xfe && text[1] === 0xff) {
        encoding = "utf-16be";
        start = 2;
    } else if (size >= 2 && text[0] === 0xff && text[1] === 0xfe) {
        encoding = "utf-16le";
        start = 2;
    }
    if (encoding !== "utf-8" && (size - start) % 2) return null;
    try {
        const value = new TextDecoder(encoding, { fatal: true })
            .decode(text.subarray(start))
            .replace(/\r\n?/g, "\n")
            .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "");
        return value.length <= MOVTEXT_LIMITS.cueCharacters ? value : null;
    } catch {
        return null;
    }
}

/** Bounded future-cue window; seeking drops the window before new packets arrive. */
export class MovTextCueBuffer {
    private track: number | null = null;
    private cues: Cue[] = [];
    private characters = 0;
    private delay = 0;
    private mediaTime: number | null = null;

    get queuedCueCount() {
        return this.cues.length;
    }
    get queuedCharacters() {
        return this.characters;
    }
    configure(track: number | null) {
        this.clear();
        this.track = Number.isInteger(track) ? track : null;
    }
    setDelay(seconds: number) {
        if (Number.isFinite(seconds)) this.delay = seconds;
    }
    clear() {
        this.cues = [];
        this.characters = 0;
        this.mediaTime = null;
    }
    pushPacket(packet: TimedPacket) {
        const { timestamp: start, duration } = packet;
        const end = start + duration;
        if (
            this.track === null ||
            packet.streamIndex !== this.track ||
            !Number.isFinite(start) ||
            !Number.isFinite(duration) ||
            duration <= 0 ||
            !Number.isFinite(end) ||
            end <= start ||
            !(packet.data instanceof Uint8Array)
        )
            return;
        const text = decodeMovText(packet.data);
        if (!text) return;
        if (
            this.cues.some(
                (cue) =>
                    cue.start === start && cue.end === end && cue.text === text,
            )
        )
            return;
        this.cues.push({ start, end, text });
        this.characters += text.length;
        this.cues.sort((a, b) => a.start - b.start || a.end - b.end);
        while (
            this.cues.length > MOVTEXT_LIMITS.queuedCues ||
            this.characters > MOVTEXT_LIMITS.queuedCharacters
        ) {
            // Retain recent past cues for subtitle-delay adjustments. Reclaim ended
            // cues only under pressure; otherwise trim the farthest future packet.
            const time =
                this.mediaTime === null ? null : this.mediaTime - this.delay;
            const expired =
                time === null
                    ? -1
                    : this.cues.findIndex((cue) => cue.end <= time);
            const removed =
                expired >= 0
                    ? this.cues.splice(expired, 1)[0]
                    : this.cues.pop()!;
            this.characters -= removed.text.length;
        }
    }
    textAt(mediaTime: number): string {
        if (!Number.isFinite(mediaTime)) return "";
        this.mediaTime = mediaTime;
        const time = mediaTime - this.delay;
        return this.cues
            .filter((cue) => cue.start <= time && time < cue.end)
            .slice(0, MOVTEXT_LIMITS.visibleCues)
            .map((cue) => cue.text)
            .join("\n")
            .slice(0, MOVTEXT_LIMITS.cueCharacters);
    }
}

/** Public Movi renderer extension; no WASM decoder or internal player mutation. */
export default class MovTextRenderer implements SubtitleRenderer {
    private buffer = new MovTextCueBuffer();
    private container: HTMLElement | null = null;
    private node: HTMLDivElement | null = null;
    private text: HTMLSpanElement | null = null;
    private sheet: CSSStyleSheet | null = null;
    private root: ShadowRoot | Document | null = null;
    private displayed = "";
    private destroyed = false;

    configure(track: SubtitleTrack) {
        this.buffer.configure(isMovTextCodec(track.codec) ? track.id : null);
        this.display("");
    }
    mount(container: HTMLElement) {
        if (this.destroyed) return;
        this.unmount();
        this.container = container;
        // The public mount is shared with Movi's built-in empty-cue renderer,
        // which writes display:none even while a custom renderer is active.
        container.classList.add("tgdrive-movtext-overlay");
        const root = container.getRootNode();
        if (root instanceof ShadowRoot || root instanceof Document) {
            this.root = root;
            this.sheet = new CSSStyleSheet();
            this.sheet.replaceSync(rendererStyles);
            root.adoptedStyleSheets = [...root.adoptedStyleSheets, this.sheet];
        }
        this.node = document.createElement("div");
        this.node.className = "tgdrive-movtext-renderer";
        this.node.setAttribute("aria-label", "Subtitles");
        this.text = document.createElement("span");
        this.text.className = "tgdrive-movtext-cue";
        this.text.hidden = true;
        this.node.append(this.text);
        container.append(this.node);
        this.displayed = "";
    }
    pushPacket(packet: Packet) {
        if (!this.destroyed) this.buffer.pushPacket(packet);
    }
    render(mediaTime: number) {
        if (!this.destroyed) this.display(this.buffer.textAt(mediaTime));
    }
    setDelay(seconds: number) {
        this.buffer.setDelay(seconds);
    }
    clear() {
        this.buffer.clear();
        this.display("");
    }
    private display(value: string) {
        if (!this.text || value === this.displayed) return;
        this.displayed = value;
        this.text.textContent = value;
        this.text.hidden = !value;
    }
    private unmount() {
        this.container?.classList.remove("tgdrive-movtext-overlay");
        this.container = null;
        this.node?.remove();
        this.node = null;
        this.text = null;
        if (this.root && this.sheet)
            this.root.adoptedStyleSheets = this.root.adoptedStyleSheets.filter(
                (sheet) => sheet !== this.sheet,
            );
        this.root = null;
        this.sheet = null;
    }
    destroy() {
        if (this.destroyed) return;
        this.destroyed = true;
        this.clear();
        this.unmount();
    }
}
