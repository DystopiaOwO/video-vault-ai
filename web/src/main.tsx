import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { BgmLibraryPage } from "./pages/BgmLibraryPage";
import "./styles.css";
import "./workspace-navigation-enhancements.css";
import { registerUnsavedNavigationGuard } from "./unsavedNavigationGuard";
import { registerWorkspaceNavigationEnhancements } from "./workspaceNavigationEnhancements";

export { App } from "./App";

registerUnsavedNavigationGuard();
registerWorkspaceNavigationEnhancements();

const rootElement = document.getElementById("root");
const application = window.location.pathname === "/bgm" ? <BgmLibraryPage /> : <App />;
if (rootElement) createRoot(rootElement).render(<StrictMode>{application}</StrictMode>);
