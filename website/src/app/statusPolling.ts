import { api } from "../shared/api";
import type { ServerStatus } from "../shared/types";
import { transfers } from "../features/transfers/transfers";

export function startStatusPolling(receive: (status: ServerStatus) => void) {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let pending = false;
    let refreshRequested = false;
    let active = transfers.hasActiveWork();
    let attention = false;
    let lastStarted = -Infinity;
    const interval = () =>
        active || attention ? 5000 : document.hidden ? 60000 : 30000;
    const schedule = (delay = interval()) => {
        clearTimeout(timer);
        if (!controller.signal.aborted && !pending)
            timer = setTimeout(() => void refresh(), delay);
    };
    const refreshSoon = () => {
        if (!pending)
            schedule(Date.now() - lastStarted < 1000 ? interval() : 0);
    };
    const refresh = async () => {
        if (controller.signal.aborted || pending) return;
        clearTimeout(timer);
        pending = true;
        lastStarted = Date.now();
        try {
            const status = await api.status(controller.signal);
            if (!controller.signal.aborted) {
                attention = Boolean(
                    status.admission_paused ||
                        status.restart_required ||
                        status.telegram_ready === false ||
                        status.telegram_issues?.length,
                );
                receive(status);
            }
        } catch {
            // Keep the last known admission state until the status service recovers.
        } finally {
            pending = false;
            if (refreshRequested) {
                refreshRequested = false;
                refreshSoon();
            } else schedule();
        }
    };
    const visible = () => {
        if (document.hidden) schedule();
        else refreshSoon();
    };
    const unsubscribe = transfers.subscribe(() => {
        const next = transfers.hasActiveWork();
        if (next === active) return;
        active = next;
        if (active) {
            if (pending) refreshRequested = true;
            else refreshSoon();
        } else schedule();
    });
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("focus", visible);
    void refresh();
    return () => {
        controller.abort();
        clearTimeout(timer);
        unsubscribe();
        document.removeEventListener("visibilitychange", visible);
        window.removeEventListener("focus", visible);
    };
}
