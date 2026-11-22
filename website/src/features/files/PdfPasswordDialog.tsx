import { NativeSelect, PasswordInput, Stack } from "@mantine/core";
import { LockKeyhole } from "lucide-react";
import { useState } from "react";
import { ApiError, errorMessage, request } from "../../shared/api";
import { ErrorNotice, Modal, Spinner } from "../../shared/components";
import {
    itemPath,
    type DriveItem,
    type Job,
    type PdfPasswordOperation,
} from "../../shared/types";
import { transfers } from "../transfers/transfers";

export function PdfPasswordFields({
    operation,
    password,
    setPassword,
    newPassword,
    setNewPassword,
    busy,
}: {
    operation: PdfPasswordOperation;
    password: string;
    setPassword: (value: string) => void;
    newPassword: string;
    setNewPassword: (value: string) => void;
    busy: boolean;
}) {
    return (
        <>
            {operation !== "add" && (
                <PasswordInput
                    label="Current PDF password"
                    autoComplete="off"
                    required
                    value={password}
                    maxLength={127}
                    disabled={busy}
                    onChange={(event) => setPassword(event.currentTarget.value)}
                />
            )}
            {operation !== "remove" && (
                <PasswordInput
                    label="New PDF password"
                    autoComplete="new-password"
                    required
                    value={newPassword}
                    maxLength={127}
                    disabled={busy}
                    onChange={(event) =>
                        setNewPassword(event.currentTarget.value)
                    }
                />
            )}
        </>
    );
}

export default function PdfPasswordDialog({
    item,
    onClose,
    onNotice,
}: {
    item: DriveItem;
    onClose: () => void;
    onNotice: (message: string) => void;
}) {
    const [operation, setOperation] = useState<PdfPasswordOperation>("add");
    const [password, setPassword] = useState("");
    const [newPassword, setNewPassword] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [exposure, setExposure] = useState(false);
    const [allow, setAllow] = useState(false);
    const destination = item.path || "/";
    async function submit() {
        setBusy(true);
        setError("");
        try {
            const { id, job } = await request<{ id: string; job: Job }>(
                "/api/v1/pdfs/password",
                {
                    path: itemPath(item),
                    destination,
                    operation,
                    password: operation === "add" ? undefined : password,
                    new_password:
                        operation === "remove" ? undefined : newPassword,
                    allow_public_exposure: allow,
                },
            );
            transfers.addJob(
                id,
                job.name || item.name,
                destination,
                "pdf_password",
            );
            onNotice("PDF copy queued. Follow it in Transfers.");
            onClose();
        } catch (cause) {
            if (
                cause instanceof ApiError &&
                cause.code.includes("PUBLIC_EXPOSURE")
            )
                setExposure(true);
            setError(errorMessage(cause));
        } finally {
            setBusy(false);
        }
    }
    return (
        <Modal
            title="PDF password"
            description="The original stays unchanged."
            initialFocus="#pdf-password-action"
            icon={<LockKeyhole />}
            onClose={() => !busy && onClose()}
        >
            <form
                onSubmit={(event) => {
                    event.preventDefault();
                    if (!busy) void submit();
                }}
            >
                <div className="archive-summary">
                    <p className="archive-source">{item.name}</p>
                    <p className="archive-location">
                        <strong>
                            {operation === "remove"
                                ? "Unlocked copy"
                                : "Protected copy"}
                        </strong>{" "}
                        in the same folder
                    </p>
                </div>
                <Stack gap="sm">
                    <NativeSelect
                        id="pdf-password-action"
                        label="Action"
                        value={operation}
                        disabled={busy}
                        data={[
                            { value: "add", label: "Add password" },
                            { value: "change", label: "Change password" },
                            { value: "remove", label: "Remove password" },
                        ]}
                        onChange={(event) => {
                            setOperation(
                                event.currentTarget
                                    .value as PdfPasswordOperation,
                            );
                            setError("");
                            setPassword("");
                            setNewPassword("");
                        }}
                    />
                    <PdfPasswordFields
                        {...{
                            operation,
                            password,
                            setPassword,
                            newPassword,
                            setNewPassword,
                            busy,
                        }}
                    />
                    <ErrorNotice message={error} />
                    {exposure && (
                        <label className="check-field">
                            <input
                                type="checkbox"
                                checked={allow}
                                disabled={busy}
                                onChange={(event) =>
                                    setAllow(event.target.checked)
                                }
                            />
                            The new file may be accessible through a shared
                            link.
                        </label>
                    )}
                </Stack>
                <div className="dialog-actions">
                    <button
                        type="button"
                        className="secondary-button"
                        disabled={busy}
                        onClick={onClose}
                    >
                        Cancel
                    </button>
                    <button
                        className="primary-button"
                        disabled={busy || (exposure && !allow)}
                    >
                        {busy && <Spinner />} Save PDF copy
                    </button>
                </div>
            </form>
        </Modal>
    );
}
