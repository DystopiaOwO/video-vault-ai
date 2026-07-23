import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
import { installUnsavedNavigationGuard } from "./unsavedNavigationGuard";

export { App } from "./App";

installUnsavedNavigationGuard();

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<StrictMode><App /></StrictMode>);
