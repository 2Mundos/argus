import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { InstallPrompt } from "./InstallPrompt";
import { registerServiceWorker } from "./pwaInstall";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing");
createRoot(root).render(
  <StrictMode>
    <App />
    <InstallPrompt />
  </StrictMode>,
);

registerServiceWorker();
