import { isCommittedEnter } from "./keyboard";

const PROJECT_SWITCH_SELECTOR = ".project-list button.project:not(.active)";
const NEW_PROJECT_BUTTON_SELECTOR = ".new-project button";
const NEW_PROJECT_INPUT_SELECTOR = "#new-project-name";
const REVIEW_NOTES_SELECTOR = '#workspace-review textarea[placeholder*="核准理由"]';
const EXPLICIT_TEXT_DRAFT_SELECTOR = '[data-unsaved-text-draft="true"]';
const LEGACY_CLIP_SUMMARY_SELECTOR = 'textarea[placeholder="內容感知描述"]';

type GuardWindow = Window & {
  __videoVaultUnsavedNavigationGuardCleanup?: () => void;
};

export const UNSAVED_NAVIGATION_MESSAGE = "目前有尚未儲存的工作區變更。離開目前專案會放棄這些變更，仍要繼續嗎？";

export function hasUnsavedTextDrafts(root: ParentNode = document): boolean {
  if (root.querySelector(EXPLICIT_TEXT_DRAFT_SELECTOR)) return true;

  const reviewNotes = root.querySelector<HTMLTextAreaElement>(REVIEW_NOTES_SELECTOR);
  if (reviewNotes?.value.trim()) return true;

  return Array.from(root.querySelectorAll<HTMLTextAreaElement>(LEGACY_CLIP_SUMMARY_SELECTOR))
    .some((textarea) => textarea.value !== textarea.defaultValue);
}

export function workspaceHasUnsavedChanges(): boolean {
  if (hasUnsavedTextDrafts()) return true;
  const event = new Event("beforeunload", { cancelable: true });
  return !window.dispatchEvent(event);
}

export function installUnsavedNavigationGuard(
  confirmNavigation: (message: string) => boolean = (message) => window.confirm(message),
): () => void {
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
    if (!isCommittedEnter(event)) return;
    const target = event.target instanceof Element ? event.target : null;
    if (!target?.matches(NEW_PROJECT_INPUT_SELECTOR) || allowNavigation()) return;

    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  };

  const handleBeforeUnload = (event: BeforeUnloadEvent) => {
    if (!hasUnsavedTextDrafts()) return;
    event.preventDefault();
    event.returnValue = "";
  };

  document.addEventListener("click", handleClick, true);
  document.addEventListener("keydown", handleKeyDown, true);
  window.addEventListener("beforeunload", handleBeforeUnload);

  return () => {
    document.removeEventListener("click", handleClick, true);
    document.removeEventListener("keydown", handleKeyDown, true);
    window.removeEventListener("beforeunload", handleBeforeUnload);
  };
}

export function registerUnsavedNavigationGuard() {
  const guardWindow = window as GuardWindow;
  guardWindow.__videoVaultUnsavedNavigationGuardCleanup?.();
  guardWindow.__videoVaultUnsavedNavigationGuardCleanup = installUnsavedNavigationGuard();
}
