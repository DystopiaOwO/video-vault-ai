import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ColorState, type ProjectDetail, type StoryboardState } from "../src/api";
import { StoryboardReviewWorkspace } from "../src/workspaces/storyboard/StoryboardReviewWorkspace";
import { StoryboardWorkspaceController } from "../src/workspaces/storyboard/StoryboardWorkspaceController";
import { buildStoryboardViewModel } from "../src/workspaces/storyboard/storyboardViewModel";

const adjustment = {
  mode: "none",
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

function color(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: { ...adjustment },
    applied: { ...adjustment },
    segments: {},
  };
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

function storyboardState(): StoryboardState {
  return {
    schema_version: 1,
    exists: true,
    groups: [
      { group_id: "group-a", title: "A", category: "travel", order: 1 },
      { group_id: "group-b", title: "B", category: "travel", order: 2 },
      { group_id: "group-c", title: "C", category: "travel", order: 3 },
    ],
    segments: {
      a: { group_id: "group-a", order: 1, included: true, locked: false, thumbnail_time_ratio: 0.5, notes: "visible" },
      b: { group_id: "group-b", order: 1, included: true, locked: false, thumbnail_time_ratio: 0.5, notes: "hidden" },
      c: { group_id: "group-c", order: 1, included: true, locked: false, thumbnail_time_ratio: 0.5, notes: "visible" },
    },
  };
}

function detail(state = storyboardState()): ProjectDetail {
  return {
    project: { id: 1, name: "分鏡回歸測試", status: "needs_review" },
    clips: [],
    segments: [
      { segment_id: "a", clip_id: "clip-a", title: "片段 A", group: "travel", start_seconds: 0, end_seconds: 5, score: 0.9, suggested_use: "main", scene_role: "arrival", story_position: "opening", manual_order: 1, audio_role: "lower", speed: 1, include: true, user_notes: "" },
      { segment_id: "b", clip_id: "clip-b", title: "片段 B", group: "travel", start_seconds: 5, end_seconds: 10, score: 0.8, suggested_use: "transition", scene_role: "walk", story_position: "middle", manual_order: 2, audio_role: "lower", speed: 1, include: true, user_notes: "" },
      { segment_id: "c", clip_id: "clip-c", title: "片段 C", group: "travel", start_seconds: 10, end_seconds: 15, score: 0.7, suggested_use: "ending", scene_role: "view", story_position: "ending", manual_order: 3, audio_role: "lower", speed: 1, include: true, user_notes: "" },
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

function renderController(
  input = detail(),
  refreshProject = vi.fn().mockResolvedValue([]),
  setMessage = vi.fn(),
) {
  const view = render(<StoryboardWorkspaceController detail={input} setMessage={setMessage} refreshProject={refreshProject} />);
  return { ...view, refreshProject, setMessage };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Issue #51 Gate 1 Storyboard 行為", () => {
  it("儲存片段 A timing 後，片段 B 的 range preview 使用 A 的 committed duration", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const preview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4", duration_seconds: 8 });
    renderController();

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));
    await waitFor(() => expect(saveTiming).toHaveBeenCalledWith(1, "a", { start_seconds: 0, end_seconds: 8, speed: 2 }));

    fireEvent.click(screen.getByRole("button", { name: /片段 B/ }));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(1, expect.objectContaining({ mode: "range", segmentId: "b", timelineStartSeconds: 4 })));
  });

  it("timing 儲存成功但 refresh 失敗時仍可使用新的 committed timing", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const preview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4", duration_seconds: 8 });
    const refreshProject = vi.fn().mockRejectedValue(new Error("GET failed"));
    renderController(detail(), refreshProject);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));
    await waitFor(() => expect(saveTiming).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /片段 B/ }));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));

    await waitFor(() => expect(preview).toHaveBeenCalledWith(1, expect.objectContaining({ segmentId: "b", timelineStartSeconds: 4 })));
  });

  it("reset timing 後恢復 server committed timing", () => {
    renderController();
    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("8");
    fireEvent.click(screen.getByRole("button", { name: "放棄剪點變更" }));
    expect((screen.getByLabelText("片段終點") as HTMLInputElement).value).toBe("5");
  });

  it("excluded 片段的預覽按鈕與 keyboard path 都不會呼叫 preview", () => {
    const excluded = storyboardState();
    excluded.segments.c = { ...excluded.segments.c, included: false };
    const onPreview = vi.fn();
    const model = buildStoryboardViewModel({ ...detail(excluded), storyboard: excluded });
    render(
      <StoryboardReviewWorkspace
        model={model}
        selectedId="c"
        onSelect={vi.fn()}
        onStoryboardChange={vi.fn()}
        onTimingChange={vi.fn()}
        onSaveTiming={vi.fn()}
        onSave={vi.fn()}
        onRegenerate={vi.fn()}
        onPreview={onPreview}
      />,
    );

    const previewButton = screen.getByRole("button", { name: "從此片段預覽 8 秒" });
    expect((previewButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(previewButton);
    fireEvent.keyDown(screen.getByRole("button", { name: /片段 C/ }), { key: "Enter" });
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("搜尋隱藏 B 時移動 C，儲存仍保留完整 A、B、C 群組 identity 與順序資料", async () => {
    const update = vi.spyOn(api, "updateStoryboard").mockImplementation(async (_projectId, state) => ({ ok: true, storyboard: state }));
    renderController();

    fireEvent.change(screen.getByLabelText("搜尋片段"), { target: { value: "visible" } });
    expect(screen.queryByRole("button", { name: /B (收合|展開)/ })).toBeNull();
    expect((screen.getByRole("button", { name: "C 分組上移" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("搜尋片段"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "C 分組上移" }));
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    const saved = update.mock.calls[0][1];
    expect(saved.groups.map((group) => group.group_id)).toEqual(["group-a", "group-c", "group-b"]);
    expect(Object.keys(saved.segments).sort()).toEqual(["a", "b", "c"]);
  });

  it("折疊與展開群組不改變穩定 group identity，重新命名後仍可保存同一群組", async () => {
    const update = vi.spyOn(api, "updateStoryboard").mockImplementation(async (_projectId, state) => ({ ok: true, storyboard: state }));
    renderController();

    fireEvent.click(screen.getByRole("button", { name: "B 收合" }));
    fireEvent.change(screen.getByLabelText("A 分組名稱"), { target: { value: "A 改名" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(update).toHaveBeenCalled());
    const saved = update.mock.calls[0][1];
    expect(saved.groups.find((group) => group.group_id === "group-a")?.title).toBe("A 改名");
    expect(saved.groups.map((group) => group.group_id).sort()).toEqual(["group-a", "group-b", "group-c"]);
  });

  it("legacy group 改名與重排仍維持由成員決定的 identity", async () => {
    const legacy = {
      ...storyboardState(),
      groups: [
        { group_id: "", group: "morning", title: "早上", category: "travel", order: 1 },
        { group_id: "", group: "night", title: "晚上", category: "travel", order: 2 },
      ],
      segments: {
        a: { ...storyboardState().segments.a, group_id: "morning" },
        b: { ...storyboardState().segments.b, group_id: "night" },
        c: { ...storyboardState().segments.c, group_id: "night", order: 2 },
      },
    } as unknown as StoryboardState;
    const first = buildStoryboardViewModel(detail(legacy));
    const morningId = first.groups.find((group) => group.title === "早上")?.id;
    const renamed = {
      ...legacy,
      groups: legacy.groups.map((group) => group.title === "早上" ? { ...group, title: "上午" } : group),
    };
    const second = buildStoryboardViewModel(detail(renamed));
    expect(morningId).toBeTruthy();
    expect(second.groups.find((group) => group.title === "上午")?.id).toBe(morningId);
  });
});
