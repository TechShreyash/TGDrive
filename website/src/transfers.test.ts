import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  fromJob,
  safeTransferMetadata,
  uploadBackupState,
  verifyReceivedChunks,
  TransferManager,
  retainTransferBatches,
  type Transfer,
} from "./transfers";
import {
  fileUrl,
  itemPath,
  formatBytes,
  formatByteProgress,
  remainingBytesHint,
} from "./types";
const managerForTest = () =>
  new TransferManager((url, options) => fetch(url, options));

async function hash(data: string) {
  return [
    ...new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data)),
    ),
  ]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
beforeEach(() => {
  const data = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => data.set(key, value),
  });
  vi.stubGlobal("window", new EventTarget());
});
afterEach(() => vi.unstubAllGlobals());
describe("safe resume and browser persistence", () => {
  it("checks all uploaded chunks before allowing a same-sized reselected file", async () => {
    const file = new File(["original"], "name.txt");
    const chunks = [
      { offset: 0, size: 4, sha256: await hash("orig") },
      { offset: 4, size: 4, sha256: await hash("inal") },
    ];
    await expect(
      verifyReceivedChunks(file, chunks, 8),
    ).resolves.toBeUndefined();
    await expect(
      verifyReceivedChunks(new File(["changed!"], "name.txt"), chunks, 8),
    ).rejects.toThrow("differs");
  });
  it("rejects missing and non-contiguous resume checksums", async () => {
    const file = new File(["abcd"], "a");
    await expect(verifyReceivedChunks(file, [], 4)).rejects.toThrow(
      "incomplete",
    );
    await expect(
      verifyReceivedChunks(
        file,
        [{ offset: 1, size: 3, sha256: await hash("bcd") }],
        4,
      ),
    ).rejects.toThrow("incomplete");
  });
  it("persists tracking metadata without source files, abort controllers or arbitrary error strings", () => {
    const transfer = {
      key: "id",
      name: "file.txt",
      size: 4,
      lastModified: 1,
      destination: "/",
      state: "error",
      phase: "Paused",
      current: 0,
      total: 4,
      file: new File(["test"], "file.txt"),
      controller: new AbortController(),
      error: "https://private.example/?credential=secret",
      password: "secret",
      auth: "token",
      url: "https://secret/",
    } as Transfer;
    const saved = JSON.stringify(safeTransferMetadata([transfer]));
    expect(saved).not.toContain("secret");
    expect(saved).not.toContain("token");
    expect(saved).not.toContain("controller");
    expect(saved).not.toContain('file":');
  });
  it("recovers unfinished browser receipt as a source-reselection action", () => {
    localStorage.setItem(
      "tgdrive.uploads.v2",
      JSON.stringify([
        {
          key: "key",
          name: "file",
          size: 4,
          lastModified: 1,
          destination: "/",
          state: "receiving",
          phase: "Receiving",
          current: 2,
          total: 4,
          uploadId: "upload",
        },
      ]),
    );
    const manager = managerForTest();
    expect(manager.snapshot()[0].state).toBe("awaiting_file");
    expect(manager.snapshot()[0].initiatedHere).toBe(true);
  });
  it("keeps URL jobs server-owned across a browser reload", async () => {
    const manager = managerForTest();
    manager.addJob("url-server-job", "URL file", "/", "remote");
    const restored = managerForTest();
    expect(restored.snapshot()[0]).toMatchObject({
      jobId: "url-server-job",
      kind: "remote",
      state: "uploading",
      initiatedHere: true,
    });
    await expect(
      restored.resume(
        restored.snapshot()[0].key,
        new File(["data"], "URL file"),
      ),
    ).rejects.toThrow("resume on the server");
  });
  it("never requests a local original for a URL receipt missing its server job", async () => {
    localStorage.setItem(
      "tgdrive.uploads.v2",
      JSON.stringify([
        {
          key: "missing",
          name: "URL file",
          size: 4,
          lastModified: 0,
          destination: "/",
          kind: "remote",
          state: "uploading",
          phase: "Sending",
          current: 1,
          total: 4,
        },
      ]),
    );
    const restored = managerForTest();
    expect(restored.snapshot()[0].state).toBe("error");
    expect(restored.snapshot()[0].phase).not.toContain("original");
    await expect(restored.retry("missing")).rejects.toThrow("no server job");
  });
  it("does not turn globally discovered transfers into local popup receipts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          jobs: [
            {
              id: "another-device",
              kind: "upload",
              status: "running",
              phase: "uploading",
              current: 1,
              total: 4,
            },
          ],
        }),
      ),
    );
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(1));
      expect(manager.snapshot()[0].initiatedHere).not.toBe(true);
      const restored = managerForTest();
      // The server is authoritative for discovered rows; stale browser copies
      // must not resurrect a job removed by a fixture/drive reset.
      expect(restored.snapshot()).toEqual([]);
      try {
        restored.setEnabled(true);
        await vi.waitFor(() => expect(restored.snapshot()).toHaveLength(1));
        expect(restored.snapshot()[0].initiatedHere).not.toBe(true);
        expect(restored.snapshot()[0].jobId).toBe("another-device");
      } finally {
        restored.setEnabled(false);
      }
    } finally {
      manager.setEnabled(false);
    }
  });
});
describe("resumable HTTP upload protocol", () => {
  it("publishes in-flight device bytes without persisting or resuming unconfirmed bytes", async () => {
    let report: ((bytes: number) => void) | undefined;
    let receipt: ((response: Response) => void) | undefined;
    const offsets: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
        if (input.endsWith("/complete")) return Response.json({ id: "job" });
        if (options.method === "HEAD")
          return new Response(null, {
            headers: { "Upload-Offset": "0", "Upload-Length": "8" },
          });
        return Response.json({
          id: "u",
          offset: 0,
          total_size: 8,
          chunk_size: 8,
          checksums: [],
        });
      }),
    );
    const manager = new TransferManager(async (_url, options, progress) => {
      offsets.push(new Headers(options.headers).get("Upload-Offset")!);
      report = progress;
      return new Promise<Response>((resolve) => {
        receipt = resolve;
      });
    });
    try {
      manager.setEnabled(true);
      manager.add([new File(["abcdefgh"], "source.txt")], "/", 10);
      await vi.waitFor(() => expect(report).toBeDefined());
      report!(3);
      expect(manager.snapshot()[0]).toMatchObject({
        current: 0,
        sentCurrent: 3,
        state: "receiving",
      });
      report!(6);
      expect(manager.snapshot()[0].sentCurrent).toBe(6);
      const saved = JSON.parse(localStorage.getItem("tgdrive.uploads.v2")!)[0];
      expect(saved.current).toBe(0);
      expect(saved.sentCurrent).toBeUndefined();
      receipt!(new Response(null, { status: 409 }));
      await vi.waitFor(() => expect(manager.snapshot()[0].state).toBe("error"));
      expect(manager.snapshot()[0].sentCurrent).toBeUndefined();
      const key = manager.snapshot()[0].key;
      report = undefined;
      await manager.retry(key);
      await vi.waitFor(() => expect(report).toBeDefined());
      expect(offsets).toEqual(["0", "0"]);
      expect(manager.snapshot()[0]).toMatchObject({
        current: 0,
        sentCurrent: 0,
      });
      report!(8);
      expect(manager.snapshot()[0].phase).toContain(
        "Waiting for server confirmation",
      );
      receipt!(
        new Response(null, { status: 204, headers: { "Upload-Offset": "8" } }),
      );
      await vi.waitFor(() => expect(manager.snapshot()[0].jobId).toBe("job"));
      expect(manager.snapshot()[0].sentCurrent).toBeUndefined();
    } finally {
      manager.setEnabled(false);
    }
  });
  it("sends each chunk at its acknowledged offset, then tracks the independent Telegram job", async () => {
    const calls: { method: string; offset: string | null; size: number }[] = [];
    let offset = 0;
    let complete = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        const method = options.method || "GET";
        const headers = new Headers(options.headers);
        calls.push({
          method,
          offset: headers.get("Upload-Offset"),
          size: options.body instanceof Blob ? options.body.size : 0,
        });
        if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
        if (method === "POST" && input === "/api/v1/uploads")
          return Response.json({
            id: "upload",
            offset: 0,
            total_size: 8,
            chunk_size: 4,
          });
        if (method === "HEAD")
          return new Response(null, {
            headers: { "Upload-Offset": String(offset), "Upload-Length": "8" },
          });
        if (method === "PATCH") {
          expect(Number(headers.get("Upload-Offset"))).toBe(offset);
          offset += (options.body as Blob).size;
          return new Response(null, {
            status: 204,
            headers: { "Upload-Offset": String(offset) },
          });
        }
        if (input.endsWith("/complete")) {
          complete = true;
          return Response.json({ id: "job" });
        }
        return Response.json({
          id: "upload",
          offset,
          total_size: 8,
          chunk_size: 4,
          checksums: [],
        });
      }),
    );
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      manager.add([new File(["abcdefgh"], "source.txt")], "/", 1024);
      await vi.waitFor(() => expect(complete).toBe(true));
      expect(
        calls
          .filter((call) => call.method === "PATCH")
          .map((call) => [call.offset, call.size]),
      ).toEqual([
        ["0", 4],
        ["4", 4],
      ]);
      expect(manager.snapshot()[0].jobId).toBe("job");
      expect(manager.snapshot()[0].phase).toBe("Queued for Telegram");
    } finally {
      manager.setEnabled(false);
    }
  });
  it("does not complete a file when the server fails to acknowledge the chunk", async () => {
    let completed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
        if (options.method === "POST" && input === "/api/v1/uploads")
          return Response.json({
            id: "u",
            offset: 0,
            total_size: 4,
            chunk_size: 4,
          });
        if (options.method === "HEAD")
          return new Response(null, {
            headers: { "Upload-Offset": "0", "Upload-Length": "4" },
          });
        if (options.method === "PATCH")
          return new Response(null, { status: 204 });
        if (input.endsWith("/complete")) completed = true;
        return Response.json({
          id: "u",
          offset: 0,
          total_size: 4,
          chunk_size: 4,
          checksums: [],
        });
      }),
    );
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      manager.add([new File(["abcd"], "a")], "/", 10);
      await vi.waitFor(() => expect(manager.snapshot()[0].state).toBe("error"));
      expect(completed).toBe(false);
    } finally {
      manager.setEnabled(false);
    }
  });
});
describe("opaque paths and Unicode filenames", () => {
  it("joins nested IDs without exposing filenames as paths", () =>
    expect(
      itemPath({
        id: "file",
        path: "/folder/",
        name: "資料 #1.txt",
        type: "file",
      }),
    ).toBe("/folder/file"));
  it("encodes share credentials and paths as query data", () => {
    const url = new URL(fileUrl("/folder/id", "signed+/token"), "http://test");
    expect(url.searchParams.get("auth")).toBe("signed+/token");
    expect(url.searchParams.get("path")).toBe("/folder/id");
  });
  it("formats zero bytes honestly", () => expect(formatBytes(0)).toBe("0 B"));
});

