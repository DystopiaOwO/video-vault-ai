import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ColorState, type ProjectDetail, type StoryboardState } from "../src/api";
import { StoryboardWorkspaceController } from "../src/workspaces/storyboard/StoryboardWorkspaceController";

const adjustment = {
  mode: "manual",
  lut_path: "",
  lut_kind: "",
  exposure: 0,
  temperature: 0,
  tint: 0,
  contrast: 1,
  saturation: 1,
  gamma: 1,
  highlights: 0,
  shadows: 0,
};

function storyboard(notes = ""): StoryboardState {
  return {
    schema_version: 1,
    exists: true,
    groups: [{ group_id: "g1", title: "第一段", category: "travel", order: 1 }],
    segments: {
      a: { group_id: "g1", order: 1, included: true, locked: false, thumbnail_time_ratio: 0.5, notes },
      b: { group_id: "g1", order: 2, included: true, locked: false, thumbnail_time_ratio: 0.5, notes: "" },
    },
    validation: { valid: true, errors: [], warnings: [] },
  };
}

function emptyStoryboard(): StoryboardState {
  return { schema_version: 1, exists: false, groups: [], segments: {} };
}

function audio(): AudioState {
  return {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: {},
  };
}

function color(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: adjustment,
    applied: adjustment,
    segments: {},
  };
}

