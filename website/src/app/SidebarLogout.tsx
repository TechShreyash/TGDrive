import { useEffect, useRef, useState } from "react";
import { LogOut } from "lucide-react";
import { api, errorMessage } from "../shared/api";
import { Spinner } from "../shared/components";

export default function SidebarLogout({
    onLoggedOut,
}: {
    onLoggedOut: () => void;
}) {
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const button = useRef<HTMLButtonElement>(null);
    useEffect(() => {
        if (error && !busy) button.current?.focus();
    }, [error, busy]);
    const logout = async () => {
        if (busy) return;
        setBusy(true);
        setError("");
        try {
            await api.logout();
            onLoggedOut();
        } catch (cause) {
            setError(errorMessage(cause));
            setBusy(false);
        }
    };
    return (
        <>
            <button
                ref={button}
                type="button"
                className="sidebar-logout"
                disabled={busy}
                aria-busy={busy}
                title="Log out of this browser. Server transfers continue."
                onClick={() => void logout()}
            >
                {busy ? <Spinner /> : <LogOut size={18} />}
                {busy ? "Logging out…" : "Log Out"}
            </button>
            {error && (
                <p className="sidebar-logout-error" role="alert">
                    {error}
                </p>
            )}
        </>
    );
}