describe("ambiguous completion and verification cancellation", () => {
  it("reattaches a server-accepted upload after its complete response is lost", async () => {
    let accepted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        if (input === "/api/v1/jobs")
          return Response.json({
            jobs: accepted
              ? [
                  {
                    id: "same-id",
                    status: "running",
                    phase: "uploading",
                    current: 0,
                    total: 4,
                    name: "original.bin",
                    destination: "/",
                  },
                ]
              : [],
          });
        if (options.method === "POST" && input === "/api/v1/uploads")
          return Response.json({
            id: "same-id",
            offset: 0,
            total_size: 4,
            chunk_size: 4,
          });
        if (options.method === "HEAD")
          return new Response(null, {
            headers: { "Upload-Offset": "0", "Upload-Length": "4" },
          });
        if (options.method === "PATCH")
          return new Response(null, {
            status: 204,
            headers: { "Upload-Offset": "4" },
          });
        if (input.endsWith("/complete")) {
          accepted = true;
          throw new TypeError("network interrupted after acceptance");
        }
        return Response.json({
          id: "same-id",
          offset: 0,
          total_size: 4,
          chunk_size: 4,
          checksums: [],
        });
      }),
    );
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      manager.add([new File(["abcd"], "original.bin")], "/", 10);
      await vi.waitFor(() => expect(manager.snapshot()[0].state).toBe("error"));
      manager.setEnabled(false);
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.snapshot()[0].jobId).toBe("same-id"),
      );
      expect(manager.snapshot()).toHaveLength(1);
      expect(manager.snapshot()[0].state).toBe("uploading");
    } finally {
      manager.setEnabled(false);
    }
  });
  it("stops original-file verification when cancellation is requested", async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(
      verifyReceivedChunks(
        new File(["abcd"], "source"),
        [{ offset: 0, size: 4, sha256: await hash("abcd") }],
        4,
        () => {},
        controller.signal,
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
  });
});

