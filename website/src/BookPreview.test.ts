import { describe, expect, it, vi, afterEach } from "vitest";
import { bookUrl, validateBook, safeNodes, bookJson } from "./BookPreview";

afterEach(() => vi.unstubAllGlobals());
describe("bounded reader descriptors", () => {
  it("encodes archive resources without navigating outside the local endpoint", () => {
    const url = bookUrl("/opaque book", "private-token", "OPS/page #1.png");
    expect(url.startsWith("/api/v1/books?")).toBe(true);
    expect(new URLSearchParams(url.split("?")[1]).get("entry")).toBe(
      "OPS/page #1.png",
    );
  });
  it("accepts a reading order and rejects unsafe or empty indexes", () => {
    expect(
      validateBook({
        kind: "comic",
        entries: [{ name: "1.png", label: "Page 1" }],
      }).entries,
    ).toHaveLength(1);
    expect(() => validateBook({ kind: "epub", entries: [] })).toThrow();
    expect(() =>
      validateBook({
        kind: "comic",
        entries: [{ name: "../secret", label: "Bad" }],
      }),
    ).toThrow();
  });
  it("only accepts passive structured chapter nodes and drops untrusted attributes", () => {
    expect(() =>
      safeNodes([{ tag: "script", children: ["alert(1)"] }]),
    ).toThrow();
    const nodes = safeNodes([
      {
        tag: "img",
        children: [],
        src: "https://tracker.invalid/x",
        onerror: "alert(1)",
      },
    ]);
    expect(nodes).toEqual([
      {
        tag: "img",
        children: [],
        src: undefined,
        href: undefined,
        alt: "",
        id: undefined,
      },
    ]);
    expect(() => safeNodes({ html: "<img onerror=alert(1)>" })).toThrow();
  });
  it("bounds oversized proxy responses and reports reader errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("{}", {
          headers: { "Content-Length": String(9 * 1024 * 1024) },
        }),
      ),
    );
    await expect(
      bookJson("/api/v1/books?path=x", new AbortController().signal),
    ).rejects.toThrow();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "The book is encrypted" }), {
          status: 422,
        }),
      ),
    );
    await expect(
      bookJson("/api/v1/books?path=x", new AbortController().signal),
    ).rejects.toThrow("encrypted");
  });
});
