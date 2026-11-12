export function measuredNumber(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) && value >= 0
        ? value
        : null;
}
export function megabits(value: unknown): string {
    const bytes = measuredNumber(value);
    if (bytes === null) return "Unavailable";
    const rate = (bytes * 8) / 1e6;
    if (rate > 0 && rate < 0.01) return "<0.01 Mbps";
    return (
        rate.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " Mbps"
    );
}

export function customHistoryBounds(
    startText: string,
    endText: string,
    now = Date.now() / 1000,
) {
    const parse = (text: string) => {
        const value = Date.parse(text + "Z") / 1000;
        if (
            !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(text) ||
            !Number.isFinite(value) ||
            new Date(value * 1000).toISOString().slice(0, 16) !== text
        )
            throw new Error("Choose valid start and end dates in UTC.");
        return value;
    };
    const start = parse(startText),
        end = parse(endText);
    if (end <= start) throw new Error("End must be after start.");
    if (end - start > 31 * 86400)
        throw new Error("Choose a range of at most 31 days.");
    if (end > now + 60) throw new Error("End cannot be in the future.");
    if (start < now - 31 * 86400)
        throw new Error(
            "Recorded history is retained for 31 days. Choose a more recent start.",
        );
    return { start, end };
}