it("shows the server's actionable chunk error without persisting its details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, options: RequestInit = {}) => {
      if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
      if (options.method === "POST" && input === "/api/v1/uploads")
        return Response.json({
          id: "quota",
          offset: 0,
          total_size: 4,
          chunk_size: 4,
        });
      if (options.method === "HEAD")
        return new Response(null, {
          headers: { "Upload-Offset": "0", "Upload-Length": "4" },
        });
      if (options.method === "PATCH")
        return Response.json(
          {
            detail:
              "Temporary Storage is full. Free disk space before retrying.",
          },
          { status: 507 },
        );
      return Response.json({
        id: "quota",
        offset: 0,
        total_size: 4,
        chunk_size: 4,
        checksums: [],
      });
    }),
  );
  const manager = managerForTest();
  try {
    manager.setEnabled(true);
    manager.add([new File(["abcd"], "quota.bin")], "/", 10);
    await vi.waitFor(() =>
      expect(manager.snapshot()[0].phase).toBe("Waiting for server space"),
    );
    expect(manager.snapshot()[0].state).toBe("queued");
    expect(localStorage.getItem("tgdrive.uploads.v2")).not.toContain(
      "Free disk space",
    );
  } finally {
    manager.setEnabled(false);
  }
});

describe("whole-batch recovery and bounded admission", () => {
  it("retains completed siblings in an unfinished batch beyond the old hundred-row limit", () => {
    const manager = managerForTest();
    manager.add(
      Array.from({ length: 150 }, (_, i) => new File(["x"], "file-" + i)),
      "/",
      10,
    );
    const original = manager.snapshot();
    for (const item of original.slice(0, -1)) {
      item.state = "completed";
      item.finishedAt = Date.now();
    }
    manager.clearFinished();
    expect(manager.snapshot()).toHaveLength(150);
    const reloaded = managerForTest().snapshot();
    expect(reloaded).toHaveLength(150);
    expect(reloaded.filter((item) => item.state === "completed")).toHaveLength(
      149,
    );
    expect(reloaded[149].state).toBe("awaiting_file");
    expect(new Set(reloaded.map((item) => item.batchId)).size).toBe(1);
  });
  it("keeps history as whole batches and retains errors", () => {
    const rows = Array.from({ length: 31 }, (_, batch) =>
      Array.from(
        { length: 2 },
        (_, index) =>
          ({
            key: batch + "-" + index,
            batchId: "batch-" + batch,
            name: "file",
            destination: "/",
            size: 1,
            lastModified: 1,
            state: "completed",
            phase: "Done",
            current: 1,
            total: 1,
            finishedAt: batch + 1,
          }) as Transfer,
      ),
    ).flat();
    const failed = {
      ...rows[0],
      key: "failed",
      batchId: "failed",
      state: "error" as const,
    };
    const kept = retainTransferBatches([...rows, failed]);
    expect(kept).toHaveLength(61);
    expect(kept.some((item) => item.batchId === "batch-0")).toBe(false);
    expect(kept.some((item) => item.key === "failed")).toBe(true);
    for (let batch = 1; batch < 31; batch++)
      expect(
        kept.filter((item) => item.batchId === "batch-" + batch),
      ).toHaveLength(2);
  });
  it("caps unfinished-batch rows at 2000 including retained completed siblings", () => {
    const manager = managerForTest();
    manager.add(
      Array.from({ length: 2000 }, (_, i) => new File(["x"], "" + i)),
      "/",
      10,
    );
    manager.snapshot()[0].state = "completed";
    expect(manager.canQueue(1)).toBe(false);
    expect(manager.add([new File(["x"], "extra")], "/", 10)[0]).toContain(
      "2,000",
    );
    expect(manager.snapshot()).toHaveLength(2000);
  });
  it("matches folder resume by exact rooted path without flattening duplicate basenames", async () => {
    const file = (path: string, data = "same") => {
      const file = new File([data], path.split("/").at(-1)!);
      Object.defineProperty(file, "webkitRelativePath", { value: path });
      return file;
    };
    const manager = managerForTest();
    manager.addEntries(
      [
        {
          file: file("Root/a/notes.txt"),
          relativePath: "Root/a/notes.txt",
          destination: "/r/a",
        },
        {
          file: file("Root/b/notes.txt"),
          relativePath: "Root/b/notes.txt",
          destination: "/r/b",
        },
      ],
      10,
      true,
      { name: "Root" },
    );
    const reloaded = managerForTest();
    const id = reloaded.snapshot()[0].batchId!;
    const result = await reloaded.resumeBatchFiles(id, [
      file("Root/b/notes.txt"),
    ]);
    expect(result).toMatchObject({ matched: 1, remaining: 1 });
    expect(reloaded.snapshot()[0].state).toBe("awaiting_file");
    expect(reloaded.snapshot()[1].state).toBe("queued");
    expect(reloaded.snapshot()[1].destination).toBe("/r/b");
    expect(new Set(reloaded.snapshot().map((item) => item.batchId)).size).toBe(
      1,
    );
  });
  it("does not guess when ordinary bulk resume has ambiguous name/size/modified signatures", async () => {
    const manager = managerForTest();
    const source = new File(["same"], "same.txt", { lastModified: 42 });
    manager.add([source, source], "/", 10);
    const reloaded = managerForTest();
    const result = await reloaded.resumeBatchFiles(
      reloaded.snapshot()[0].batchId!,
      [source],
    );
    expect(result.matched).toBe(0);
    expect(result.errors.length).toBeGreaterThan(0);
    expect(
      reloaded.snapshot().every((item) => item.state === "awaiting_file"),
    ).toBe(true);
  });
});

