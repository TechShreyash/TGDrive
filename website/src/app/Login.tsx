import { useState, type FormEvent } from "react";
import { ShieldCheck } from "lucide-react";
import BrandLogo from "../shared/BrandLogo";
import { api, errorMessage } from "../shared/api";
import { ErrorNotice, Spinner } from "../shared/components";

export default function Login({ onSuccess }: { onSuccess: () => void }) {
    const [password, setPassword] = useState("");
    const [shown, setShown] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const submit = async (event: FormEvent) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        try {
            await api.login(password);
            setPassword("");
            onSuccess();
        } catch (error) {
            setError(errorMessage(error));
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
                <ShieldCheck className="login-symbol" size={36} />
                <h1 id="login-title">Your files, your space.</h1>
                <p>Log in to your private drive.</p>
                <form onSubmit={(event) => void submit(event)}>
                    <label htmlFor="owner-password">Password</label>
                    <div className="password-field">
                        <input
                            id="owner-password"
                            autoFocus
                            type={shown ? "text" : "password"}
                            autoComplete="current-password"
                            required
                            value={password}
                            onChange={(event) =>
                                setPassword(event.target.value)
                            }
                            maxLength={512}
                        />
                        <button
                            type="button"
                            className="text-button"
                            aria-pressed={shown}
                            onClick={() => setShown(!shown)}
                        >
                            {shown ? "Hide" : "Show"}
                        </button>
                    </div>
                    <ErrorNotice message={error} />
                    <button
                        className="primary-button full-width"
                        disabled={busy}
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
                <p className="login-note">
                    Use the owner password configured for this installation.
                </p>
            </section>
            <p className="login-footer">
                Private by default. Share only what you choose.
            </p>
        </main>
    );
}
