import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { collectUnsavedDrafts, UnsavedDraftIndicator } from "../src/components/UnsavedDraftIndicator";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("UnsavedDraftIndicator", () => {
  it("collects workspace badges and text drafts with counts", () => {
    document.body.innerHTML = `
      <section id="workspace-storyboard">
        <div class="review-toolbar-actions">
          <strong class="review-dirty">有未儲存變更</strong>
          <strong class="review-dirty timing">剪點未儲存 2</strong>
        </div>
      </section>
      <section id="workspace-review"><textarea placeholder="記錄核准理由、退回項目或重建故事需求">待確認</textarea></section>
      <section id="workspace-media">
        <div class="color-header-actions"><strong>有未儲存變更</strong></div>
        <textarea placeholder="內容感知描述">原始內容</textarea>
      </section>
      <section id="workspace-audio"><div class="audio-header-actions"><strong>有未儲存變更</strong></div></section>
    `;
    const summary = document.querySelector('textarea[placeholder="內容感知描述"]') as HTMLTextAreaElement;
    summary.value = "本地修改";

    expect(collectUnsavedDrafts()).toEqual([
      { id: "storyboard", label: "分鏡", target: "#workspace-storyboard" },
      { id: "timing", label: "剪點", target: "#workspace-storyboard", count: 2 },
      { id: "color", label: "調色", target: "#workspace-media .color-workspace" },
      { id: "audio", label: "音訊", target: "#workspace-audio" },
      { id: "review", label: "審核備註", target: "#workspace-review" },
      { id: "clip-summary", label: "素材描述", target: "#workspace-media", count: 1 },
    ]);
  });

  it("renders only while drafts exist and jumps to the selected workspace", async () => {
    const target = document.createElement("section");
    target.id = "workspace-audio";
    target.tabIndex = -1;
    target.innerHTML = '<div class="audio-header-actions"><strong>有未儲存變更</strong></div>';
    target.scrollIntoView = vi.fn();
    document.body.appendChild(target);

    render(<UnsavedDraftIndicator />);

    const button = await screen.findByRole("button", { name: "音訊" });
    fireEvent.click(button);
    expect(target.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });

    target.querySelector("strong")?.remove();
    await waitFor(() => expect(screen.queryByLabelText("未儲存變更摘要")).toBeNull());
  });

  it("updates when review notes are edited", async () => {
    document.body.innerHTML = '<section id="workspace-review"><textarea placeholder="記錄核准理由、退回項目或重建故事需求"></textarea></section>';
    render(<UnsavedDraftIndicator />);
    expect(screen.queryByLabelText("未儲存變更摘要")).toBeNull();

    const textarea = document.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.input(textarea, { target: { value: "補上轉場" } });

    expect(await screen.findByRole("button", { name: "審核備註" })).toBeTruthy();
  });
});