describe("independent browser receiving and Telegram backlog", () => {
  it("receives all five files while earlier Telegram uploads remain busy", async () => {
    let receipts = 0;
    let complete = 0;
    let patching = 0;
    let maxPatching = 0;
    const jobs: {
      id: string;
      status: string;
      phase: string;
      current: number;
      total: number;
      kind: string;
    }[] = [];
    const offsets = new Map<string, number>();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        if (input === "/api/v1/jobs") return Response.json({ jobs });
        if (input === "/api/v1/uploads" && options.method === "POST") {
          const id = "upload-" + ++receipts;
          offsets.set(id, 0);
          return Response.json({
            id,
            offset: 0,
            total_size: 4,
            chunk_size: 4,
          });
        }
        const id = input.split("/")[4];
        if (options.method === "HEAD")
          return new Response(null, {
            headers: {
              "Upload-Offset": String(offsets.get(id)),
              "Upload-Length": "4",
            },
          });
        if (options.method === "PATCH") {
          patching += 1;
          maxPatching = Math.max(maxPatching, patching);
          await Promise.resolve();
          offsets.set(id, 4);
          patching -= 1;
          return new Response(null, {
            status: 204,
            headers: { "Upload-Offset": "4" },
          });
        }
        if (input.endsWith("/complete")) {
          jobs.push({
            id,
            status: "running",
            phase: "uploading",
            current: 0,
            total: 4,
            kind: "upload",
          });
          complete += 1;
          return Response.json({ id });
        }
        return Response.json({
          id,
          offset: offsets.get(id),
          total_size: 4,
          chunk_size: 4,
          checksums: [],
        });
      }),
    );
    const manager = managerForTest();
    try {
      manager.add(
        Array.from({ length: 5 }, (_, i) => new File(["data"], "" + i)),
        "/",
        10,
      );
      manager.setEnabled(true);
      await vi.waitFor(() => expect(complete).toBe(5));
      expect(receipts).toBe(5);
      expect(jobs.every((job) => job.status === "running")).toBe(true);
      expect(maxPatching).toBe(1);
    } finally {
      manager.setEnabled(false);
    }
  });
  it("preserves known URL origin when the server changes its job kind to upload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          jobs: [
            {
              id: "remote",
              status: "running",
              phase: "uploading",
              current: 1,
              total: 4,
              kind: "upload",
            },
          ],
        }),
      ),
    );
    const manager = managerForTest();
    manager.addJob("remote", "URL file", "/", "remote");
    try {
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.snapshot()[0].serverStatus).toBe("running"),
      );
      expect(manager.snapshot()[0].kind).toBe("remote");
    } finally {
      manager.setEnabled(false);
    }
  });
});

