const positionPrefix = "tgdrive.video.position.v1:";

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
