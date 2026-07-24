import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type Clip } from "../src/api";
import { ClipSummaryEditor } from "../src/components/project/ClipSummaryEditor";

function clip(userSummary = "抵達車站後前往飯店", videoId = 1, aiSummary = "車站月台與列車進站"): Clip {
  return {
    clip_id: `clip-${videoId}`,
    video_id: videoId,
    filename: `clip-${videoId}.mp4`,
    status: "perceived",
    segment_count: 2,
    duration_seconds: 12,
    detected_category: "travel",
    time_of_day: "morning",
    visual_summary: aiSummary,
    ai_visual_summary: aiSummary,
    user_summary: userSummary,
    user_summary_updated_at: null,
    user_summary_migration_state: "native",
    effective_summary: userSummary || aiSummary,
    effective_summary_source: userSummary ? "user" : "ai",
  };
}

function renderEditor(input = clip(), projectId = 1) {
  const setMessage = vi.fn();
  const refreshProject = vi.fn(async () => []);
  const view = render(<ClipSummaryEditor projectId={projectId} clip={input} setMessage={setMessage} refreshProject={refreshProject} />);
  return { ...view, setMessage, refreshProject };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ClipSummaryEditor", () => {
  it("shows AI perception read-only and edits only user story notes", () => {
    renderEditor();
    expect(screen.getByLabelText("clip-1.mp4 AI 畫面感知").textContent).toContain("車站月台與列車進站");
    const textarea = screen.getByLabelText("clip-1.mp4 使用者故事備註") as HTMLTextAreaElement;
    expect(textarea.value).toBe("抵達車站後前往飯店");

    fireEvent.change(textarea, { target: { value: "修改後的旅遊故事備註" } });

    expect(screen.getByText("有未儲存變更")).toBeTruthy();
    expect(document.querySelector('[data-unsaved-text-draft="true"]')).toBeTruthy();
    expect((screen.getByRole("button", { name: "儲存故事備註" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByLabelText("clip-1.mp4 AI 畫面感知").textContent).toContain("車站月台與列車進站");

    fireEvent.click(screen.getByRole("button", { name: "放棄變更" }));
    expect(textarea.value).toBe("抵達車站後前往飯店");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("protects a dirty draft from polling and accepts clean server updates", async () => {
    const view = renderEditor();
    const textarea = screen.getByLabelText("clip-1.mp4 使用者故事備註") as HTMLTextAreaElement;

    view.rerender(<ClipSummaryEditor projectId={1} clip={clip("伺服器更新一")} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(textarea.value).toBe("伺服器更新一"));

    fireEvent.change(textarea, { target: { value: "本地尚未儲存" } });
    view.rerender(<ClipSummaryEditor projectId={1} clip={clip("輪詢更新二")} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(textarea.value).toBe("本地尚未儲存"));
  });

  it("saves trimmed user context, clears dirty state, and refreshes rebuilt plan", async () => {
    const save = vi.spyOn(api, "saveClipSummary").mockResolvedValue({ ok: true, plan_rebuilt: true });
    const { refreshProject, setMessage } = renderEditor();
    const textarea = screen.getByLabelText("clip-1.mp4 使用者故事備註") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "  抵達博多站後走出月台，不要放在早餐  " } });
    fireEvent.click(screen.getByRole("button", { name: "儲存故事備註" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(1, 1, "抵達博多站後走出月台，不要放在早餐"));
    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true }));
    expect(textarea.value).toBe("抵達博多站後走出月台，不要放在早餐");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
    expect(setMessage).toHaveBeenCalledWith("使用者故事備註已儲存，故事整理已重建並回到待審。");
  });

  it("keeps the draft available when saving fails", async () => {
    vi.spyOn(api, "saveClipSummary").mockResolvedValue({ ok: false });
    const { setMessage } = renderEditor();
    const textarea = screen.getByLabelText("clip-1.mp4 使用者故事備註") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "仍要保留的草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存故事備註" }));

    await waitFor(() => expect(setMessage).toHaveBeenCalledWith("使用者故事備註儲存失敗：找不到素材。"));
    expect(textarea.value).toBe("仍要保留的草稿");
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
  });

  it("resets the editor when the clip or project identity changes", async () => {
    const view = renderEditor();
    fireEvent.change(screen.getByLabelText("clip-1.mp4 使用者故事備註"), { target: { value: "本地草稿" } });

    view.rerender(<ClipSummaryEditor projectId={1} clip={clip("第二支素材故事", 2)} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect((screen.getByLabelText("clip-2.mp4 使用者故事備註") as HTMLTextAreaElement).value).toBe("第二支素材故事"));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("flags ambiguous legacy summaries for review", () => {
    renderEditor({ ...clip(""), user_summary_migration_state: "review" });
    expect(screen.getByRole("note").textContent).toContain("無法安全判斷來源");
  });

  it("registers browser unload protection only while dirty", () => {
    renderEditor();
    const cleanEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(cleanEvent)).toBe(true);

    fireEvent.change(screen.getByLabelText("clip-1.mp4 使用者故事備註"), { target: { value: "本地草稿" } });
    const dirtyEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(dirtyEvent)).toBe(false);
    expect(dirtyEvent.defaultPrevented).toBe(true);
  });
});