it("warns when browser storage cannot retain recovery metadata while allowing uploads", () => {
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {
      throw new DOMException("full", "QuotaExceededError");
    },
  });
  const manager = managerForTest();
  expect(manager.add([new File(["data"], "file")], "/", 10)).toEqual([]);
  expect(manager.snapshot()).toHaveLength(1);
  expect(manager.recoveryWarning).toContain("could not save");
});

it("continues device receipts while four recovered Telegram jobs are busy", async () => {
  let receipts = 0;
  const jobs = Array.from({ length: 4 }, (_, i) => ({
    id: "recovered-" + i,
    name: "server",
    destination: "/",
    status: "running",
    phase: "uploading",
    current: 0,
    total: 4,
    kind: "remote",
  }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/jobs") return Response.json({ jobs });
      if (input === "/api/v1/uploads") receipts += 1;
      throw new Error("synthetic stopped receipt");
    }),
  );
  const manager = managerForTest();
  try {
    manager.add([new File(["data"], "new")], "/", 10);
    manager.setEnabled(true);
    await vi.waitFor(() => expect(manager.snapshot()).toHaveLength(5));
    await vi.waitFor(() => expect(receipts).toBe(1));
  } finally {
    manager.setEnabled(false);
  }
});

describe("Telegram backup protection", () => {
  it("keeps checkpoint-pending files unfinished and refuses cancellation locally", async () => {
    const fetchMock = vi.fn(async () =>
      Response.json({
        jobs: [
          {
            id: "protected-job",
            status: "running",
            phase: "backup_pending",
            name: "safe.txt",
            current: 4,
            total: 4,
            kind: "upload",
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.snapshot()[0]?.stage).toBe("backup_pending"),
      );
      manager.setEnabled(false);
      fetchMock.mockClear();
      expect(manager.snapshot()[0].state).toBe("uploading");
      expect(manager.snapshot()[0].phase).toContain(
        "Securing your drive backup",
      );
      await manager.cancelAll();
      await manager.cancel(manager.snapshot()[0].key);
      expect(fetchMock).not.toHaveBeenCalled();
      expect(manager.snapshot()[0].state).toBe("uploading");
    } finally {
      manager.setEnabled(false);
    }
  });
  it("reconciles a committed upload if cancellation races the checkpoint phase", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json(
          { detail: "Already catalogued", code: "BACKUP_PENDING" },
          { status: 409 },
        ),
      ),
    );
    const manager = managerForTest();
    manager.addJob("raced-job", "safe.txt", "/");
    await manager.cancel(manager.snapshot()[0].key);
    expect(manager.snapshot()[0].stage).toBe("backup_pending");
    expect(manager.snapshot()[0].state).toBe("uploading");
    expect(manager.snapshot()[0].error).toBeUndefined();
  });
  it("labels a failed checkpoint as a backup retry, not an upload retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          jobs: [
            {
              id: "retry-job",
              status: "error",
              phase: "backup_pending",
              name: "safe.txt",
              current: 4,
              total: 4,
            },
          ],
        }),
      ),
    );
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.snapshot()[0]?.state).toBe("error"),
      );
      expect(manager.snapshot()[0].phase).toContain("Backup Needs Attention");
    } finally {
      manager.setEnabled(false);
    }
  });
});

