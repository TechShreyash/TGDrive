import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./app/App";
import EmbedView from "./features/shares/EmbedView";
import "@mantine/core/styles.css";
import "./app/style.css";
import "./shared/design-system.css";
import "./shared/refresh.css";
import "./shared/pages/monitor.css";
import "./shared/pages/settings.css";
import "./shared/pages/connections.css";
import "./shared/pages/storage.css";
import "./shared/pages/dialogs.css";
import "./shared/pages/previews.css";
import "./shared/pages/motion.css";
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
