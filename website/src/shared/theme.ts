export type ThemePreference = "light" | "dark" | "system";
export const THEME_STORAGE_KEY = "tgdrive.appearance";
export const THEME_CHANGE_EVENT = "tgdrive:appearance-changed";

export function resolveTheme(preference: ThemePreference, darkDevice: boolean) {
    return preference === "system"
        ? darkDevice
            ? "dark"
            : "light"
        : preference;
}
export function readThemePreference(
    storage?: Pick<Storage, "getItem">,
): ThemePreference {
    try {
        const value = (storage || window.localStorage).getItem(
            THEME_STORAGE_KEY,
        );
        return value === "light" || value === "dark" ? value : "system";
    } catch {
        return "system";
    }
}
export function saveThemePreference(preference: ThemePreference) {
    try {
        window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    } catch {
        /* The current tab still applies the choice when browser storage is blocked. */
    }
    window.dispatchEvent(
        new CustomEvent(THEME_CHANGE_EVENT, { detail: preference }),
    );
}
export function applyThemePreference(preference: ThemePreference) {
    const theme = resolveTheme(
        preference,
        window.matchMedia("(prefers-color-scheme: dark)").matches,
    );
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = theme;
    document
        .querySelector('meta[name="theme-color"]')
        ?.setAttribute("content", theme === "dark" ? "#1c1d1f" : "#f5f7f9");
}
export function initializeTheme() {
    applyThemePreference(readThemePreference());
}
