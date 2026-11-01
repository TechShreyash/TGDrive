import { useState } from "react";

/** Parses a typed page number; null when it is not a whole number. */
export function parsePage(text: string, total: number): number | null {
    if (!/^\d+$/.test(text.trim()) || total < 1) return null;
    return Math.min(total, Math.max(1, Number(text)));
}

/** A numeric page field: type a number, then Enter or leave the field to jump. */
export default function PageJump({
    id,
    current,
    total,
    onJump,
}: {
    id: string;
    current: number;
    total: number;
    onJump: (page: number) => void;
}) {
    const [draft, setDraft] = useState<string | null>(null);
    function commit() {
        if (draft === null) return;
        const page = parsePage(draft, total);
        setDraft(null);
        if (page !== null && page !== current) onJump(page);
    }
    return (
        <input
            id={id}
            className="page-jump"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            enterKeyHint="go"
            disabled={total < 1}
            placeholder="–"
            value={draft ?? (total < 1 ? "" : String(current))}
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) =>
                setDraft(event.target.value.replace(/\D/g, "").slice(0, 6))
            }
            onBlur={commit}
            onKeyDown={(event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    commit();
                    event.currentTarget.select();
                } else if (event.key === "Escape" && draft !== null) {
                    event.preventDefault();
                    event.stopPropagation();
                    setDraft(null);
                }
            }}
        />
    );
}
