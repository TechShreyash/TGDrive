import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import TransferPanel, {
    BatchTransfer,
    CollapsedTransferSummary,
} from "./TransferPanel";
import { summarizeBatches } from "./transferBatches";
import { deviceUploadMessage } from "./deviceUploadStatus";
import type { Transfer } from "./transfers";
import { getTimezone, setTimezone } from "../../shared/types";

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
    folderUpload: change.relativePath
        ? {
              id: change.batchId || "folder",
              name: change.relativePath.split("/")[0],
              root_path: "/" + change.relativePath.split("/")[0],
              created_at: 1,
          }
        : undefined,
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

function renderCollapsedSummary(items: Transfer[]) {
    return renderToStaticMarkup(
        createElement(CollapsedTransferSummary, {
            batches: summarizeBatches(items, 10000),
            deviceMessage: deviceUploadMessage(items),
        }),
    );
}

function renderExpandedBatches(items: Transfer[], noticeBatch?: string) {
    const batches = summarizeBatches(items, 10000);
    const deviceUploadsPending = batches.some((batch) => batch.device.pending);
    return renderToStaticMarkup(
        createElement(
            "div",
            null,
            ...batches.map((batch) =>
                createElement(BatchTransfer, {
                    key: batch.id,
                    batch,
                    resumeNotice:
                        batch.id === noticeBatch
                            ? {
                                  text: "1 still need their original files.",
                                  dismiss: () => {},
                              }
                            : undefined,
                    deviceUploadsPending,
                    expanded: true,
                    onToggleExpanded: () => {},
                    now: 10000,
                    run: () => {},
                    onChooseFile: () => {},
                    onChooseBatch: () => {},
                    folderPickerSupported: true,
                }),
            ),
        ),
    );
}

