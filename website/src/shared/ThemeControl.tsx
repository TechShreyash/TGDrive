import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Check, Moon, Sun, SunMoon } from "lucide-react";
import {
    applyThemePreference,
    readThemePreference,
    saveThemePreference,
    THEME_STORAGE_KEY,
    THEME_CHANGE_EVENT,
    type ThemePreference,
    resolveTheme,
} from "./theme";

const choices = [
    { value: "light" as const, label: "Light", icon: Sun },
    { value: "dark" as const, label: "Dark", icon: Moon },
    { value: "system" as const, label: "System", icon: SunMoon },
];
export function useAppearance() {
    const [preference, setPreference] =
        useState<ThemePreference>(readThemePreference);
    const [theme, setTheme] = useState<"light" | "dark">(() =>
        resolveTheme(
            readThemePreference(),
            window.matchMedia("(prefers-color-scheme: dark)").matches,
        ),
    );
    useEffect(() => {
        const media = window.matchMedia("(prefers-color-scheme: dark)");
        const update = () => {
            applyThemePreference(preference);
            setTheme(resolveTheme(preference, media.matches));
        };
        const changed = (event: StorageEvent) => {
            if (event.key === THEME_STORAGE_KEY || event.key === null)
                setPreference(readThemePreference());
        };
        const local = (event: Event) => {
            const value = (event as CustomEvent<ThemePreference>).detail;
            if (value === "light" || value === "dark" || value === "system")
                setPreference(value);
        };
        update();
        media.addEventListener("change", update);
        window.addEventListener("storage", changed);
        window.addEventListener(THEME_CHANGE_EVENT, local);
        return () => {
            media.removeEventListener("change", update);
            window.removeEventListener("storage", changed);
            window.removeEventListener(THEME_CHANGE_EVENT, local);
        };
    }, [preference]);
    const choose = (value: ThemePreference) => {
        saveThemePreference(value);
        setPreference(value);
    };
    return { preference, choose, theme };
}
export default function ThemeControl() {
    const { preference, choose } = useAppearance();
    const [open, setOpen] = useState(false);
    const root = useRef<HTMLDivElement>(null);
    const trigger = useRef<HTMLButtonElement>(null);
    const menuId = useId();
    const choice = choices.find((item) => item.value === preference)!;
    useEffect(() => {
        if (!open) return;
        root.current
            ?.querySelector<HTMLButtonElement>(
                '[role="menuitemradio"][aria-checked="true"]',
            )
            ?.focus();
        const outside = (event: PointerEvent) => {
            if (!root.current?.contains(event.target as Node)) setOpen(false);
        };
        document.addEventListener("pointerdown", outside);
        return () => document.removeEventListener("pointerdown", outside);
    }, [open]);
    const select = (value: ThemePreference) => {
        choose(value);
        setOpen(false);
        trigger.current?.focus();
    };
    const menuKey = (event: KeyboardEvent<HTMLDivElement>) => {
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            setOpen(false);
            trigger.current?.focus();
        } else if (event.key === "Tab") {
            setOpen(false);
            trigger.current?.focus();
        } else if (
            ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)
        ) {
            event.preventDefault();
            const items = [
                ...(root.current?.querySelectorAll<HTMLButtonElement>(
                    '[role="menuitemradio"]',
                ) || []),
            ];
            const current = items.indexOf(
                document.activeElement as HTMLButtonElement,
            );
            const next =
                event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? items.length - 1
                      : (current +
                            (event.key === "ArrowDown" ? 1 : -1) +
                            items.length) %
                        items.length;
            items[next]?.focus();
        }
    };
    return (
        <div className="theme-control" ref={root}>
            <button
                className="icon-button theme-trigger"
                type="button"
                ref={trigger}
                aria-label="Change Appearance"
                title={"Appearance: " + choice.label}
                aria-haspopup="menu"
                aria-expanded={open}
                aria-controls={open ? menuId : undefined}
                onClick={() => setOpen(!open)}
                onKeyDown={(event) => {
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                        event.preventDefault();
                        setOpen(true);
                    }
                }}
            >
                <SunMoon size={20} aria-hidden="true" />
            </button>
            {open && (
                <div
                    id={menuId}
                    className="theme-menu"
                    role="menu"
                    aria-label="Appearance"
                    onKeyDown={menuKey}
                >
                    {choices.map((item) => (
                        <button
                            key={item.value}
                            type="button"
                            role="menuitemradio"
                            aria-checked={preference === item.value}
                            tabIndex={preference === item.value ? 0 : -1}
                            onClick={() => select(item.value)}
                        >
                            <item.icon size={18} aria-hidden="true" />
                            <span>{item.label}</span>
                            {preference === item.value && (
                                <Check size={16} aria-hidden="true" />
                            )}
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
