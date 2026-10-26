import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import "@mantine/core/styles.css";
import "./app/style.css";
import "./shared/design-system.css";
import DesignSystem from "./shared/DesignSystem";
import { initializeTheme } from "./shared/theme";
initializeTheme();
createRoot(document.getElementById("root")!).render(
    <StrictMode>
        <DesignSystem>
            <App />
        </DesignSystem>
    </StrictMode>,
);
