import { useState, type FormEvent } from "react";
import { errorMessage, request } from "../../shared/api";
import { Modal, Spinner } from "../../shared/components";

const CONFIRMATION = "START NEW";

/** Erase this drive's database, channel pin and local data, then start fresh. */
export default function StartNewDrive({
    demoMode = false,
}: {
    demoMode?: boolean;
}) {
    const [open, setOpen] = useState(false);
    const [confirm, setConfirm] = useState("");
    const [password, setPassword] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const submit = async (event: FormEvent) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
            // Starting the new runtime connects Telegram, which can take a while.
            await request(
                "/api/v1/start-new",
                { confirm, password },
                {
                    timeoutMs: 180000,
                },
            );
            window.location.assign("/");
        } catch (cause) {
            setError(errorMessage(cause));
            setBusy(false);
        }
    };
    return (
        <section
            className="settings-section"
            aria-labelledby="start-new-drive-heading"
        >
            <h2 id="start-new-drive-heading">Start a new drive</h2>
            <p>
                Erase this drive and start empty with the same configuration.
                Use this when the database or channel holds an old drive you do
                not want to restore.
            </p>
            <button
                type="button"
                className="danger-button"
                disabled={demoMode}
                onClick={() => setOpen(true)}
            >
                Start new drive…
            </button>
            {open && (
                <Modal
                    title="Start a new drive"
                    onClose={() => !busy && setOpen(false)}
                >
                    <form onSubmit={(event) => void submit(event)}>
                        <p>This permanently erases:</p>
                        <ul>
                            <li>
                                every file and folder entry, share link,
                                transfer and setting saved in MongoDB;
                            </li>
                            <li>
                                the pinned backup in the storage channel, so it
                                is not offered for restore again;
                            </li>
                            <li>
                                cached and partly uploaded files on this server.
                            </li>
                        </ul>
                        <p>
                            Messages already in your Telegram channel are kept
                            but will no longer appear in TGDrive. Your .env and
                            ENCRYPTION_KEY are kept. You will need to sign in
                            again.
                        </p>
                        <label>
                            Type {CONFIRMATION} to confirm
                            <input
                                value={confirm}
                                autoComplete="off"
                                onChange={(event) =>
                                    setConfirm(event.target.value)
                                }
                                disabled={busy}
                            />
                        </label>
                        <label>
                            Owner password
                            <input
                                type="password"
                                value={password}
                                autoComplete="current-password"
                                onChange={(event) =>
                                    setPassword(event.target.value)
                                }
                                disabled={busy}
                            />
                        </label>
                        {error && <p role="alert">{error}</p>}
                        <button
                            type="submit"
                            className="danger-button"
                            disabled={
                                busy || confirm !== CONFIRMATION || !password
                            }
                        >
                            {busy && <Spinner />}
                            {busy
                                ? "Starting new drive…"
                                : "Erase and start new"}
                        </button>
                    </form>
                </Modal>
            )}
        </section>
    );
}
