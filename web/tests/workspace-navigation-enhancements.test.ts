import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  focusProjectSearch,
  installWorkspaceNavigationEnhancements,
  navigateToWorkspace,
} from "../src/workspaceNavigationEnhancements";

let cleanupNavigation: (() => void) | undefined;
let originalIntersectionObserver: typeof IntersectionObserver | undefined;

function workspaceMarkup() {
  return `
    <input aria-label="搜尋專案" value="福岡" />
    <nav class="workspace-nav">
      <a href="#workspace-overview">總覽</a>
      <a href="#workspace-storyboard">分鏡</a>
      <a href="#workspace-review">審核與輸出</a>
      <a href="#workspace-media">素材與調色</a>
      <a href="#workspace-audio">音訊</a>
      <a href="#workspace-script">故事整理</a>
    </nav>
    <section id="workspace-overview" tabindex="-1"></section>
    <section id="workspace-storyboard" tabindex="-1"></section>
    <section id="workspace-review" tabindex="-1"></section>
    <section id="workspace-media" tabindex="-1"></section>
    <section id="workspace-audio" tabindex="-1"></section>
    <section id="workspace-script" tabindex="-1"></section>
  `;
}

beforeEach(() => {
  document.body.innerHTML = workspaceMarkup();
  history.replaceState(null, "", "/");
  originalIntersectionObserver = globalThis.IntersectionObserver;
  Reflect.deleteProperty(globalThis, "IntersectionObserver");
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn(() => ({ matches: false })),
  });
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanupNavigation?.();
  cleanupNavigation = undefined;
  if (originalIntersectionObserver) globalThis.IntersectionObserver = originalIntersectionObserver;
  else Reflect.deleteProperty(globalThis, "IntersectionObserver");
  document.body.innerHTML = "";
  history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("workspace navigation enhancements", () => {
  it("focuses and selects the project search", () => {
    const input = document.querySelector('input[aria-label="搜尋專案"]') as HTMLInputElement;

    expect(focusProjectSearch()).toBe(true);
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
  });

  it("navigates to a numbered workspace and updates the current location", () => {
    const section = document.getElementById("workspace-storyboard") as HTMLElement;
    const link = document.querySelector('a[href="#workspace-storyboard"]') as HTMLAnchorElement;

    expect(navigateToWorkspace(1)).toBe(true);
    expect(section.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(document.activeElement).toBe(section);
    expect(window.location.hash).toBe("#workspace-storyboard");
    expect(link.getAttribute("aria-current")).toBe("location");
  });

  it("decorates shortcuts and supports Ctrl+K and Alt+number", () => {
    cleanupNavigation = installWorkspaceNavigationEnhancements();
    const search = document.querySelector('input[aria-label="搜尋專案"]') as HTMLInputElement;
    const media = document.getElementById("workspace-media") as HTMLElement;
    const mediaLink = document.querySelector('a[href="#workspace-media"]') as HTMLAnchorElement;

    expect(search.getAttribute("aria-keyshortcuts")).toBe("Control+K Meta+K");
    expect(mediaLink.getAttribute("aria-keyshortcuts")).toBe("Alt+4");
    expect(document.querySelector('a[href="#workspace-overview"]')?.getAttribute("aria-current")).toBe("location");

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(search);

    search.blur();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "4", altKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(media);
    expect(mediaLink.getAttribute("aria-current")).toBe("location");
  });

  it("does not steal Alt shortcuts while editing a field", () => {
    cleanupNavigation = installWorkspaceNavigationEnhancements();
    const search = document.querySelector('input[aria-label="搜尋專案"]') as HTMLInputElement;
    const audio = document.getElementById("workspace-audio") as HTMLElement;
    search.focus();

    search.dispatchEvent(new KeyboardEvent("keydown", { key: "5", altKey: true, bubbles: true, cancelable: true }));

    expect(document.activeElement).toBe(search);
    expect(audio.scrollIntoView).not.toHaveBeenCalled();
  });

  it("tracks the most visible section with IntersectionObserver", () => {
    let observerCallback: IntersectionObserverCallback | undefined;
    class MockIntersectionObserver {
      constructor(callback: IntersectionObserverCallback) {
        observerCallback = callback;
      }
      observe() {}
      disconnect() {}
      unobserve() {}
      takeRecords() { return []; }
      root = null;
      rootMargin = "";
      thresholds = [];
    }
    globalThis.IntersectionObserver = MockIntersectionObserver as unknown as typeof IntersectionObserver;
    cleanupNavigation = installWorkspaceNavigationEnhancements();
    const audio = document.getElementById("workspace-audio") as HTMLElement;

    observerCallback?.([{
      target: audio,
      isIntersecting: true,
      intersectionRatio: .7,
    } as IntersectionObserverEntry], {} as IntersectionObserver);

    expect(document.querySelector('a[href="#workspace-audio"]')?.getAttribute("aria-current")).toBe("location");
    expect(document.querySelector('a[href="#workspace-overview"]')?.hasAttribute("aria-current")).toBe(false);
  });

  it("decorates navigation that appears after installation", async () => {
    document.body.innerHTML = '<div id="late-root"></div>';
    cleanupNavigation = installWorkspaceNavigationEnhancements();
    document.getElementById("late-root")!.innerHTML = workspaceMarkup();

    await Promise.resolve();
    await Promise.resolve();

    expect(document.querySelector('a[href="#workspace-script"]')?.getAttribute("aria-keyshortcuts")).toBe("Alt+6");
  });
});
