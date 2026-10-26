import { describe, expect, it } from "vitest";
import {
    clientConnectionLabel,
    clientDisplayName,
    createConnectionDraft,
    isCredentialRepair,
    MAX_STORAGE_CLIENTS,
    prepareConnectionUpdate,
} from "./settingsManagement";
import type { ManagementSettings } from "../../shared/types";

const snapshot = (): ManagementSettings => ({
    revision: 3,
    applied_revision: 3,
    restart_required: false,
    application_mode: "restart",
    admission_paused: false,
    api_id: 12345,
    api_hash_configured: true,
    storage_channel: -1001234567890,
    clients: [
        {
            id: "configured-bot",
            label: "Storage Bot",
            kind: "bot",
            enabled: true,
            configured: true,
            connected: true,
            healthy: true,
            premium: null,
            active_leases: 2,
            error: null,
        },
    ],
    active_clients: [],
    stats: {
        files: 1,
        folders: 2,
        total_bytes: 3,
        trashed_files: 0,
        trashed_bytes: 0,
        active_jobs: 1,
        queued_jobs: 2,
        failed_jobs: 0,
        staging_budget_bytes: 1000,
        staging_reserved_bytes: 10,
        free_disk_bytes: 2000,
    },
});

it("permits credential repair without treating account removal as repair", () => {
    const settings = snapshot();
    const draft = createConnectionDraft(settings);
    expect(isCredentialRepair(draft, settings)).toBe(false);
    draft.clients[0].credential = "123456:replacement";
    expect(isCredentialRepair(draft, settings)).toBe(true);
    draft.clients[0].removed = true;
    expect(isCredentialRepair(draft, settings)).toBe(false);
    draft.clients[0].removed = false;
    draft.clients[0].enabled = false;
    expect(isCredentialRepair(draft, settings)).toBe(false);
    draft.clients[0].enabled = true;
    draft.apiId = "99999";
    expect(isCredentialRepair(draft, settings)).toBe(false);
});

