import { useEffect, useState } from "react";
import { hasUnsavedTextDrafts } from "../unsavedNavigationGuard";
import "./unsaved-draft-indicator.css";

export type UnsavedDraftItem = {
  id: string;
  label: string;
  target: string;
  count?: number;
};

export function collectUnsavedDrafts(root: ParentNode = document): UnsavedDraftItem[] {
  const items: UnsavedDraftItem[] = [];
  const hasText = (selector: string, text: string) => Array.from(root.querySelectorAll(selector))
    .some((element) => element.textContent?.includes(text));

  if (hasText(".review-toolbar-actions", "有未儲存變更")) {
    items.push({ id: "storyboard", label: "分鏡", target: "#workspace-storyboard" });
  }

  const timing = Array.from(root.querySelectorAll(".review-dirty.timing"))
    .map((element) => Number(element.textContent?.match(/\d+/)?.[0] || 0))
    .reduce((total, value) => total + value, 0);
  if (timing > 0) items.push({ id: "timing", label: "剪點", target: "#workspace-storyboard", count: timing });

  if (hasText(".color-header-actions", "有未儲存變更")) {
    items.push({ id: "color", label: "調色", target: "#workspace-media .color-workspace" });
  }

  if (hasText(".audio-header-actions", "有未儲存變更")) {
    items.push({ id: "audio", label: "音訊", target: "#workspace-audio" });
  }

  const reviewNotes = root.querySelector<HTMLTextAreaElement>('#workspace-review textarea[placeholder*="核准理由"]');
  if (reviewNotes?.value.trim()) items.push({ id: "review", label: "審核備註", target: "#workspace-review" });

  const clipDraftCount = Array.from(root.querySelectorAll<HTMLTextAreaElement>('textarea[placeholder="內容感知描述"]'))
    .filter((textarea) => textarea.value !== textarea.defaultValue).length;
  if (clipDraftCount > 0) items.push({ id: "clip-summary", label: "素材描述", target: "#workspace-media", count: clipDraftCount });

  return items;
}

export function UnsavedDraftIndicator() {
  const [items, setItems] = useState<UnsavedDraftItem[]>([]);

  useEffect(() => {
    let frame = 0;
    const refresh = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => setItems(collectUnsavedDrafts()));
    };
    const observer = new MutationObserver(refresh);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    document.addEventListener("input", refresh, true);
    document.addEventListener("change", refresh, true);
    refresh();
    return () => {
      observer.disconnect();
      document.removeEventListener("input", refresh, true);
      document.removeEventListener("change", refresh, true);
      window.cancelAnimationFrame(frame);
    };
  }, []);

  if (!items.length || !hasUnsavedTextDrafts() && !document.querySelector(".review-dirty, .audio-header-actions strong, .color-header-actions strong")) return null;

  function focus(item: UnsavedDraftItem) {
    const target = document.querySelector<HTMLElement>(item.target);
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.focus({ preventScroll: true });
  }

  return <aside className="unsaved-draft-indicator" aria-label="未儲存變更摘要" aria-live="polite">
    <div>
      <strong>尚未儲存</strong>
      <span>切換專案或離開頁面前請先確認。</span>
    </div>
    <nav aria-label="跳到未儲存區域">
      {items.map((item) => <button key={item.id} type="button" onClick={() => focus(item)}>
        {item.label}{item.count ? ` ${item.count}` : ""}
      </button>)}
    </nav>
  </aside>;
}
