import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../shared/api";
import type { ServerStatus } from "../shared/types";
import { startStatusPolling } from "./statusPolling";

const work = vi.hoisted(() => ({
    active: false,
    listeners: new Set<() => void>(),
}));
vi.mock("../features/transfers/transfers", () => ({
    transfers: {
        hasActiveWork: () => work.active,
        subscribe: (listener: () => void) => {
            work.listeners.add(listener);
            return () => work.listeners.delete(listener);
        },
    },
}));

let visibility: EventTarget & { hidden: boolean };
const stops: (() => void)[] = [];
const start = () => {
    const receive = vi.fn();
    stops.push(startStatusPolling(receive));
    return receive;
};
const setActive = (active: boolean) => {
    work.active = active;
    for (const listener of work.listeners) listener();
};

beforeEach(() => {
    vi.useFakeTimers();
    work.active = false;
    work.listeners.clear();
    visibility = Object.assign(new EventTarget(), { hidden: false });
    vi.stubGlobal("document", visibility);
    vi.stubGlobal("window", new EventTarget());
    vi.spyOn(api, "status").mockResolvedValue({ telegram_ready: true });
});
afterEach(() => {
    for (const stop of stops.splice(0)) stop();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
});

it("checks healthy idle status every thirty seconds", async () => {
    start();
    await vi.advanceTimersByTimeAsync(29999);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(2);
});

it("slows hidden idle checks and coalesces focus and visibility refreshes", async () => {
    start();
    await vi.advanceTimersByTimeAsync(10000);
    visibility.hidden = true;
    visibility.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(59999);
    expect(api.status).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2000);
    visibility.hidden = false;
    visibility.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(3);
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.status).toHaveBeenCalledTimes(3);
});

it("wakes for new work and retains five-second checks while hidden and active", async () => {
    start();
    await vi.advanceTimersByTimeAsync(10000);
    setActive(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(2);
    visibility.hidden = true;
    visibility.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(4999);
    expect(api.status).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(3);
    setActive(false);
    await vi.advanceTimersByTimeAsync(59999);
    expect(api.status).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(4);
});

it.each([
    { admission_paused: true },
    { restart_required: true },
    { telegram_ready: false },
    { telegram_issues: [{ code: "DISCONNECTED", detail: "Reconnect" }] },
])("keeps checking readiness problems promptly: %j", async (status) => {
    vi.mocked(api.status).mockResolvedValue(status);
    visibility.hidden = true;
    start();
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.status).toHaveBeenCalledTimes(2);
});

it("does not overlap slow reads or replace the last status after a failed read", async () => {
    let finish!: (status: ServerStatus) => void;
    vi.mocked(api.status).mockImplementationOnce(
        () => new Promise((resolve) => (finish = resolve)),
    );
    const receive = start();
    await vi.advanceTimersByTimeAsync(10000);
    setActive(true);
    window.dispatchEvent(new Event("focus"));
    visibility.dispatchEvent(new Event("visibilitychange"));
    expect(api.status).toHaveBeenCalledTimes(1);
    finish({ telegram_ready: true });
    vi.mocked(api.status).mockRejectedValue(new Error("Offline"));
    await vi.advanceTimersByTimeAsync(0);
    expect(api.status).toHaveBeenCalledTimes(2);
    expect(receive).toHaveBeenCalledTimes(1);
    expect(receive).toHaveBeenCalledWith({ telegram_ready: true });
    await vi.advanceTimersByTimeAsync(4999);
    expect(api.status).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.status).toHaveBeenCalledTimes(3);
});

it("aborts and removes all wakeups when polling stops", async () => {
    let finish!: (status: ServerStatus) => void;
    vi.mocked(api.status).mockImplementationOnce(
        () => new Promise((resolve) => (finish = resolve)),
    );
    const receive = start();
    const signal = vi.mocked(api.status).mock.calls[0][0]!;
    stops.pop()!();
    expect(signal.aborted).toBe(true);
    expect(work.listeners.size).toBe(0);
    finish({ telegram_ready: true });
    visibility.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await vi.advanceTimersByTimeAsync(120000);
    expect(api.status).toHaveBeenCalledTimes(1);
    expect(receive).not.toHaveBeenCalled();
});