describe("visible device receipt feedback", () => {
    it("keeps dismissible resume feedback inside its originating folder", () => {
        const html = renderExpandedBatches(
            [
                local({
                    key: "jee-file",
                    name: "missing.bin",
                    batchId: "jee",
                    relativePath: "jee/missing.bin",
                    state: "awaiting_file",
                }),
                local({
                    key: "solar-file",
                    name: "source.bin",
                    batchId: "solar",
                    relativePath: "SolarSync/source.bin",
                    state: "receiving",
                }),
            ],
            "jee",
        );
        expect(html).toContain("jee: 1 still need their original files.");
        expect(html).toContain('aria-label="Dismiss resume result for jee"');
        expect(html.slice(html.indexOf('data-batch-id="solar"'))).not.toContain(
            "transfer-notice",
        );
    });

    it("uses the instance timezone for finished file and folder dates", () => {
        const previousTimezone = getTimezone();
        const finishedAt = Date.parse("2026-01-02T00:30:00Z");
        const items = [
            local({ state: "completed", finishedAt }),
            local({
                key: "folder-file",
                state: "completed",
                finishedAt,
                relativePath: "Pictures/Photo.jpg",
            }),
        ];
        const views: string[] = [];
        try {
            for (const timezone of ["Pacific/Honolulu", "Pacific/Kiritimati"]) {
                setTimezone(timezone);
                const html = render(items);
                const full = new Intl.DateTimeFormat(undefined, {
                    dateStyle: "full",
                    timeStyle: "short",
                    timeZone: timezone,
                }).format(finishedAt);
                const compact = new Intl.DateTimeFormat(undefined, {
                    dateStyle: "medium",
                    timeStyle: "short",
                    timeZone: timezone,
                }).format(finishedAt);
                expect(html.split(`title="${full}"`)).toHaveLength(3);
                expect(html.split(`>${compact}</time>`)).toHaveLength(3);
                views.push(html);
            }
            expect(views[0]).not.toBe(views[1]);
        } finally {
            setTimezone(previousTimezone);
        }
    });

    it("keeps separate standalone uploads in the device phase until all are accepted", () => {
        const accepted = local({
            batchId: "accepted-file",
            jobId: "first-job",
            state: "uploading",
            stage: "uploading",
            phase: "Uploading to Telegram",
            current: 25,
            speedBytesPerSecond: 10,
            progressUpdatedAt: 9,
            serverObservedAt: 9000,
        });
        const sending = local({
            key: "second",
            batchId: "sending-file",
            state: "receiving",
            phase: "Uploading from your device",
            current: 40,
            meter: {
                stage: "receipt",
                bytes: 40,
                at: 9000,
                advancedAt: 9000,
                rate: 20,
            },
        });
        const pending = renderExpandedBatches([accepted, sending]);
        expect(pending).toContain('class="transfer-phase">Received');
        expect(pending).toContain("40 / 100 B");
        expect(pending).toContain("20 B/s");
        expect(pending).not.toContain("Telegram");
        expect(pending).not.toContain("25 / 100 B");
        expect(pending).not.toContain("10 B/s");

        const complete = renderExpandedBatches([
            accepted,
            {
                ...sending,
                jobId: "second-job",
                state: "uploading",
                stage: "uploading",
                phase: "Uploading to Telegram",
                current: 50,
                speedBytesPerSecond: 30,
                progressUpdatedAt: 9,
                serverObservedAt: 9000,
            },
        ]);
        expect(complete).toContain("25 / 100 B");
        expect(complete).toContain("50 / 100 B");
        expect(complete).toContain("10 B/s");
        expect(complete).toContain("30 B/s");
        expect(complete).not.toContain("Uploading from your device");
        expect(complete).not.toContain('class="transfer-phase">Received');
    });

    it("keeps accepted folders and their children received while another folder sends", () => {
        const first = local({
            batchId: "accepted-folder",
            relativePath: "First/Photo.jpg",
            jobId: "first-job",
            state: "uploading",
            stage: "uploading",
            phase: "Uploading to Telegram",
            current: 25,
            speedBytesPerSecond: 10,
            progressUpdatedAt: 9,
            serverObservedAt: 9000,
        });
        const second = local({
            key: "second",
            batchId: "sending-folder",
            relativePath: "Second/Photo.jpg",
            state: "receiving",
            phase: "Uploading from your device",
            current: 40,
            meter: {
                stage: "receipt",
                bytes: 40,
                at: 9000,
                advancedAt: 9000,
                rate: 20,
            },
        });
        const pending = renderExpandedBatches([first, second]);
        expect(pending.match(/data-stage="device"/g)).toHaveLength(2);
        expect(pending.match(/<progress/g)).toHaveLength(2);
        expect(pending).toContain(
            'class="transfer-batch-status" aria-live="polite">Received',
        );
        expect(pending).toContain('class="transfer-phase">Received');
        expect(pending).toContain("20 B/s");
        expect(pending).not.toContain("Telegram");
        expect(pending).not.toContain("25 / 100 B");
        expect(pending).not.toContain("10 B/s");
        expect(pending).not.toContain("0 of 1");

        const complete = renderExpandedBatches([
            first,
            {
                ...second,
                jobId: "second-job",
                state: "uploading",
                stage: "uploading",
                phase: "Uploading to Telegram",
                current: 50,
                speedBytesPerSecond: 30,
                progressUpdatedAt: 9,
                serverObservedAt: 9000,
            },
        ]);
        expect(complete.match(/data-stage="telegram"/g)).toHaveLength(2);
        expect(complete).toContain("25 / 100 B");
        expect(complete).toContain("50 / 100 B");
        expect(complete).toContain("10 B/s");
        expect(complete).toContain("30 B/s");
        expect(complete).not.toContain("Uploading from device");
        expect(complete).not.toContain('class="transfer-phase">Received');
    });

    it("keeps discovered upload jobs received while local device work is pending", () => {
        const server = local({
            batchId: "server:job",
            initiatedHere: false,
            jobId: "server-job",
            state: "uploading",
            stage: "uploading",
            phase: "Uploading to Telegram",
            current: 25,
            speedBytesPerSecond: 10,
            progressUpdatedAt: 9,
            serverObservedAt: 9000,
        });
        const pending = local({ key: "pending", batchId: "device" });
        const html = renderExpandedBatches([server, pending]);
        expect(html).toContain('class="transfer-phase">Received');
        expect(html).not.toContain("Telegram");
        expect(html).not.toContain("25 / 100 B");
        expect(html).not.toContain("10 B/s");

        const complete = renderExpandedBatches([server]);
        expect(complete).toContain("Uploading to Telegram");
        expect(complete).toContain("25 / 100 B");
        expect(complete).toContain("10 B/s");
    });

    it("keeps single-file folder summaries free of redundant completion counts", () => {
        const folder = local({
            batchId: "single-folder",
            relativePath: "Pictures/Photo.jpg",
            state: "receiving",
            phase: "Uploading from your device",
        });
        const receiving = render([folder]);
        expect(receiving).toContain("Uploading from device");
        expect(receiving).not.toContain("0 of 1");
        expect(receiving).not.toContain("Uploading to Telegram");

        const accepted = render([
            {
                ...folder,
                jobId: "accepted",
                state: "uploading",
                phase: "Uploading to Telegram",
                stage: "uploading",
            },
        ]);
        expect(accepted).toContain("Uploading to Telegram");
        expect(accepted).not.toContain("0 of 1");
        expect(accepted).not.toContain("Uploading from device");

        const ordinary = render([
            { ...folder, relativePath: undefined, folderUpload: undefined },
        ]);
        expect(ordinary).not.toContain("0 of 1");
        expect(ordinary).toContain("Uploading from your device");
        expect(ordinary).not.toContain("transfer-batch-status");
    });

    it("preserves attention and recovery actions for a single-file folder", () => {
        vi.stubGlobal("document", {
            createElement: () => ({ webkitdirectory: "" }),
        });
        const html = render([
            local({
                batchId: "single-folder",
                relativePath: "Pictures/Photo.jpg",
                state: "error",
                error: "Connection interrupted",
            }),
        ]);
        expect(html).toContain("Uploading from device · 1 failed");
        expect(html).toContain("Choose Folder");
        expect(html).not.toContain("0 of 1");
    });

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
        expect(html).toContain("Uploading from device · 1 of 2 received");
        expect(html).toContain("Keep this tab open");
        expect(html).not.toContain("You can close this tab");
        expect(html).not.toContain("transfer-child");
    });

    it("offers to close the tab only after every standalone upload is accepted", () => {
        const accepted = local({ jobId: "accepted", state: "uploading" });
        const pending = local({ key: "second", name: "Second.jpg" });
        expect(render([accepted, pending])).not.toContain(
            "You can close this tab",
        );
        expect(render([accepted, pending])).toContain(
            "Keep this tab open for other uploads",
        );
        expect(
            render([
                accepted,
                { ...pending, jobId: "second-job", state: "uploading" },
            ]),
        ).toContain("You can close this tab");
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
            render([
                local({ kind: "remote", jobId: "url-job", state: "uploading" }),
            ]),
        ).not.toContain("transfer-device-status");
    });

    it("shows ongoing work above collapsed tab guidance without adding an overlay row", () => {
        vi.stubGlobal("window", {
            history: { state: { tgdriveReveal: true } },
            matchMedia: () => ({ matches: false }),
        });
        const pending = render([local({ state: "receiving" })], false);
        const pendingHeader = pending.split("</h2>")[0];
        expect(pendingHeader).toContain("<span>1 uploading</span>");
        expect(pendingHeader).toContain(
            'class="transfer-collapsed-guidance">Keep this tab open',
        );
        expect(pendingHeader).toContain('role="status" aria-atomic="true"');
        expect(pending).not.toContain('class="transfer-notice"');
        const accepted = render(
            [local({ jobId: "accepted", state: "uploading" })],
            false,
        );
        const acceptedHeader = accepted.split("</h2>")[0];
        expect(acceptedHeader).toContain("<span>1 uploading</span>");
        expect(acceptedHeader).toContain(
            'class="transfer-collapsed-guidance">You can close this tab',
        );
        expect(accepted).not.toContain('class="transfer-notice"');
    });

    it("counts folder tasks once and keeps mixed attention visible beside active and queued work", () => {
        const html = renderCollapsedSummary([
            local({
                key: "sending",
                batchId: "folder",
                relativePath: "Pictures/Photo.jpg",
                state: "receiving",
            }),
            local({
                key: "original-needed",
                batchId: "folder",
                relativePath: "Pictures/Second.jpg",
                state: "awaiting_file",
            }),
            local({ key: "queued" }),
            local({ key: "failed", state: "error" }),
            local({ key: "old-success", state: "completed", jobId: "old" }),
        ]);
        expect(html).toContain("1 uploading · 1 queued · 2 need attention");
        expect(html).toContain("Keep this tab open");
        expect(html).not.toContain("completed");
        expect(html).not.toContain("Uploads complete");
    });

    it("asks for details when minimized uploads are blocked on originals", () => {
        const html = renderCollapsedSummary([
            local({ state: "awaiting_file" }),
        ]);
        expect(html).toContain("1 needs attention");
        expect(html).toContain("Open details to continue");
        expect(html).not.toContain("Keep this tab open");
        expect(html).not.toContain("uploading");
    });

    it.each([
        ["completed", "Uploads complete"],
        ["cancelled", "Uploads cancelled"],
    ] as const)(
        "shows %s without counting old finished history",
        (state, label) => {
            const html = renderCollapsedSummary(
                Array.from({ length: 29 }, (_, index) =>
                    local({ key: "finished-" + index, state }),
                ),
            );
            expect(html).toContain("<span>" + label + "</span>");
            expect(html).not.toContain("transfer-collapsed-guidance");
            expect(html).not.toContain("uploading");
        },
    );

    it("uses a neutral finished status when completions and cancellations are mixed", () => {
        const html = renderCollapsedSummary([
            local({ state: "completed" }),
            local({ key: "cancelled", state: "cancelled" }),
        ]);
        expect(html).toContain("Uploads finished");
        expect(html).not.toContain("Uploads complete");
    });

    it("does not call cancelling work uploading", () => {
        const html = renderCollapsedSummary([local({ state: "cancelling" })]);
        expect(html).toContain("1 cancelling");
        expect(html).not.toContain("uploading");
    });

    it("shows only device bytes, rates and received child states before handoff", () => {
        const first = local({
            batchId: "folder",
            relativePath: "Pictures/Photo.jpg",
            jobId: "accepted",
            state: "uploading",
            stage: "uploading",
            phase: "Uploading to Telegram",
            current: 25,
            speedBytesPerSecond: 10,
            progressUpdatedAt: 9,
            serverObservedAt: 9000,
        });
        const second = local({
            batchId: "folder",
            key: "second",
            relativePath: "Pictures/Second.jpg",
            state: "receiving",
            phase: "Uploading from your device",
            current: 20,
            sentCurrent: 40,
            meter: {
                stage: "receipt",
                bytes: 40,
                at: 9000,
                advancedAt: 9000,
                rate: 20,
            },
        });
        const batch = summarizeBatches([first, second], 10000)[0];
        const html = renderToStaticMarkup(
            createElement(BatchTransfer, {
                batch,
                deviceUploadsPending: true,
                expanded: true,
                onToggleExpanded: () => {},
                now: 10000,
                run: () => {},
                onChooseFile: () => {},
                onChooseBatch: () => {},
                folderPickerSupported: true,
            }),
        );
        expect(html).toContain('data-stage="device"');
        expect(html).toContain("140 / 200 B");
        expect(html).toContain("70%");
        expect(html).toContain("20 B/s");
        expect(html).toContain('class="transfer-phase">Received');
        expect(html).not.toContain("Telegram");
        expect(html).not.toContain("10 B/s");
        expect(html).not.toContain("25 / 100 B");
        expect(html).not.toContain("Done");

        const accepted = render([
            first,
            {
                ...second,
                jobId: "second-job",
                state: "uploading",
                stage: "queued",
                current: 0,
            },
        ]);
        expect(accepted).toContain('data-stage="telegram"');
        expect(accepted).toContain("Uploading to Telegram");
        expect(accepted).toContain("25 / 200 B");
        expect(accepted).not.toContain("Uploading from device");
    });

    it("counts a running folder as one task while keeping its file total", () => {
        const html = render([
            local({
                batchId: "folder",
                relativePath: "Pictures/Photo.jpg",
                state: "completed",
                jobId: "first",
            }),
            local({
                key: "second",
                batchId: "folder",
                relativePath: "Pictures/Second.jpg",
                state: "receiving",
            }),
        ]);
        expect(html).toMatch(/Running<span>1<\/span>/);
        expect(html).toContain("2 files");
    });

    it("renders files from one ordinary selection as separate tasks", () => {
        const selected = ["First.jpg", "Second.jpg", "Third.jpg"].map(
            (name, index) =>
                local({
                    key: "selected-" + index,
                    batchId: "selected-" + index,
                    batchName: "3 files",
                    name,
                    state: "receiving",
                }),
        );
        const html = render(selected);
        expect(html).toContain("3 transfers shown");
        expect(html).toMatch(/Running<span>3<\/span>/);
        expect(html.match(/class="transfer-item"/g)).toHaveLength(3);
        expect(html).not.toContain('class="transfer-batch"');
        expect(html).not.toContain('aria-label="Show Files"');
        for (const item of selected) expect(html).toContain(item.name);
        expect(render(selected, false)).toContain("3 uploads in progress");
    });

    it("counts a 72-file folder and two ordinary files as three tasks", () => {
        const folder = Array.from({ length: 72 }, (_, index) =>
            local({
                key: "folder-" + index,
                name: index + ".jpg",
                batchId: "folder",
                batchName: "Pictures",
                relativePath: "Pictures/" + index + ".jpg",
                state: "receiving",
            }),
        );
        const ordinary = ["First.jpg", "Second.jpg"].map((name, index) =>
            local({
                key: "ordinary-" + index,
                batchId: "selection",
                name,
                state: "receiving",
            }),
        );
        const html = render([...folder, ...ordinary]);
        expect(html).toContain("3 transfers shown");
        expect(html).toMatch(/Running<span>3<\/span>/);
        expect(html.match(/class="transfer-batch"/g)).toHaveLength(1);
        expect(html.match(/class="transfer-item"/g)).toHaveLength(2);
        expect(html).toContain("72 files");
    });

    it("keeps a one-file folder grouped using server folder metadata alone", () => {
        const html = render([
            local({
                folderUpload: {
                    id: "folder-task",
                    name: "Pictures",
                    root_path: "/Pictures",
                    created_at: 1,
                },
                state: "receiving",
            }),
        ]);
        expect(html).toContain('class="transfer-batch"');
        expect(html).toContain('data-folder="true"');
        expect(html).toContain('aria-label="Show Files"');
        expect(html).toContain("Pictures");
        expect(html).toContain("1 file");
        expect(html).toMatch(/Running<span>1<\/span>/);
    });

    it("uses the uploaded folder root for a nested server history member", () => {
        const html = render([
            local({
                folderUpload: {
                    id: "folder-task",
                    name: "Pictures",
                    root_path: "/Pictures",
                    created_at: 1,
                },
                destination: "/Pictures/Nested",
                state: "completed",
                jobId: "server-file",
                historyOnly: true,
                location: {
                    available: true,
                    folder_label: "My Files / Pictures / Nested",
                    folder_path: "/Pictures/Nested",
                },
            }),
        ]);
        expect(html).toContain('title="My Files / Pictures"');
        expect(html).not.toContain('title="My Files / Pictures / Nested"');
    });

    it("counts finished folders and ordinary files by task", () => {
        const items = [
            local({
                batchId: "folder",
                relativePath: "Pictures/Photo.jpg",
                state: "completed",
                jobId: "first",
            }),
            local({
                key: "folder-second",
                batchId: "folder",
                relativePath: "Pictures/Second.jpg",
                state: "completed",
                jobId: "second",
            }),
            local({
                key: "ordinary",
                name: "Readme.txt",
                state: "completed",
                jobId: "third",
            }),
        ];
        expect(render(items)).toMatch(/Finished<span>2<\/span>/);
    });
});
