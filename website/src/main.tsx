import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@mantine/core/styles.css";
import "./style.css";
import "./design-system.css";
import DesignSystem from "./DesignSystem";
import { initializeTheme } from "./theme";
initializeTheme();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <DesignSystem>
      <App />
    </DesignSystem>
  </StrictMode>,
);
