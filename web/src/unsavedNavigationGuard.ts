const PROJECT_SWITCH_SELECTOR = ".project-list button.project:not(.active)";
const NEW_PROJECT_BUTTON_SELECTOR = ".new-project button";
const NEW_PROJECT_INPUT_SELECTOR = "#new-project-name";

export const UNSAVED_NAVIGATION_MESSAGE = "目前有尚未儲存的工作區變更。離開目前專案會放棄這些變更，仍要繼續嗎？";

export function workspaceHasUnsavedChanges(): boolean {
  const event = new Event("beforeunload", { cancelable: true });
  return !window.dispatchEvent(event);
}

export function installUnsavedNavigationGuard(confirmNavigation: (message: string) => boolean = window.confirm): () => void {
  const allowNavigation = () => !workspaceHasUnsavedChanges() || confirmNavigation(UNSAVED_NAVIGATION_MESSAGE);

  const handleClick = (event: MouseEvent) => {
    const target = event.target instanceof Element ? event.target : null;
    const guardedTarget = target?.closest(`${PROJECT_SWITCH_SELECTOR}, ${NEW_PROJECT_BUTTON_SELECTOR}`);
    if (!guardedTarget || allowNavigation()) return;

    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  };

  const handleKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Enter") return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target?.matches(NEW_PROJECT_INPUT_SELECTOR) || allowNavigation()) return;

    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  };

  document.addEventListener("click", handleClick, true);
  document.addEventListener("keydown", handleKeyDown, true);

  return () => {
    document.removeEventListener("click", handleClick, true);
    document.removeEventListener("keydown", handleKeyDown, true);
  };
}
