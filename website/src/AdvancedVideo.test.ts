import { describe, expect, it } from "vitest";
import {
  initialVideoLoadState,
  unavailableVideoCodec,
  videoLoadTransition,
  videoResumeTime,
  videoIsAdvancing,
  videoChapters,
  chapterTime,
  type VideoLoadState,
} from "./AdvancedVideo";

describe("stale buffering recovery", () => {
  it("accepts advancing playback, but not paused clocks or seek jumps", () => {
    expect(videoIsAdvancing(12, 12.25, false, false)).toBe(true);
    expect(videoIsAdvancing(12, 12, false, false)).toBe(false);
    expect(videoIsAdvancing(12, 12.25, true, false)).toBe(false);
    expect(videoIsAdvancing(12, 12.25, false, true)).toBe(false);
    expect(videoIsAdvancing(12, 90, false, false)).toBe(false);
    expect(videoIsAdvancing(Number.NaN, 12, false, false)).toBe(false);
  });
  it("offers chapter names and times without any thumbnail operation", () => {
    expect(
      videoChapters(
        [
          { title: "Opening", start: 0 },
          { title: "", start: 12.5 },
          { title: "Invalid", start: Number.NaN },
          { title: "Past end", start: 40 },
        ],
        30,
      ),
    ).toEqual([
      { title: "Opening", start: 0 },
      { title: "Chapter 2", start: 12.5 },
    ]);
    expect(chapterTime(0)).toBe("0:00");
    expect(chapterTime(3665.2)).toBe("1:01:05");
  });
});

describe("video startup and buffering states", () => {
  it("uses actual engine and track events for startup phases", () => {
    const opening = videoLoadTransition(initialVideoLoadState, "engine-loaded");
    expect(opening).toEqual({ phase: "opening", ready: false });
    expect(videoLoadTransition(opening, "trackschange")).toEqual({
      phase: "preparing",
      ready: false,
    });
  });
  it.each(["loadedmetadata", "loadeddata", "canplay", "ready"] as const)(
    "unlocks controls on %s without requiring playback or autoplay",
    (event) => {
      expect(videoLoadTransition(initialVideoLoadState, event)).toEqual({
        phase: "ready",
        ready: true,
      });
    },
  );
  it("keeps a startup wait distinct from a playback stall", () => {
    expect(videoLoadTransition(initialVideoLoadState, "waiting")).toEqual({
      phase: "preparing",
      ready: false,
    });
    expect(
      videoLoadTransition({ phase: "ready", ready: true }, "waiting"),
    ).toEqual({ phase: "buffering", ready: true });
  });
  it("leaves controls available during buffering and seeking", () => {
    const ready: VideoLoadState = { phase: "ready", ready: true };
    const buffering = videoLoadTransition(ready, "waiting");
    const seeking = videoLoadTransition(buffering, "seeking");
    expect(seeking).toEqual({ phase: "seeking", ready: true });
    expect(videoLoadTransition(seeking, "seeked")).toEqual(ready);
    expect(videoLoadTransition(buffering, "pause")).toEqual(ready);
    expect(videoLoadTransition(buffering, "playing")).toEqual(ready);
    expect(videoLoadTransition(buffering, "ended")).toEqual(ready);
  });
  it("does not mistake pausing before metadata for loaded media", () => {
    expect(videoLoadTransition(initialVideoLoadState, "pause")).toEqual(
      initialVideoLoadState,
    );
  });
  it("does not reopen startup when tracks change during playback", () => {
    const ready: VideoLoadState = { phase: "ready", ready: true };
    expect(videoLoadTransition(ready, "trackschange")).toBe(ready);
    expect(videoLoadTransition(ready, "loadstart")).toEqual({
      phase: "opening",
      ready: false,
    });
    expect(videoLoadTransition(ready, "reset")).toEqual(initialVideoLoadState);
  });
});

describe("saved video position bounds", () => {
  it("preserves a valid saved position until the new duration is known", () => {
    expect(videoResumeTime(37.5)).toBe(37.5);
    expect(videoResumeTime(37.5, 0)).toBe(37.5);
    expect(videoResumeTime(37.5, Number.NaN)).toBe(37.5);
    expect(videoResumeTime(37.5, 80)).toBe(37.5);
  });
  it("keeps a restored position inside a shorter rendition", () => {
    expect(videoResumeTime(37.5, 30)).toBe(29.9);
    expect(videoResumeTime(37.5, 0.05)).toBe(0);
  });
  it("normalizes invalid positions without creating an invalid startup seek", () => {
    expect(videoResumeTime(-1, 30)).toBe(0);
    expect(videoResumeTime(Number.NaN, 30)).toBe(0);
    expect(videoResumeTime(Number.POSITIVE_INFINITY, 30)).toBe(0);
  });
});

describe("settled video decoder availability", () => {
  const tracks = [{ type: "video", codec: "msmpeg4v3" }];
  it("detects an actually rejected decoder rather than trusting a ready clock", () => {
    expect(
      unavailableVideoCodec({
        videoDecoder: { configured: false },
        getMediaInfo: () => ({ tracks }),
      }),
    ).toBe("msmpeg4v3");
  });
  it("keeps supported ASF codecs and unrecognized adapter shapes available", () => {
    expect(
      unavailableVideoCodec({
        videoDecoder: { configured: true },
        getMediaInfo: () => ({ tracks }),
      }),
    ).toBeNull();
    expect(
      unavailableVideoCodec({ getMediaInfo: () => ({ tracks }) }),
    ).toBeNull();
    expect(unavailableVideoCodec(null)).toBeNull();
  });
  it("does not reject audio-only media or cover art", () => {
    for (const mediaTracks of [
      [{ type: "audio", codec: "aac" }],
      [{ type: "video", codec: "mjpeg", isAttachedPic: true }],
    ]) {
      expect(
        unavailableVideoCodec({
          videoDecoder: { configured: false },
          getMediaInfo: () => ({ tracks: mediaTracks }),
        }),
      ).toBeNull();
    }
  });
});
