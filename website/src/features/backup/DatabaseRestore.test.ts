import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
    MAX_DATABASE_ARCHIVE_BYTES,
    databaseRecoveryKey,
    uploadDatabaseArchive,
    validateDatabaseArchive,
} from "./DatabaseRestore";

class MockXHR {
    static latest: MockXHR;
    upload = {
        onprogress: null as ((event: { loaded: number }) => void) | null,
        onload: null as (() => void) | null,
    };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    status = 200;
    responseText = JSON.stringify({ restart_required: true });
    withCredentials = false;
    timeout = 60000;
    open = vi.fn();
    setRequestHeader = vi.fn();
    send = vi.fn();
    constructor() {
        MockXHR.latest = this;
    }
}

beforeEach(() => vi.stubGlobal("XMLHttpRequest", MockXHR));
afterEach(() => vi.unstubAllGlobals());

describe("database archive input", () => {
    it.each([
        { name: "checkpoint.json", size: 100 },
        { name: "backup.tgdb", size: 0 },
        { name: "backup.tgdb", size: MAX_DATABASE_ARCHIVE_BYTES + 1 },
    ])("rejects an unusable archive %j", (file) => {
        expect(() => validateDatabaseArchive(file)).toThrow();
    });
    it("accepts an encrypted archive at the documented upload limit", () => {
        expect(() =>
            validateDatabaseArchive({
                name: "BACKUP.TGDB",
                size: MAX_DATABASE_ARCHIVE_BYTES,
            }),
        ).not.toThrow();
    });
    it("trims a copied key and rejects invalid keys without echoing them", () => {
        const key = "a".repeat(64);
        expect(databaseRecoveryKey(` ${key}\n`)).toBe(key);
        const invalid = "private-recovery-key-that-must-not-be-echoed";
        expect(() => databaseRecoveryKey(invalid)).toThrow(
            "64-character hexadecimal",
        );
        try {
            databaseRecoveryKey(invalid);
        } catch (error) {
            expect(String(error)).not.toContain(invalid);
        }
    });
});

describe("database restore transport", () => {
    const archive = new File(["encrypted archive"], "backup.tgdb");
    const key = "b".repeat(64);

    it("sends the file once with the private key in a header and waits for confirmation", async () => {
        const progress = vi.fn();
        const result = uploadDatabaseArchive(archive, key, progress);
        const xhr = MockXHR.latest;
        expect(xhr.open).toHaveBeenCalledWith("POST", "/api/v1/backup/restore");
        expect(xhr.setRequestHeader).toHaveBeenCalledWith(
            "X-TGDrive-Recovery-Key",
            key,
        );
        expect(xhr.setRequestHeader).toHaveBeenCalledWith(
            "X-TGDrive-Confirm",
            "RESTORE",
        );
        expect(xhr.setRequestHeader).toHaveBeenCalledWith(
            "Content-Type",
            "application/octet-stream",
        );
        expect(xhr.withCredentials).toBe(true);
        expect(xhr.timeout).toBe(0);
        expect(xhr.send).toHaveBeenCalledOnce();
        expect(xhr.send).toHaveBeenCalledWith(archive);
        xhr.upload.onprogress?.({ loaded: 3 });
        xhr.upload.onload?.();
        expect(progress.mock.calls.map(([bytes]) => bytes)).toEqual([
            3,
            archive.size,
        ]);
        xhr.onload?.();
        await expect(result).resolves.toBeUndefined();
        expect(xhr.send).toHaveBeenCalledOnce();
    });

    it("preserves a validation failure as a correctable form error", async () => {
        const result = uploadDatabaseArchive(archive, key, vi.fn());
        const rejection = expect(result).rejects.toMatchObject({
            message: "Archive authentication failed. Check its matching key.",
            status: 422,
            restartRequired: false,
        });
        const xhr = MockXHR.latest;
        xhr.status = 422;
        xhr.responseText = JSON.stringify({
            detail: "Archive authentication failed. Check its matching key.",
        });
        xhr.onload?.();
        await rejection;
        expect(xhr.send).toHaveBeenCalledOnce();
    });

    it("keeps a known pre-restore storage failure retryable without a restart", async () => {
        const result = uploadDatabaseArchive(archive, key, vi.fn());
        const rejection = expect(result).rejects.toMatchObject({
            status: 507,
            restartRequired: false,
        });
        const xhr = MockXHR.latest;
        xhr.status = 507;
        xhr.responseText = JSON.stringify({
            detail: "Check temporary storage before retrying.",
            restart_required: false,
        });
        xhr.onload?.();
        await rejection;
    });

    it.each(["server", "network", "unreadable-success"])(
        "requires restart and inspection after an uncertain %s outcome without replaying",
        async (failure) => {
            const result = uploadDatabaseArchive(archive, key, vi.fn());
            const rejection = expect(result).rejects.toMatchObject({
                restartRequired: true,
            });
            const xhr = MockXHR.latest;
            if (failure === "network") xhr.onerror?.();
            else {
                xhr.status = failure === "server" ? 503 : 200;
                xhr.responseText =
                    failure === "server"
                        ? JSON.stringify({
                              detail: "Restart TGDrive and inspect the restore.",
                          })
                        : "Connection interrupted";
                xhr.onload?.();
            }
            await rejection;
            expect(xhr.send).toHaveBeenCalledOnce();
        },
    );
});