describe("overall backup readiness", () => {
  const row = (state: Transfer["state"], kind = "upload"): Transfer => ({
    key: state,
    name: "file",
    size: 4,
    lastModified: 1,
    destination: "/",
    phase: state,
    state,
    current: 0,
    total: 4,
    kind,
  });
  it("does not claim full protection while selected files are still queued or receiving", () => {
    expect(uploadBackupState([row("completed"), row("queued")])).toBe(
      "pending",
    );
    expect(uploadBackupState([row("receiving")])).toBe("pending");
    expect(uploadBackupState([row("uploading")])).toBe("pending");
  });
  it("requires failed uploads and missing originals to be resolved", () => {
    expect(uploadBackupState([row("error")])).toBe("attention");
    expect(uploadBackupState([row("awaiting_file")])).toBe("attention");
  });
  it("allows protection after completion or intentional cancellation", () => {
    expect(uploadBackupState([row("completed"), row("cancelled")])).toBeNull();
  });
  it("does not confuse pending deletion with an unsent upload", () => {
    expect(uploadBackupState([row("error", "delete")])).toBeNull();
  });
});

describe("deletion operation tracking", () => {
  it("keeps active and failed deletions out of upload history, including restored browser history", async () => {
    localStorage.setItem(
      "tgdrive.uploads.v2",
      JSON.stringify([
        {
          key: "old-delete",
          name: "Old",
          destination: "/",
          size: 0,
          kind: "delete",
          jobId: "delete-old",
          state: "uploading",
        },
      ]),
    );
    const jobs = [
      {
        id: "delete-1",
        kind: "delete",
        status: "running",
        phase: "deleting",
        current: 0,
        total: 10,
        name: "First",
      },
      {
        id: "delete-2",
        kind: "delete",
        status: "error",
        phase: "deleting",
        current: 0,
        total: 10,
        name: "Second",
        error: "Try Again",
      },
      {
        id: "upload-1",
        kind: "upload",
        status: "running",
        phase: "uploading",
        current: 3,
        total: 10,
        name: "Upload",
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ jobs })),
    );
    const manager = managerForTest();
    expect(manager.snapshot()).toEqual([]);
    try {
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.deletionSnapshot()).toHaveLength(2),
      );
      expect(manager.snapshot().map((item) => item.jobId)).toEqual([
        "upload-1",
      ]);
      expect(localStorage.getItem("tgdrive.uploads.v2")).not.toContain(
        "delete-",
      );
    } finally {
      manager.setEnabled(false);
    }
  });
  it("refreshes files when a tracked deletion actually completes", async () => {
    let completed = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          jobs: [
            {
              id: "delete-1",
              kind: "delete",
              status: completed ? "completed" : "running",
              phase: "deleting",
              current: 0,
              total: 10,
              name: "First",
            },
          ],
        }),
      ),
    );
    const changed = vi.fn();
    window.addEventListener("tgdrive:files-changed", changed);
    const manager = managerForTest();
    try {
      manager.setEnabled(true);
      await vi.waitFor(() =>
        expect(manager.deletionSnapshot()).toHaveLength(1),
      );
      expect(changed).not.toHaveBeenCalled();
      completed = true;
      manager.refreshJobs();
      await vi.waitFor(() => expect(changed).toHaveBeenCalledOnce());
      expect(manager.snapshot()).toEqual([]);
    } finally {
      manager.setEnabled(false);
    }
  });
});

