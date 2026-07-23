import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  hasUnsavedTextDrafts,
  installUnsavedNavigationGuard,
  UNSAVED_NAVIGATION_MESSAGE,
  workspaceHasUnsavedChanges,
} from "../src/unsavedNavigationGuard";

let cleanupGuard: (() => void) | undefined;
let cleanupDirty: (() => void) | undefined;

function markWorkspaceDirty() {
  const handler = (event: Event) => event.preventDefault();
  window.addEventListener("beforeunload", handler);
  cleanupDirty = () => window.removeEventListener("beforeunload", handler);
}

beforeEach(() => {
  document.body.innerHTML = "";
});

afterEach(() => {
  cleanupGuard?.();
  cleanupDirty?.();
  cleanupGuard = undefined;
  cleanupDirty = undefined;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("unsaved navigation guard", () => {
  it("detects existing workspace beforeunload handlers", () => {
    expect(workspaceHasUnsavedChanges()).toBe(false);
    markWorkspaceDirty();
    expect(workspaceHasUnsavedChanges()).toBe(true);
  });

  it("detects review notes and modified clip summaries", () => {
    document.body.innerHTML = `
      <section id="workspace-review"><textarea placeholder="記錄核准理由、退回項目或重建故事需求"></textarea></section>
      <textarea placeholder="內容感知描述">伺服器內容</textarea>
    `;
    const review = document.querySelector("#workspace-review textarea") as HTMLTextAreaElement;
    const summary = document.querySelector('textarea[placeholder="內容感知描述"]') as HTMLTextAreaElement;

    expect(hasUnsavedTextDrafts()).toBe(false);
    review.value = "需要補一個轉場";
    expect(hasUnsavedTextDrafts()).toBe(true);
    review.value = "";
    summary.value = "本地草稿";
    expect(hasUnsavedTextDrafts()).toBe(true);
  });

  it("warns on browser unload for unsaved text drafts", () => {
    document.body.innerHTML = '<section id="workspace-review"><textarea placeholder="記錄核准理由、退回項目或重建故事需求">待確認</textarea></section>';
    cleanupGuard = installUnsavedNavigationGuard(vi.fn(() => true));
    const event = new Event("beforeunload", { cancelable: true });

    expect(window.dispatchEvent(event)).toBe(false);
    expect(event.defaultPrevented).toBe(true);
  });

  it("blocks switching projects when the user cancels", () => {
    document.body.innerHTML = '<div class="project-list"><button class="project">其他專案</button></div>';
    markWorkspaceDirty();
    const confirmNavigation = vi.fn(() => false);
    cleanupGuard = installUnsavedNavigationGuard(confirmNavigation);
    const projectButton = document.querySelector("button") as HTMLButtonElement;
    const reactHandler = vi.fn();
    projectButton.addEventListener("click", reactHandler);

    projectButton.click();

    expect(confirmNavigation).toHaveBeenCalledWith(UNSAVED_NAVIGATION_MESSAGE);
    expect(reactHandler).not.toHaveBeenCalled();
  });

  it("blocks project switching for an edited clip summary", () => {
    document.body.innerHTML = `
      <textarea placeholder="內容感知描述">伺服器內容</textarea>
      <div class="project-list"><button class="project">其他專案</button></div>
    `;
    const summary = document.querySelector("textarea") as HTMLTextAreaElement;
    summary.value = "尚未儲存";
    const confirmNavigation = vi.fn(() => false);
    cleanupGuard = installUnsavedNavigationGuard(confirmNavigation);

    (document.querySelector("button") as HTMLButtonElement).click();

    expect(confirmNavigation).toHaveBeenCalledOnce();
  });

  it("allows switching projects after explicit confirmation", () => {
    document.body.innerHTML = '<div class="project-list"><button class="project">其他專案</button></div>';
    markWorkspaceDirty();
    const confirmNavigation = vi.fn(() => true);
    cleanupGuard = installUnsavedNavigationGuard(confirmNavigation);
    const projectButton = document.querySelector("button") as HTMLButtonElement;
    const reactHandler = vi.fn();
    projectButton.addEventListener("click", reactHandler);

    projectButton.click();

    expect(confirmNavigation).toHaveBeenCalledOnce();
    expect(reactHandler).toHaveBeenCalledOnce();
  });

  it("does not prompt when there are no drafts or the active project is clicked", () => {
    document.body.innerHTML = '<div class="project-list"><button class="project active">目前專案</button><button class="project">其他專案</button></div>';
    const confirmNavigation = vi.fn(() => false);
    cleanupGuard = installUnsavedNavigationGuard(confirmNavigation);
    const buttons = document.querySelectorAll("button");

    (buttons[1] as HTMLButtonElement).click();
    markWorkspaceDirty();
    (buttons[0] as HTMLButtonElement).click();

    expect(confirmNavigation).not.toHaveBeenCalled();
  });

  it("protects Enter-based project creation", () => {
    document.body.innerHTML = '<div class="new-project"><input id="new-project-name" /><button>新增專案</button></div>';
    markWorkspaceDirty();
    const confirmNavigation = vi.fn(() => false);
    cleanupGuard = installUnsavedNavigationGuard(confirmNavigation);
    const input = document.querySelector("input") as HTMLInputElement;
    const reactHandler = vi.fn();
    input.addEventListener("keydown", reactHandler);

    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));

    expect(confirmNavigation).toHaveBeenCalledOnce();
    expect(reactHandler).not.toHaveBeenCalled();
  });
});
