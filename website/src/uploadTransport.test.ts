import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendUploadChunk } from "./uploadTransport";

class MockXHR {
  static latest: MockXHR;
  upload = { onprogress: null as ((event: { loaded: number }) => void) | null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  status = 204;
  statusText = "No Content";
  responseText = "";
  withCredentials = false;
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn();
  abort = vi.fn(() => this.onabort?.());
  getAllResponseHeaders = () => "Upload-Offset: 8\r\nX-Test: receipt\r\n";
  constructor() {
    MockXHR.latest = this;
  }
}
beforeEach(() => vi.stubGlobal("XMLHttpRequest", MockXHR));
afterEach(() => vi.unstubAllGlobals());

describe("observed browser upload transport", () => {
  const options = (signal: AbortSignal) => ({
    method: "PATCH",
    headers: {
      "Content-Type": "application/octet-stream",
      "Upload-Offset": "0",
    },
    body: new Blob(["12345678"]),
    signal,
  });
  it("reports within-chunk bytes before the server receipt and keeps resume headers", async () => {
    const progress = vi.fn();
    const result = sendUploadChunk(
      "/api/v1/uploads/id",
      options(new AbortController().signal),
      progress,
    );
    const xhr = MockXHR.latest;
    expect(xhr.open).toHaveBeenCalledWith("PATCH", "/api/v1/uploads/id");
    expect(xhr.withCredentials).toBe(true);
    expect(xhr.setRequestHeader).toHaveBeenCalledWith("upload-offset", "0");
    xhr.upload.onprogress?.({ loaded: 2 });
    xhr.upload.onprogress?.({ loaded: 5 });
    xhr.upload.onprogress?.({ loaded: 99 });
    expect(progress.mock.calls.map(([bytes]) => bytes)).toEqual([2, 5, 8]);
    xhr.onload?.();
    const response = await result;
    expect(response.status).toBe(204);
    expect(response.headers.get("Upload-Offset")).toBe("8");
    expect(await response.text()).toBe("");
  });
  it("cancels the request, rejects with AbortError and ignores late progress", async () => {
    const controller = new AbortController();
    const progress = vi.fn();
    const result = sendUploadChunk(
      "/upload",
      options(controller.signal),
      progress,
    );
    const rejection = expect(result).rejects.toMatchObject({
      name: "AbortError",
    });
    controller.abort();
    MockXHR.latest.upload.onprogress?.({ loaded: 8 });
    await rejection;
    expect(MockXHR.latest.abort).toHaveBeenCalledOnce();
    expect(progress).not.toHaveBeenCalled();
    expect(MockXHR.latest.send).toHaveBeenCalledOnce();
  });
  it("does not send an already cancelled chunk", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      sendUploadChunk("/upload", options(controller.signal), vi.fn()),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(MockXHR.latest.send).not.toHaveBeenCalled();
  });
  it("returns HTTP failures for the existing safe-resume path and propagates network failure", async () => {
    const controller = new AbortController();
    const result = sendUploadChunk(
      "/upload",
      options(controller.signal),
      vi.fn(),
    );
    MockXHR.latest.status = 409;
    MockXHR.latest.statusText = "Conflict";
    MockXHR.latest.responseText = JSON.stringify({ detail: "Offset changed" });
    MockXHR.latest.onload?.();
    const response = await result;
    expect(response.ok).toBe(false);
    expect(await response.json()).toEqual({ detail: "Offset changed" });
    const failure = sendUploadChunk(
      "/upload",
      options(controller.signal),
      vi.fn(),
    );
    const rejection = expect(failure).rejects.toThrow("interrupted");
    MockXHR.latest.onerror?.();
    await rejection;
  });
});