describe("write-only connection settings", () => {
    it("never prepopulates a credential from health/configuration", () => {
        const draft = createConnectionDraft(snapshot());
        expect(draft.apiHash).toBe("");
        expect(draft.clients[0].credential).toBe("");
        expect(draft.clients[0]).not.toHaveProperty("connected");
        expect(draft.clients[0]).not.toHaveProperty("active_leases");
    });
    it("preserves existing secrets by omission and sends only writable fields", () => {
        const update = prepareConnectionUpdate(
            createConnectionDraft(snapshot()),
        );
        expect(update).toEqual({
            expected_revision: 3,
            api_id: 12345,
            clients: [
                {
                    id: "configured-bot",
                    label: "Storage Bot",
                    kind: "bot",
                    enabled: true,
                },
            ],
        });
        expect(update).not.toHaveProperty("api_hash");
        expect(update).not.toHaveProperty("storage_channel");
        expect(update.clients?.[0]).not.toHaveProperty("credential");
    });
    it("uses a write-only replacement without leaking it into validation errors", () => {
        const draft = createConnectionDraft(snapshot());
        draft.apiHash = " a".trim().repeat(32);
        draft.clients[0].credential = "12345:" + "x".repeat(25);
        const update = prepareConnectionUpdate(draft);
        expect(update.api_hash).toBe("a".repeat(32));
        expect(update.clients?.[0].credential).toBe("12345:" + "x".repeat(25));
        draft.clients[0].credential = "very-private-invalid-value";
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "valid Telegram bot token",
        );
        try {
            prepareConnectionUpdate(draft);
        } catch (error) {
            expect(String(error)).not.toContain("very-private-invalid-value");
        }
    });
    it.each(["0", "-1", "1.5", "2147483648", "1e5", "NaN"])(
        "rejects invalid API ID %s",
        (value) => {
            const draft = createConnectionDraft(snapshot());
            draft.apiId = value;
            expect(() => prepareConnectionUpdate(draft)).toThrow("API ID");
        },
    );
    it("requires a credential for new clients and does not send their local key as an ID", () => {
        const draft = createConnectionDraft(snapshot());
        draft.clients.push({
            key: "browser-only-key",
            label: "My Premium account",
            kind: "user",
            enabled: true,
            credential: "",
            removed: false,
        });
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "Telethon Session",
        );
        draft.clients[1].credential = "a-session-to-be-validated-by-the-server";
        const client = prepareConnectionUpdate(draft).clients?.[1];
        expect(client).not.toHaveProperty("id");
        expect(client).not.toHaveProperty("key");
    });
    it("omits removed rows but allows undo before saving", () => {
        const draft = createConnectionDraft(snapshot());
        draft.clients.push({
            ...draft.clients[0],
            key: "second",
            id: "second",
            label: "Second bot",
        });
        draft.clients[1].removed = true;
        expect(prepareConnectionUpdate(draft).clients).toHaveLength(1);
        draft.clients[1].removed = false;
        expect(prepareConnectionUpdate(draft).clients).toHaveLength(2);
    });
    it("requires an enabled bot even if Premium accounts exist", () => {
        const draft = createConnectionDraft(snapshot());
        draft.clients[0].enabled = false;
        draft.clients.push({
            key: "user",
            id: "user",
            label: "Premium",
            kind: "user",
            enabled: true,
            credential: "",
            removed: false,
        });
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "enabled storage bot",
        );
        draft.clients[0].removed = true;
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "enabled storage bot",
        );
    });
    it("bounds the saved client count and validates hidden disabled-new credentials too", () => {
        const draft = createConnectionDraft(snapshot());
        draft.clients = Array.from(
            { length: MAX_STORAGE_CLIENTS + 1 },
            (_, i) => ({
                ...draft.clients[0],
                key: String(i),
                id: String(i),
            }),
        );
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "50 storage clients",
        );
        draft.clients = draft.clients.slice(0, MAX_STORAGE_CLIENTS);
        expect(prepareConnectionUpdate(draft).clients).toHaveLength(
            MAX_STORAGE_CLIENTS,
        );
        draft.clients = draft.clients.slice(0, MAX_STORAGE_CLIENTS - 1);
        draft.clients.push({
            key: "new",
            kind: "bot",
            label: "Disabled bot",
            enabled: false,
            credential: "",
            removed: false,
        });
        expect(() => prepareConnectionUpdate(draft)).toThrow("bot token");
    });
    it("validates names and API Hashes without echoing values", () => {
        const draft = createConnectionDraft(snapshot());
        draft.apiHash = "secret-invalid-hash";
        expect(() => prepareConnectionUpdate(draft)).toThrow("32 hexadecimal");
        draft.apiHash = "";
        draft.clients[0].label = "line\nbreak";
        expect(() => prepareConnectionUpdate(draft)).toThrow(
            "control characters",
        );
    });
    it("distinguishes disabled saved intent from connected runtime health", () => {
        expect(
            clientConnectionLabel({
                enabled: false,
                connected: true,
                healthy: true,
            }),
        ).toBe("Disabled in saved configuration");
        expect(
            clientConnectionLabel({
                enabled: true,
                connected: false,
                healthy: true,
            }),
        ).toBe("Disconnected");
        expect(
            clientConnectionLabel({
                enabled: true,
                connected: null,
                healthy: false,
            }),
        ).toBe("Not connected");
        expect(
            clientConnectionLabel({
                enabled: true,
                connected: true,
                healthy: false,
            }),
        ).toBe("Connected · needs attention");
        expect(
            clientConnectionLabel({
                enabled: true,
                connected: true,
                healthy: true,
            }),
        ).toBe("Connected");
    });
});

describe("Telegram connection names", () => {
    it("prefers the Telegram name without changing the saved local label", () => {
        const client = {
            ...snapshot().clients[0],
            display_name: "Drive · संग्रह",
            username: "drive_bot",
        };
        expect(clientDisplayName(client)).toBe("Drive · संग्रह");
        const value = snapshot();
        value.clients[0] = client;
        expect(
            prepareConnectionUpdate(createConnectionDraft(value)).clients?.[0]
                .label,
        ).toBe("Storage Bot");
    });
    it("uses the username then local label when a Telegram name is unavailable", () => {
        expect(
            clientDisplayName({
                label: "Backup",
                display_name: null,
                username: "drive_bot",
            }),
        ).toBe("@drive_bot");
        expect(clientDisplayName({ label: "Backup" })).toBe("Backup");
    });
});

describe("manual connection verification status", () => {
    it("shows verification failures even while an existing socket is connected", () => {
        const client = { enabled: true, connected: true, healthy: true };
        expect(
            clientConnectionLabel({ ...client, connection_check: "checking" }),
        ).toBe("Checking connection…");
        expect(
            clientConnectionLabel({ ...client, connection_check: "failed" }),
        ).toBe("Check failed");
        expect(
            clientConnectionLabel({ ...client, connection_check: "waiting" }),
        ).toBe("Waiting to retry…");
        expect(
            clientConnectionLabel({ ...client, connection_check: "passed" }),
        ).toBe("Connected");
        expect(
            clientConnectionLabel({
                ...client,
                connected: false,
                connection_check: "passed",
            }),
        ).toBe("Verified · runtime disconnected");
    });
});
