import { useRef, useState, type FormEvent } from "react";
import BrandLogo from "../shared/BrandLogo";
import { api, errorMessage } from "../shared/api";
import { ErrorNotice, Spinner } from "../shared/components";

export default function Login({ onSuccess }: { onSuccess: () => void }) {
    const [password, setPassword] = useState("");
    const [shown, setShown] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const field = useRef<HTMLInputElement>(null);
    const submit = async (event: FormEvent) => {
        event.preventDefault();
        if (busy) return;
        setBusy(true);
        setError("");
        try {
            await api.login(password);
            setPassword("");
            onSuccess();
        } catch (error) {
            setError(errorMessage(error));
            field.current?.focus();
        } finally {
            setBusy(false);
        }
    };
    return (
        <main className="login-page">
            <a href="/" className="brand">
                <BrandLogo size="large" />
            </a>
            <section
                id="login-dialog"
                className="login-form"
                aria-labelledby="login-title"
            >
                <h1 id="login-title">Log in to TGDrive</h1>
                <p className="login-intro">
                    Use the owner password for this drive.
                </p>
                <form onSubmit={(event) => void submit(event)}>
                    <label htmlFor="owner-password">Password</label>
                    <div className="password-field">
                        <input
                            ref={field}
                            id="owner-password"
                            autoFocus
                            type={shown ? "text" : "password"}
                            autoComplete="current-password"
                            autoCapitalize="none"
                            autoCorrect="off"
                            spellCheck={false}
                            required
                            value={password}
                            readOnly={busy}
                            aria-describedby={
                                error ? "owner-password-error" : undefined
                            }
                            onChange={(event) => {
                                setPassword(event.target.value);
                                if (error) setError("");
                            }}
                            maxLength={512}
                        />
                        <button
                            type="button"
                            className="text-button"
                            aria-label={
                                shown ? "Hide password" : "Show password"
                            }
                            aria-controls="owner-password"
                            onClick={() => setShown(!shown)}
                        >
                            {shown ? "Hide" : "Show"}
                        </button>
                    </div>
                    <ErrorNotice id="owner-password-error" message={error} />
                    <button
                        type="submit"
                        className="primary-button full-width"
                        disabled={busy}
                        aria-busy={busy}
                    >
                        {busy ? (
                            <>
                                <Spinner /> Logging in…
                            </>
                        ) : (
                            "Log in"
                        )}
                    </button>
                </form>
            </section>
        </main>
    );
}
