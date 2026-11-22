import { useEffect, useState } from "react";
import {
    Menu,
    MenuContent,
    MenuItemIndicator,
    MenuRadioGroup,
    MenuRadioItem,
    MenuTrigger,
} from "@ui-sdk/react/menu";
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
    const choice = choices.find((item) => item.value === preference)!;
    return (
        <div className="theme-control">
            <Menu container={document.body} modal={false}>
                <MenuTrigger
                    className="icon-button theme-trigger"
                    aria-label="Change Appearance"
                    title={"Appearance: " + choice.label}
                >
                    <SunMoon size={20} aria-hidden="true" />
                </MenuTrigger>
                <MenuContent
                    className="theme-menu"
                    aria-label="Appearance"
                    align="end"
                    sideOffset={6}
                    collisionPadding={12}
                >
                    <MenuRadioGroup value={preference} onValueChange={choose}>
                        {choices.map((item) => (
                            <MenuRadioItem
                                key={item.value}
                                value={item.value}
                                label={item.label}
                                closeOnSelect
                                render={<button type="button" />}
                            >
                                <item.icon size={18} aria-hidden="true" />
                                <span>{item.label}</span>
                                <MenuItemIndicator>
                                    <Check size={16} aria-hidden="true" />
                                </MenuItemIndicator>
                            </MenuRadioItem>
                        ))}
                    </MenuRadioGroup>
                </MenuContent>
            </Menu>
        </div>
    );
}