describe("connection restart admission", () => {
  it("keeps job polling and cancellation available while pausing new browser receipts", async () => {
    let polls = 0;
    let receipts = 0;
    let complete = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string, options: RequestInit = {}) => {
        if (input === "/api/v1/jobs") {
          polls++;
          return Response.json({ jobs: [] });
        }
        if (input === "/api/v1/uploads" && options.method === "POST") {
          receipts++;
          return Response.json({
            id: "receipt",
            offset: 0,
            total_size: 4,
            chunk_size: 4,
          });
        }
        if (options.method === "HEAD")
          return new Response(null, {
            headers: { "Upload-Offset": "0", "Upload-Length": "4" },
          });
        if (options.method === "PATCH")
          return new Response(null, {
            status: 204,
            headers: { "Upload-Offset": "4" },
          });
        if (input.endsWith("/complete")) {
          complete = true;
          return Response.json({ id: "job" });
        }
        return Response.json({
          id: "receipt",
          offset: 0,
          total_size: 4,
          chunk_size: 4,
          checksums: [],
        });
      }),
    );
    const manager = managerForTest();
    try {
      manager.setAdmissionPaused(true);
      manager.setEnabled(true);
      manager.add([new File(["test"], "waiting.txt")], "/", 10);
      await vi.waitFor(() => expect(polls).toBeGreaterThan(0));
      expect(receipts).toBe(0);
      manager.refreshJobs();
      await vi.waitFor(() => expect(polls).toBeGreaterThan(1));
      await manager.cancel(manager.snapshot()[0].key);
      expect(manager.snapshot()[0].state).toBe("cancelled");
      manager.add([new File(["test"], "next.txt")], "/", 10);
      expect(receipts).toBe(0);
      manager.setAdmissionPaused(false);
      await vi.waitFor(() => expect(complete).toBe(true));
      expect(receipts).toBe(1);
    } finally {
      manager.setEnabled(false);
    }
  });
});

