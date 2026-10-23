import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TransferPanel from "./TransferPanel";
import type { Transfer } from "./transfers";

const local = (change: Partial<Transfer> = {}): Transfer => ({
  key: "device-file",
  name: "Photo.jpg",
  size: 100,
  total: 100,
  current: 0,
  lastModified: 0,
  destination: "/",
  initiatedHere: true,
  state: "queued",
  phase: "Waiting to send from this device",
  ...change,
});

beforeEach(() => {
  vi.stubGlobal("window", {
    history: { state: null },
    matchMedia: () => ({ matches: false }),
  });
  vi.stubGlobal("document", { createElement: () => ({}) });
});
afterEach(() => vi.unstubAllGlobals());

function render(items: Transfer[], expandedPage = true) {
  return renderToStaticMarkup(
    createElement(TransferPanel, {
      items,
      expandedPage,
      onError: () => {},
    }),
  );
}

describe("visible device receipt feedback", () => {
  it("shows server receipt counts in a folder summary before its files are expanded", () => {
    const first = local({
      batchId: "folder",
      relativePath: "Pictures/Photo.jpg",
      jobId: "accepted",
      state: "uploading",
    });
    const second = local({
      batchId: "folder",
      key: "second",
      relativePath: "Pictures/Second.jpg",
    });
    const html = render([first, second]);
    expect(html).toContain("1 of 2 files received by server");
    expect(html).toContain("Keep this tab open");
    expect(html).not.toContain("You can close this tab");
    expect(html).not.toContain("transfer-child");
  });

  it("offers to close the tab only after every standalone upload is accepted", () => {
    const accepted = local({ jobId: "accepted", state: "uploading" });
    const pending = local({ key: "second", name: "Second.jpg" });
    expect(render([accepted, pending])).not.toContain("You can close this tab");
    expect(render([accepted, pending])).toContain("remaining device uploads");
    expect(
      render([
        accepted,
        { ...pending, jobId: "second-job", state: "uploading" },
      ]),
    ).toContain(
      "You can close this tab; Telegram uploads continue on the server.",
    );
  });

  it("does not show a device receipt for server-discovered or URL transfers", () => {
    expect(
      render([
        local({
          initiatedHere: false,
          jobId: "server-job",
          state: "uploading",
        }),
      ]),
    ).not.toContain("transfer-device-status");
    expect(
      render([local({ kind: "remote", jobId: "url-job", state: "uploading" })]),
    ).not.toContain("transfer-device-status");
  });

  it("keeps collapsed readiness in the original header without adding an overlay row", () => {
    vi.stubGlobal("window", {
      history: { state: { tgdriveReveal: true } },
      matchMedia: () => ({ matches: false }),
    });
    const pending = render([local()], false);
    expect(pending).toContain(
      'class="transfer-collapsed-status" role="status">Keep this tab open',
    );
    expect(pending).not.toContain('class="transfer-notice"');
    const accepted = render(
      [local({ jobId: "accepted", state: "uploading" })],
      false,
    );
    expect(accepted).toContain(
      'class="transfer-collapsed-status" role="status">You can close this tab',
    );
    expect(accepted).toContain(
      'title="Device upload complete. You can close this tab; Telegram uploads continue on the server."',
    );
    expect(accepted).not.toContain('class="transfer-notice"');
  });
});
