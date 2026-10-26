import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "./api";

export type RefreshNotice = (message: string, error?: boolean) => void;

/** Explicit refresh feedback; automatic polling stays with the page's loader. */
export function useManualRefresh(onNotice?: RefreshNotice) {
    const [refreshing, setRefreshing] = useState(false);
    const mounted = useRef(true);
    const active = useRef<AbortController | null>(null);
    useEffect(() => {
        mounted.current = true;
        return () => {
            mounted.current = false;
            active.current?.abort();
            active.current = null;
        };
    }, []);
    const run = useCallback(
        async (
            task: (signal: AbortSignal) => Promise<void | boolean>,
            successMessage: string,
        ) => {
            // A ref closes the gap before React commits the disabled button state.
            if (!mounted.current || active.current) return;
            const controller = new AbortController();
            active.current = controller;
            setRefreshing(true);
            try {
                const result = await task(controller.signal);
                if (
                    mounted.current &&
                    !controller.signal.aborted &&
                    result !== false
                )
                    onNotice?.(successMessage);
            } catch (cause) {
                if (mounted.current && !controller.signal.aborted)
                    onNotice?.(errorMessage(cause), true);
            } finally {
                if (active.current === controller) {
                    active.current = null;
                    if (mounted.current) setRefreshing(false);
                }
            }
        },
        [onNotice],
    );
    return { refreshing, run };
}