it("cancels only uploads included in the confirmed selection", async () => {
  const manager = managerForTest();
  manager.add(
    [new File(["a"], "first.txt"), new File(["b"], "second.txt")],
    "/",
    100,
  );
  const confirmedKeys = manager.snapshot().map((item) => item.key);
  manager.add([new File(["new"], "added-later.txt")], "/", 100);
  // A file can finish while the confirmation is open; it must be kept.
  manager.snapshot()[0].state = "completed";
  await manager.cancelAll(confirmedKeys);
  expect(
    manager.snapshot().find((item) => item.name === "first.txt")?.state,
  ).toBe("completed");
  expect(
    manager.snapshot().find((item) => item.name === "second.txt")?.state,
  ).toBe("cancelled");
  expect(
    manager.snapshot().find((item) => item.name === "added-later.txt")?.state,
  ).toBe("queued");
});

it("queues device files for automatic retry when staging is full", async () => {
  let receipts = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      if (input === "/api/v1/jobs") return Response.json({ jobs: [] });
      receipts++;
      return Response.json(
        { detail: "Temporary Storage budget is full" },
        { status: 507 },
      );
    }),
  );
  const manager = managerForTest();
  try {
    manager.add(
      [new File(["data"], "one"), new File(["more"], "two")],
      "/",
      10,
    );
    manager.setEnabled(true);
    await vi.waitFor(() => expect(receipts).toBe(1));
    expect(manager.snapshot()[0].state).toBe("queued");
    expect(manager.snapshot()[0].phase).toBe("Waiting for server space");
    expect(manager.snapshot()[0].file).toBeDefined();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(receipts).toBe(1);
  } finally {
    manager.setEnabled(false);
  }
});

it("distinguishes bytes sent from multipart messages stored in the channel", () => {
  const job = {
    id: "parts",
    status: "running",
    phase: "uploading",
    current: 3200,
    total: 4500,
    telegram_parts: { stored: 0, total: 3 },
  };
  expect(fromJob(job).phase).toBe(
    "Uploading to Telegram · 0/3 Parts in Channel",
  );
  expect(
    fromJob({ ...job, telegram_parts: { stored: 1, total: 3 } }).phase,
  ).toContain("1/3");
  expect(fromJob({ ...job, status: "completed" }).phase).toBe(
    "Available in your drive",
  );
  expect(fromJob({ ...job, telegram_parts: undefined }).phase).toBe(
    "Uploading to Telegram",
  );
});

it("keeps near-complete byte counters precise without implying completion", () => {
  const gb = 1024 ** 3;
  expect(formatByteProgress(4.2 * gb, 4.25 * gb)).toBe("4.2 / 4.25 GB");
  expect(formatByteProgress(4.2499 * gb, 4.25 * gb)).toBe("4.25 / 4.25 GB");
  expect(remainingBytesHint(4.25 * gb - 1024, 4.25 * gb)).toBe(
    "1 KB remaining",
  );
  expect(remainingBytesHint(4.25 * gb, 4.25 * gb)).toBe("");
  expect(remainingBytesHint(gb, 4.25 * gb)).toBe("");
});
