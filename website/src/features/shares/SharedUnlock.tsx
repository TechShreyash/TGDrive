import {
    Button,
    Paper,
    PasswordInput,
    Stack,
    Text,
    Title,
} from "@mantine/core";
import { LockKeyhole } from "lucide-react";
import { useRef, useState } from "react";
import { ApiError, errorMessage, request } from "../../shared/api";
import BrandLogo from "../../shared/BrandLogo";
import "./SharedUnlock.css";

export function needsSharePassword(error: unknown) {
    return (
        error instanceof ApiError && error.code === "SHARE_PASSWORD_REQUIRED"
    );
}

export default function SharedUnlock({
    auth,
    embed = false,
    needsTopLevel = false,
    onUnlocked,
}: {
    auth: string;
    embed?: boolean;
    needsTopLevel?: boolean;
    onUnlocked: () => void;
}) {
    const inFrame = embed && window.parent !== window;
    const [password, setPassword] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const passwordField = useRef<HTMLInputElement>(null);
    async function unlock() {
        setBusy(true);
        setError("");
        try {
            await request(
                "/api/v1/shared/unlock",
                { auth, password },
                {
                    notifySessionExpiry: false,
                },
            );
            setPassword("");
            onUnlocked();
        } catch (cause) {
            setError(errorMessage(cause));
            setPassword("");
            passwordField.current?.focus();
        } finally {
            setBusy(false);
        }
    }
    return (
        <main
            id="main-content"
            className={
                inFrame ? "shared-unlock shared-unlock-framed" : "shared-unlock"
            }
        >
            {!inFrame && (
                <div className="shared-unlock-brand">
                    <BrandLogo />
                </div>
            )}
            <Paper
                component="section"
                aria-labelledby="share-unlock-title"
                className="shared-unlock-panel"
            >
                <div className="shared-unlock-intro">
                    <span className="shared-unlock-icon">
                        <LockKeyhole size={21} aria-hidden="true" />
                    </span>
                    <div>
                        <Title order={1} id="share-unlock-title">
                            Password-protected link
                        </Title>
                        <Text size="sm" c="var(--muted)">
                            {needsTopLevel
                                ? "Your browser could not keep the unlock in this embed. Open the link in a new tab to continue."
                                : "Enter the password from the person who shared this link."}
                        </Text>
                    </div>
                </div>
                {needsTopLevel ? (
                    <Button
                        component="a"
                        className="primary-button"
                        href={window.location.href}
                        target="_blank"
                        rel="noopener"
                        fullWidth
                    >
                        Open in a new tab
                    </Button>
                ) : (
                    <form
                        onSubmit={(event) => {
                            event.preventDefault();
                            if (!busy && password) void unlock();
                        }}
                    >
                        <Stack gap="md">
                            <PasswordInput
                                ref={passwordField}
                                label="Share password"
                                size="md"
                                value={password}
                                maxLength={255}
                                autoComplete="off"
                                autoCapitalize="none"
                                spellCheck={false}
                                required
                                withAsterisk={false}
                                aria-invalid={Boolean(error)}
                                rightSectionWidth={44}
                                readOnly={busy}
                                description={error || undefined}
                                descriptionProps={{
                                    role: "alert",
                                    c: "var(--danger)",
                                    mt: 8,
                                }}
                                inputWrapperOrder={[
                                    "label",
                                    "input",
                                    "description",
                                ]}
                                onChange={(event) => {
                                    setPassword(event.currentTarget.value);
                                    if (error) setError("");
                                }}
                            />
                            <Button
                                type="submit"
                                className="primary-button"
                                size="md"
                                loading={busy}
                            >
                                Open shared link
                            </Button>
                            {inFrame && (
                                <a
                                    href={window.location.href}
                                    target="_blank"
                                    rel="noopener"
                                    className="inline-link shared-unlock-outside"
                                >
                                    Open in a new tab
                                </a>
                            )}
                        </Stack>
                    </form>
                )}
            </Paper>
        </main>
    );
}
