import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import EmbedView from "./features/shares/EmbedView";
import "@mantine/core/styles.css";
import "./app/style.css";
import "./shared/design-system.css";
import DesignSystem from "./shared/DesignSystem";
import { initializeTheme } from "./shared/theme";
initializeTheme();
createRoot(document.getElementById("root")!).render(
    <StrictMode>
        <DesignSystem>
            {window.location.pathname === "/embed" ? <EmbedView /> : <App />}
        </DesignSystem>
    </StrictMode>,
);
