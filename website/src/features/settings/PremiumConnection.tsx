import { useEffect, useRef, useState, type FormEvent } from "react";
import { Check, ShieldCheck } from "lucide-react";
import { api, ApiError, errorMessage } from "../../shared/api";
import { ErrorNotice, Spinner } from "../../shared/components";
import type { ManagementSettings, PremiumChallenge } from "../../shared/types";

export default function PremiumConnection({
    revision,
    blockedReason,
    onComplete,
    onCancel,
    onRefresh,
}: {
    revision: number;
    blockedReason: string;
    onComplete: (value: ManagementSettings) => void;
    onCancel: () => void;
    onRefresh: () => void;
}) {
    const [stage, setStage] = useState<"phone" | "code" | "password">("phone");
    const [label, setLabel] = useState("My Premium account");
    const [phone, setPhone] = useState("");
    const [code, setCode] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    const [waitUntil, setWaitUntil] = useState(0);
    const [remaining, setRemaining] = useState(0);
    const challenge = useRef<PremiumChallenge | null>(null);
    const mounted = useRef(true);
    const focusField = useRef<HTMLInputElement>(null);
    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            const active = challenge.current;
            challenge.current = null;
            if (active)
                void api
                    .cancelPremiumLogin(active.challenge_id)
                    .catch(() => {});
        };
    }, []);
    useEffect(() => {
        focusField.current?.focus();
    }, [stage]);
    useEffect(() => {
        if (!waitUntil) return;
        const tick = () => {
            const seconds = Math.max(
                0,
                Math.ceil((waitUntil - Date.now()) / 1000),
            );
            setRemaining(seconds);
            if (!seconds) setWaitUntil(0);
        };
        tick();
        const timer = setInterval(tick, 1000);
        return () => clearInterval(timer);
    }, [waitUntil]);
    const submit = async (event: FormEvent) => {
        event.preventDefault();
        if (busy || remaining || blockedReason) return;
        setError("");
        const normalizedPhone = phone.replace(/[ ()-]/g, "").trim();
        if (
            stage === "phone" &&
            !/^\+[1-9][0-9]{6,14}$/.test(normalizedPhone)
        ) {
            setError(
                "Enter your phone number with its country code, such as +441234567890.",
            );
            return;
        }
        setBusy(true);
        try {
            if (stage === "phone") {
                const value = await api.startPremiumLogin(
                    revision,
                    normalizedPhone,
                    label.trim(),
                );
                if (!mounted.current) {
                    void api
                        .cancelPremiumLogin(value.challenge_id)
                        .catch(() => {});
                    return;
                }
                challenge.current = value;
                setStage("code");
            } else {
                const active = challenge.current;
                if (!active)
                    throw new Error(
                        "This connection request expired. Start again.",
                    );
                const result = await api.verifyPremiumLogin(
                    active.challenge_id,
                    stage === "code"
                        ? { code: code.replace(/\s/g, "") }
                        : { password },
                );
                if (!mounted.current) return;
                if (result.stage === "complete") {
                    challenge.current = null;
                    onComplete(result.settings);
                } else {
                    challenge.current = result;
                    setStage(result.stage);
                }
            }
        } catch (cause) {
            if (!mounted.current) return;
            if (
                cause instanceof ApiError &&
                cause.status === 429 &&
                cause.retryAfter > 0
            ) {
                const seconds = Math.ceil(cause.retryAfter);
                setRemaining(seconds);
                setWaitUntil(Date.now() + seconds * 1000);
            }
            if (
                cause instanceof ApiError &&
                (cause.status === 410 ||
                    cause.status === 502 ||
                    cause.code === "TELEGRAM_PREMIUM_REQUIRED" ||
                    cause.code.startsWith("SETTINGS_") ||
                    (cause.status === 429 &&
                        cause.code === "TELEGRAM_LOGIN_FAILED"))
            ) {
                const active = challenge.current;
                challenge.current = null;
                if (active)
                    void api
                        .cancelPremiumLogin(active.challenge_id)
                        .catch(() => {});
                setStage("phone");
                if (cause.code.startsWith("SETTINGS_")) onRefresh();
            }
            setError(errorMessage(cause));
            focusField.current?.focus();
        } finally {
            setCode("");
            setPassword("");
            if (mounted.current) setBusy(false);
        }
    };
    return (
        <form
            className="management-form premium-connection"
            onSubmit={(event) => void submit(event)}
            aria-labelledby="premium-connect-heading"
        >
            <h3 id="premium-connect-heading">
                <ShieldCheck size={18} /> Connect a Premium Account
            </h3>
            <ol className="premium-steps" aria-label="Connection progress">
                <li
                    aria-current={stage === "phone" ? "step" : undefined}
                    data-complete={stage !== "phone"}
                >
                    Account
                </li>
                <li
                    aria-current={stage === "code" ? "step" : undefined}
                    data-complete={stage === "password"}
                >
                    Login code
                </li>
                <li aria-current={stage === "password" ? "step" : undefined}>
                    Verification
                </li>
            </ol>
            <p>
                Your account must already have Telegram Premium and access to
                this drive’s channel. Login happens here; your code and two-step
                password are not saved.
            </p>
            <fieldset disabled={busy || Boolean(blockedReason)}>
                {stage === "phone" ? (
                    <>
                        <label htmlFor="premium-account-label">
                            Account Name
                        </label>
                        <input
                            id="premium-account-label"
                            required
                            maxLength={80}
                            value={label}
                            autoComplete="off"
                            onChange={(event) => setLabel(event.target.value)}
                        />
                        <label htmlFor="premium-phone">Phone Number</label>
                        <input
                            id="premium-phone"
                            ref={focusField}
                            type="tel"
                            autoComplete="tel"
                            required
                            maxLength={32}
                            placeholder="+441234567890"
                            value={phone}
                            aria-describedby="premium-phone-help"
                            onChange={(event) => setPhone(event.target.value)}
                        />
                        <p
                            id="premium-phone-help"
                            className="management-caption"
                        >
                            Include the + country code. Telegram will send a
                            login code for this account.
                        </p>
                    </>
                ) : stage === "code" ? (
                    <>
                        <label htmlFor="premium-code">
                            Telegram Login Code
                        </label>
                        <input
                            id="premium-code"
                            ref={focusField}
                            inputMode="numeric"
                            autoComplete="one-time-code"
                            required
                            maxLength={16}
                            value={code}
                            aria-describedby="premium-code-help"
                            onChange={(event) => setCode(event.target.value)}
                        />
                        <p
                            id="premium-code-help"
                            className="management-caption"
                        >
                            Enter the code from Telegram. This request expires
                            after five minutes.
                        </p>
                    </>
                ) : (
                    <>
                        <label htmlFor="premium-2fa">
                            Telegram Two-Step Password
                        </label>
                        <input
                            id="premium-2fa"
                            ref={focusField}
                            type="password"
                            autoComplete="off"
                            required
                            maxLength={512}
                            value={password}
                            onChange={(event) =>
                                setPassword(event.target.value)
                            }
                        />
                        <p className="management-caption">
                            This account has two-step verification. Enter its
                            Telegram password.
                        </p>
                    </>
                )}
            </fieldset>
            {blockedReason && <p className="settings-help">{blockedReason}</p>}
            <ErrorNotice message={error} />
            {busy && (
                <p role="status">
                    <Spinner />
                    {stage === "phone"
                        ? "Requesting a Telegram Login Code…"
                        : "Verifying your Telegram account…"}
                </p>
            )}
            <div className="management-actions">
                <button
                    className="primary-button"
                    disabled={busy || remaining > 0 || Boolean(blockedReason)}
                >
                    {busy ? <Spinner /> : <Check size={17} />}
                    {remaining
                        ? `Try Again in ${remaining}s`
                        : stage === "phone"
                          ? "Send Login Code"
                          : stage === "code"
                            ? "Verify Code"
                            : "Connect Account"}
                </button>
                <button
                    type="button"
                    className="secondary-button"
                    disabled={busy}
                    onClick={onCancel}
                >
                    Cancel Connection
                </button>
            </div>
            <p className="management-caption">
                After verification, the account is saved privately for this
                installation. Restart TGDrive to use it. Treat the saved session
                as an account credential.
            </p>
        </form>
    );
}
