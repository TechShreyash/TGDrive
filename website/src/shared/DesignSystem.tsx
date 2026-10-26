import { useAppearance } from "./ThemeControl";
import { type ReactNode } from "react";
import {
    MantineProvider,
    createTheme,
    Button,
    Fieldset,
    NumberInput,
    TextInput,
    NativeSelect,
    Switch,
    Paper,
} from "@mantine/core";

export const driveTheme = createTheme({
    scale: 16 / 14,
    primaryColor: "blue",
    primaryShade: { light: 6, dark: 3 },
    fontFamily: "InterVariable, Inter, ui-sans-serif, system-ui, sans-serif",
    fontFamilyMonospace: "ui-monospace, monospace",
    defaultRadius: "md",
    fontSizes: { xs: "12px", sm: "13px", md: "14px", lg: "16px", xl: "20px" },
    headings: {
        fontFamily: "InterVariable, Inter, sans-serif",
        fontWeight: "600",
    },
    respectReducedMotion: true,
    cursorType: "pointer",
    components: {
        Button: Button.extend({
            defaultProps: { size: "sm", radius: "md" },
            styles: { label: { gap: 8 } },
        }),
        Fieldset: Fieldset.extend({
            defaultProps: { radius: "lg", variant: "filled" },
        }),
        NumberInput: NumberInput.extend({
            defaultProps: { size: "md", radius: "md", clampBehavior: "none" },
        }),
        TextInput: TextInput.extend({
            defaultProps: { size: "md", radius: "md" },
        }),
        NativeSelect: NativeSelect.extend({
            defaultProps: { size: "sm", radius: "md" },
        }),
        Switch: Switch.extend({ defaultProps: { size: "md", color: "teal" } }),
        Paper: Paper.extend({
            defaultProps: { radius: "lg", withBorder: true, shadow: undefined },
        }),
    },
});

export default function DesignSystem({ children }: { children: ReactNode }) {
    const { theme } = useAppearance();
    return (
        <MantineProvider
            theme={driveTheme}
            forceColorScheme={theme}
            withCssVariables={false}
            withGlobalClasses={false}
        >
            {children}
        </MantineProvider>
    );
}