function detail(projectId = 1, state = storyboard()): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "needs_review" },
    clips: [],
    segments: [
      {
        segment_id: "a",
        clip_id: "clip-a",
        title: "抵達車站",
        group: "travel",
        start_seconds: 0,
        end_seconds: 5,
        score: 0.9,
        suggested_use: "main",
        scene_role: "arrival",
        story_position: "opening",
        manual_order: 1,
        audio_role: "lower",
        speed: 1,
        include: true,
        user_notes: "",
      },
      {
        segment_id: "b",
        clip_id: "clip-b",
        title: "巷弄散步",
        group: "travel",
        start_seconds: 5,
        end_seconds: 10,
        score: 0.8,
        suggested_use: "transition",
        scene_role: "walk",
        story_position: "middle",
        manual_order: 2,
        audio_role: "lower",
        speed: 1,
        include: true,
        user_notes: "",
      },
    ],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "storyboard", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "待核准",
    color: color(),
    audio: audio(),
    storyboard: state,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("StoryboardWorkspaceController", () => {
  it("preserves a dirty storyboard when polling returns a newer server payload", async () => {
    const refreshProject = vi.fn(async () => []);
    const setMessage = vi.fn();
    const update = vi.spyOn(api, "updateStoryboard").mockImplementation(async (_projectId, state) => ({ ok: true, storyboard: state }));
    const view = render(<StoryboardWorkspaceController detail={detail()} setMessage={setMessage} refreshProject={refreshProject} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "本地未儲存內容" } });
    view.rerender(<StoryboardWorkspaceController detail={detail(1, storyboard("伺服器輪詢內容"))} setMessage={setMessage} refreshProject={refreshProject} />);
    expect((screen.getByLabelText("分鏡備註") as HTMLTextAreaElement).value).toBe("本地未儲存內容");

    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].segments.a.notes).toBe("本地未儲存內容");
  });

  it("syncs clean timing from the server but protects a dirty timing draft", () => {
    const base = detail();
    const serverUpdate = detail();
    serverUpdate.segments[0] = { ...serverUpdate.segments[0], end_seconds: 6 };
    const laterServerUpdate = detail();
    laterServerUpdate.segments[0] = { ...laterServerUpdate.segments[0], end_seconds: 7 };
    const view = render(<StoryboardWorkspaceController detail={base} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    view.rerender(<StoryboardWorkspaceController detail={serverUpdate} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("6");

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    view.rerender(<StoryboardWorkspaceController detail={laterServerUpdate} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("8");
    expect(screen.getAllByText("剪點未儲存").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "放棄剪點變更" }));
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("7");
    expect(screen.queryByText("剪點未儲存")).toBeNull();
  });

  it("normalizes reordered segments before saving", async () => {
    const update = vi.spyOn(api, "updateStoryboard").mockImplementation(async (_projectId, state) => ({ ok: true, storyboard: state }));
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "片段下移" }));
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].segments.b.order).toBe(1);
    expect(update.mock.calls[0][1].segments.a.order).toBe(2);
  });

  it("shows the reapproval message returned by storyboard save", async () => {
    vi.spyOn(api, "updateStoryboard").mockResolvedValue({ ok: true, storyboard: storyboard("變更"), approval_invalidated: true });
    const setMessage = vi.fn();
    render(<StoryboardWorkspaceController detail={detail()} setMessage={setMessage} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "改變輸出內容" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(setMessage).toHaveBeenCalledWith("分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("keeps mutation success separate when storyboard refresh fails", async () => {
    vi.spyOn(api, "updateStoryboard").mockResolvedValue({ ok: true, storyboard: storyboard("已儲存") });
    const setMessage = vi.fn();
    const refreshProject = vi.fn().mockRejectedValue(new Error("GET failed"));
    render(<StoryboardWorkspaceController detail={detail()} setMessage={setMessage} refreshProject={refreshProject} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "只改備註" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(setMessage).toHaveBeenLastCalledWith("分鏡已儲存，這次未修改輸出內容，既有核准仍有效。 但畫面更新失敗：GET failed"));
    expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true });
  });

  it("uses create mode for an empty storyboard and does not create a fake dirty state", async () => {
    vi.spyOn(api, "generateStoryboard").mockResolvedValue({ ok: true, storyboard: storyboard() });
    render(<StoryboardWorkspaceController detail={detail(1, emptyStoryboard())} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "建立分鏡" }));

    await waitFor(() => expect(api.generateStoryboard).toHaveBeenCalledWith(1, false));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("keeps timing persistence separate from storyboard persistence and clears the dirty state", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const updateStoryboard = vi.spyOn(api, "updateStoryboard");
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    expect((screen.getByRole("button", { name: "儲存剪點" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    expect((screen.getByRole("button", { name: "儲存剪點" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));

    await waitFor(() => expect(saveTiming).toHaveBeenCalledWith(1, "a", { start_seconds: 0, end_seconds: 8, speed: 2 }));
    await waitFor(() => expect(screen.queryByText("剪點未儲存")).toBeNull());
    expect(updateStoryboard).not.toHaveBeenCalled();
  });

  it("uses the committed timing snapshot for range preview after timing save", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const storyboardPreview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4", duration_seconds: 8 });
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));
    await waitFor(() => expect(saveTiming).toHaveBeenCalledWith(1, "a", { start_seconds: 0, end_seconds: 8, speed: 2 }));

    fireEvent.click(screen.getByRole("button", { name: /巷弄散步/ }));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));
    await waitFor(() => expect(storyboardPreview).toHaveBeenCalledWith(1, expect.objectContaining({
      mode: "range",
      segmentId: "b",
      timelineStartSeconds: 4,
    })));
  });

  it("keeps the newly committed timing when refresh returns a stale detail", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const storyboardPreview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4", duration_seconds: 8 });
    const refreshProject = vi.fn().mockRejectedValue(new Error("GET failed"));
    const view = render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={refreshProject} />);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));
    await waitFor(() => expect(saveTiming).toHaveBeenCalled());

    view.rerender(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={refreshProject} />);
    fireEvent.click(screen.getByRole("button", { name: /巷弄散步/ }));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));
    await waitFor(() => expect(storyboardPreview).toHaveBeenCalledWith(1, expect.objectContaining({
      mode: "range",
      segmentId: "b",
      timelineStartSeconds: 4,
    })));
  });

  it("maps audio role changes to the existing audio settings API", async () => {
    const audioSettings = vi.spyOn(api, "audioSettings").mockResolvedValue({ ok: true, state: audio() });
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("原音角色"), { target: { value: "keep" } });
    await waitFor(() => expect(audioSettings).toHaveBeenCalledWith(1, { segments: { a: { role: "keep" } } }));
  });

  it("maps effective color toggles and reset to the existing color settings API", async () => {
    const input = detail();
    input.color.segments.a = { enabled: false, locked: true, excluded: false };
    const colorSettings = vi.spyOn(api, "colorSettings").mockResolvedValue({ ok: true, state: color() });
    const view = render(<StoryboardWorkspaceController detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "啟用此片段" }));
    await waitFor(() => expect(colorSettings).toHaveBeenCalled());
    expect(colorSettings.mock.calls[0][1].segments.a?.enabled).toBe(true);

    colorSettings.mockClear();
    view.rerender(<StoryboardWorkspaceController detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    fireEvent.click(screen.getByRole("button", { name: "恢復專案預設" }));
    await waitFor(() => expect(colorSettings).toHaveBeenCalledWith(1, expect.objectContaining({ segments: { a: null } })));
  });

  it("generates representative frames with normal and forced cache behavior", async () => {
    const thumbnail = vi.spyOn(api, "storyboardThumbnail").mockResolvedValue({ ok: true, url: "/thumb.jpg", cache_hit: false });
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "產生代表畫格" }));

    await waitFor(() => expect(thumbnail).toHaveBeenCalledWith(1, "a", 0.5, false));
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
    expect((screen.getByAltText("抵達車站 代表畫格") as HTMLImageElement).src).toContain("/thumb.jpg");

    thumbnail.mockClear();
    await waitFor(() => expect((screen.getByLabelText("忽略快取並強制重跑") as HTMLInputElement).disabled).toBe(false));
    fireEvent.click(screen.getByLabelText("忽略快取並強制重跑"));
    fireEvent.click(screen.getByRole("button", { name: "產生代表畫格" }));
    await waitFor(() => expect(thumbnail).toHaveBeenCalledWith(1, "a", 0.5, true));
  });

  it("maps normal and forced previews to the existing API, renders, and clears stale results on selection", async () => {
    const storyboardPreview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4", duration_seconds: 5 });
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "產生 5 秒預覽" }));
    await waitFor(() => expect(storyboardPreview).toHaveBeenCalledWith(1, expect.objectContaining({ mode: "segment", segmentId: "a", durationSeconds: 5, force: false })));
    expect(document.querySelector("video")?.getAttribute("src")).toBe("/preview.mp4");

    fireEvent.click(screen.getByRole("button", { name: /巷弄散步/ }));
    expect(document.querySelector("video")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /抵達車站/ }));
    storyboardPreview.mockClear();
    fireEvent.click(screen.getByLabelText("忽略快取並強制重跑"));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));
    await waitFor(() => expect(storyboardPreview).toHaveBeenCalledWith(1, expect.objectContaining({ mode: "range", segmentId: "a", durationSeconds: 8, timelineStartSeconds: 0, force: true })));
  });

  it("resets local state when switching to another project", () => {
    const first = detail(1, storyboard("project one"));
    const second = detail(2, storyboard("project two"));
    const view = render(<StoryboardWorkspaceController detail={first} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "本地修改" } });
    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    view.rerender(<StoryboardWorkspaceController detail={second} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    expect((screen.getByLabelText("分鏡備註") as HTMLTextAreaElement).value).toBe("project two");
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("5");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
    expect(screen.queryByText("剪點未儲存")).toBeNull();
  });
});
