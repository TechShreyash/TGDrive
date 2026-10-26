import type {
    ManagedClient,
    ManagementSettings,
    ManagementUpdate,
} from "../../shared/types";

export const MAX_STORAGE_CLIENTS = 50;

export interface ClientDraft {
    key: string;
    id?: string;
    label: string;
    kind: "bot" | "user";
    enabled: boolean;
    credential: string;
    removed: boolean;
}
export interface ConnectionDraft {
    revision: number;
    apiId: string;
    apiHash: string;
    clients: ClientDraft[];
}
export function isCredentialRepair(
    draft: ConnectionDraft | null,
    settings: ManagementSettings | null,
): boolean {
    if (
        !draft ||
        !settings ||
        draft.apiId.trim() !== String(settings.api_id) ||
        draft.apiHash.trim() ||
        draft.clients.length !== settings.clients.length
    )
        return false;
    return (
        draft.clients.some((row) => row.credential.trim()) &&
        draft.clients.every((row, index) => {
            const prior = settings.clients[index];
            return (
                row.id === prior.id &&
                !row.removed &&
                row.kind === prior.kind &&
                row.enabled === prior.enabled
            );
        })
    );
}

export function createConnectionDraft(
    settings: ManagementSettings,
): ConnectionDraft {
    return {
        revision: settings.revision,
        apiId: String(settings.api_id),
        apiHash: "",
        clients: settings.clients.map((client) => ({
            key: client.id,
            id: client.id,
            label: client.label,
            kind: client.kind,
            enabled: client.enabled,
            credential: "",
            removed: false,
        })),
    };
}
export function prepareConnectionUpdate(
    draft: ConnectionDraft,
): ManagementUpdate {
    if (
        !/^[1-9]\d*$/.test(draft.apiId.trim()) ||
        !Number.isSafeInteger(Number(draft.apiId.trim())) ||
        Number(draft.apiId.trim()) > 2147483647
    ) {
        throw new Error("Enter a Telegram API ID between 1 and 2147483647.");
    }
    const apiHash = draft.apiHash.trim();
    if (apiHash && !/^[a-fA-F0-9]{32}$/.test(apiHash)) {
        throw new Error(
            "The API Hash must contain exactly 32 hexadecimal characters.",
        );
    }
    if (
        draft.clients.filter((client) => !client.removed).length >
        MAX_STORAGE_CLIENTS
    )
        throw new Error(
            `You can configure up to ${MAX_STORAGE_CLIENTS} storage clients.`,
        );
    const clients = draft.clients
        .filter((client) => !client.removed)
        .map((client) => {
            const label = client.label.trim();
            if (
                !label ||
                label.length > 80 ||
                /[\u0000-\u001f\u007f-\u009f]/.test(label)
            )
                throw new Error(
                    "Give each client a name of 1–80 characters without control characters.",
                );
            const credential = client.credential.trim();
            if (!client.id && !credential)
                throw new Error(
                    `Enter the ${client.kind === "bot" ? "bot token" : "Telethon Session"} for ${label}.`,
                );
            if (credential.length > 8192)
                throw new Error(
                    `The credential for ${label} is too long. Check the copied value.`,
                );
            if (
                credential &&
                client.kind === "bot" &&
                !/^[1-9][0-9]{4,14}:[A-Za-z0-9_-]{20,}$/.test(credential)
            ) {
                throw new Error(
                    `Enter a valid Telegram bot token for ${label}.`,
                );
            }
            return {
                ...(client.id ? { id: client.id } : {}),
                label,
                kind: client.kind,
                enabled: client.enabled,
                ...(credential ? { credential } : {}),
            };
        });
    if (!clients.some((client) => client.kind === "bot" && client.enabled))
        throw new Error("Keep at least one enabled storage bot.");
    return {
        expected_revision: draft.revision,
        api_id: Number(draft.apiId.trim()),
        ...(apiHash ? { api_hash: apiHash } : {}),
        clients,
    };
}
export function clientConnectionLabel(client: {
    enabled: boolean;
    connected: boolean | null;
    healthy: boolean;
    connection_check?: ManagedClient["connection_check"];
}): string {
    if (!client.enabled) return "Disabled in saved configuration";
    if (client.connection_check === "queued") return "Waiting to check";
    if (client.connection_check === "checking") return "Checking connection…";
    if (client.connection_check === "waiting") return "Waiting to retry…";
    if (client.connection_check === "failed") return "Check failed";
    if (
        client.connection_check === "passed" &&
        !(client.connected && client.healthy)
    )
        return "Verified · runtime disconnected";
    if (client.connected === false) return "Disconnected";
    if (client.connected === true && client.healthy) return "Connected";
    if (client.connected === true) return "Connected · needs attention";
    return "Not connected";
}

export function clientDisplayName(
    client: Pick<ManagedClient, "label" | "display_name" | "username">,
): string {
    return (
        client.display_name?.trim() ||
        (client.username ? "@" + client.username : client.label)
    );
}
