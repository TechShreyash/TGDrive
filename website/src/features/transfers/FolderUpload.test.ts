import { afterEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import FolderUpload from "./FolderUpload";
import {
    folderTreeRequest,
    planFolderUpload,
    prepareFolderUpload,
} from "./FolderUpload";
function picked(path: string, contents = "data") {
    const file = new File([contents], path.split("/").at(-1)!);
    Object.defineProperty(file, "webkitRelativePath", { value: path });
    return file;
}
afterEach(() => vi.unstubAllGlobals());
describe("folder selection validation", () => {
    it("preserves the selected root and ancestors while keeping duplicate basenames separate", () => {
        const plan = planFolderUpload(
            [
                picked("Project/readme.txt"),
                picked("Project/alpha/notes.txt"),
                picked("Project/beta/deep/notes.txt"),
            ],
            100,
        );
        expect(plan.rootName).toBe("Project");
        expect(plan.folders).toEqual([["alpha"], ["beta"], ["beta", "deep"]]);
        expect(
            plan.files.map(({ relativePath, parent }) => [
                relativePath,
                parent,
            ]),
        ).toEqual([
            ["Project/readme.txt", ""],
            ["Project/alpha/notes.txt", "alpha"],
            ["Project/beta/deep/notes.txt", "beta/deep"],
        ]);
    });
    it("normalizes relative folder keys to the backend NFC contract", () => {
        const plan = planFolderUpload([picked("資料/Cafe\u0301/a.txt")], 100);
        expect(plan.folders).toEqual([["Café"]]);
        expect(plan.files[0].parent).toBe("Café");
    });
    it.each([
        "Project/../a",
        "Project//a",
        "/Project/a",
        "Project/a\\b",
        "Project/ bad/a",
        "Project/\u0001/a",
    ])("rejects unsafe relative paths: %s", (path) =>
        expect(() => planFolderUpload([picked(path)], 100)).toThrow(),
    );
    it("does not flatten a browser selection that omitted folder paths", () => {
        expect(() =>
            planFolderUpload([new File(["ok"], "file.txt")], 100),
        ).toThrow("did not provide folder paths");
    });
    it("rejects sibling collisions and file-versus-folder conflicts", () => {
        expect(() =>
            planFolderUpload([picked("Root/A/a"), picked("Root/a/b")], 100),
        ).toThrow("conflicting");
        expect(() =>
            planFolderUpload([picked("Root/a"), picked("Root/a/b")], 100),
        ).toThrow("conflicting");
        expect(() =>
            planFolderUpload([picked("Root/a"), picked("Root/a")], 100),
        ).toThrow("conflicting");
    });
    it("lists unsupported files explicitly without creating their otherwise-empty ancestors", () => {
        const plan = planFolderUpload(
            [
                picked("Root/a", "ok"),
                picked("Root/empty/zero", ""),
                picked("Root/large/file", "x".repeat(200)),
            ],
            100,
        );
        expect(plan.files).toHaveLength(1);
        expect(plan.skipped).toHaveLength(2);
        expect(plan.folders).toEqual([]);
        expect(plan.totalSize).toBe(2);
    });
    it("bounds admission and actual UTF-8 metadata payload bytes", () => {
        expect(() =>
            planFolderUpload(
                Array.from({ length: 1001 }, (_, i) => picked("Root/" + i)),
                100,
            ),
        ).toThrow("1,000 files");
        expect(() =>
            planFolderUpload(
                [picked(["Root", ...Array(63).fill("a"), "file"].join("/"))],
                100,
            ),
        ).toThrow("64 levels");
        const plan = planFolderUpload([picked("Root/a")], 100);
        plan.folders = Array.from({ length: 300 }, () => ["界".repeat(255)]);
        expect(() => folderTreeRequest(plan, "/", "id")).toThrow(
            "preparation limit",
        );
    });
});
describe("confirmed atomic folder preparation", () => {
    const response = {
        root: { id: "root-id", name: "Project (1)", path: "/", type: "folder" },
        folders: { "": "/root-id", alpha: "/root-id/alpha-id" },
    };
    it("resolves opaque destinations only after a confirmed response and never sends file bytes", async () => {
        const fetcher = vi.fn(async () =>
            Response.json(response, { status: 201 }),
        );
        vi.stubGlobal("fetch", fetcher);
        const plan = planFolderUpload([picked("Project/alpha/a.txt")], 100);
        const result = await prepareFolderUpload(
            plan,
            "/",
            "stable-key",
            new AbortController().signal,
        );
        expect(result.root.name).toBe("Project (1)");
        expect(result.entries[0].destination).toBe("/root-id/alpha-id");
        expect(result.entries[0].relativePath).toBe("Project/alpha/a.txt");
        expect(fetcher).toHaveBeenCalledTimes(1);
        const [url, options] = fetcher.mock.calls[0] as unknown as [
            string,
            RequestInit,
        ];
        expect(url).toBe("/api/v1/folders/tree");
        expect(JSON.parse(options.body as string)).toEqual({
            destination: "/",
            root_name: "Project",
            folders: [["alpha"]],
            client_request_id: "stable-key",
        });
    });
    it("replays the same idempotency key and manifest after an unknown response outcome", async () => {
        const calls: string[] = [];
        vi.stubGlobal(
            "fetch",
            vi.fn(async (_url: string, options: RequestInit) => {
                calls.push(options.body as string);
                if (calls.length === 1) throw new TypeError("response lost");
                return Response.json(response);
            }),
        );
        const plan = planFolderUpload([picked("Project/alpha/a.txt")], 100);
        await expect(
            prepareFolderUpload(
                plan,
                "/",
                "stable-key",
                new AbortController().signal,
            ),
        ).rejects.toThrow();
        await prepareFolderUpload(
            plan,
            "/",
            "stable-key",
            new AbortController().signal,
        );
        expect(calls[0]).toBe(calls[1]);
    });
    it("rejects missing or foreign destination mappings", async () => {
        const plan = planFolderUpload([picked("Project/alpha/a")], 100);
        vi.stubGlobal(
            "fetch",
            vi.fn(async () =>
                Response.json({
                    ...response,
                    folders: { "": "/root-id", alpha: "/unrelated" },
                }),
            ),
        );
        await expect(
            prepareFolderUpload(plan, "/", "key", new AbortController().signal),
        ).rejects.toThrow("incomplete");
        vi.stubGlobal(
            "fetch",
            vi.fn(async () => Response.json(response)),
        );
        await expect(
            prepareFolderUpload(
                plan,
                "/another",
                "key",
                new AbortController().signal,
            ),
        ).rejects.toThrow("invalid");
    });
});

describe("folder confirmation", () => {
    it("shows only the selection, destination and necessary upload controls", () => {
        const html = renderToStaticMarkup(
            createElement(FolderUpload, {
                files: [
                    picked("Project/readme.txt"),
                    picked("Project/sub/notes.txt"),
                ],
                destination: "/",
                destinationName: "My Files",
                maxSize: 100,
                supported: true,
                onClose: () => {},
                onQueued: () => {},
                onFilesFallback: () => {},
                onChanged: () => {},
            }),
        );
        expect(html).toContain("<strong>Project</strong>");
        expect(html).toContain("2 files · 8 B");
        expect(html).toContain("To <strong>My Files</strong>");
        expect(html).not.toContain("folder-upload-help");
        expect(html).toContain(">Upload</button>");
        expect(html).not.toContain("resumable upload queue");
        expect(html).not.toContain("Keep this tab open");
        expect(html).not.toContain("preparation request");
    });
});
