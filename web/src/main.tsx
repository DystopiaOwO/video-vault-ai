import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { UnsavedDraftIndicator } from "./components/UnsavedDraftIndicator";
import { WorkspaceCommandPalette } from "./components/WorkspaceCommandPalette";
import { BgmLibraryPage } from "./pages/BgmLibraryPage";
import "./styles.css";
import "./workspace-navigation-enhancements.css";
import { registerUnsavedNavigationGuard } from "./unsavedNavigationGuard";
import { registerWorkspaceNavigationEnhancements } from "./workspaceNavigationEnhancements";

export { App } from "./App";

registerUnsavedNavigationGuard();
registerWorkspaceNavigationEnhancements();

const rootElement = document.getElementById("root");
const bgmPage = window.location.pathname === "/bgm";
const application = bgmPage
  ? <BgmLibraryPage />
  : <><App /><UnsavedDraftIndicator /><WorkspaceCommandPalette /></>;
if (rootElement) createRoot(rootElement).render(<StrictMode>{application}</StrictMode>);
