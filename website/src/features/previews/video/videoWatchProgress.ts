const positionPrefix = "tgdrive.video.position.v1:";

export function formatVideoTime(seconds: number): string {
    const whole = Math.floor(Math.max(0, seconds));
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const remaining = String(whole % 60).padStart(2, "0");
    return hours > 0
        ? `${hours}:${String(minutes).padStart(2, "0")}:${remaining}`
        : `${minutes}:${remaining}`;
}

export function readVideoPosition(fileId: string): number | null {
    try {
        const time = Number(localStorage.getItem(positionPrefix + fileId));
        return Number.isFinite(time) && time > 0 ? time : null;
    } catch {
        return null;
    }
}

export function saveVideoPosition(fileId: string, time: number): void {
    if (!Number.isFinite(time) || time < 0) return;
    try {
        if (time > 0) localStorage.setItem(positionPrefix + fileId, String(time));
        else localStorage.removeItem(positionPrefix + fileId);
    } catch {
        // Playback still works when browser storage is unavailable or full.
    }
}
