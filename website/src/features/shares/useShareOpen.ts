import { useEffect, useRef } from "react";
import { ApiError, request } from "../../shared/api";

export function useShareOpen(visible: boolean, auth: string, path: string) {
    const recorded = useRef(new Set<string>());
    useEffect(() => {
        const node = path
            .replace(/^\/share_/, "/")
            .split("/")
            .filter(Boolean)
            .at(-1);
        if (!visible || !auth || !node || recorded.current.has(auth)) return;
        const report = () => {
            if (
                document.visibilityState !== "visible" ||
                recorded.current.has(auth)
            )
                return;
            recorded.current.add(auth);
            const bytes = crypto.getRandomValues(new Uint8Array(16));
            const event_id = Array.from(bytes, (value) =>
                value.toString(16).padStart(2, "0"),
            ).join("");
            const body = {
                auth,
                path: node === "root" ? "/" : "/" + node,
                event_id,
            };
            const send = () =>
                request("/api/v1/shared/opens", body, {
                    keepalive: true,
                    timeoutMs: 4000,
                    notifySessionExpiry: false,
                });
            void send().catch((error: unknown) => {
                if (
                    error instanceof ApiError &&
                    (!error.status ||
                        error.status === 408 ||
                        error.status >= 500)
                )
                    return send().catch(() => {});
            });
        };
        report();
        document.addEventListener("visibilitychange", report);
        return () => document.removeEventListener("visibilitychange", report);
    }, [visible, auth, path]);
}
