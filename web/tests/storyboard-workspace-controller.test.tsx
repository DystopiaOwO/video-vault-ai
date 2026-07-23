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
    },
    validation: { valid: true, errors: [], warnings: [] },
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
    segments: [{
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
    }],
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

  it("shows the reapproval message returned by storyboard save", async () => {
    vi.spyOn(api, "updateStoryboard").mockResolvedValue({ ok: true, storyboard: storyboard("變更"), approval_invalidated: true });
    const setMessage = vi.fn();
    render(<StoryboardWorkspaceController detail={detail()} setMessage={setMessage} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "改變輸出內容" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));

    await waitFor(() => expect(setMessage).toHaveBeenCalledWith("分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("keeps timing persistence separate from storyboard persistence", async () => {
    const saveTiming = vi.spyOn(api, "saveSegmentTiming").mockResolvedValue({ ok: true, path: "segment_review.json" });
    const updateStoryboard = vi.spyOn(api, "updateStoryboard");
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("片段速度"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));

    await waitFor(() => expect(saveTiming).toHaveBeenCalledWith(1, "a", { start_seconds: 0, end_seconds: 8, speed: 2 }));
    expect(updateStoryboard).not.toHaveBeenCalled();
  });

  it("maps audio, color, and preview controls to the existing APIs", async () => {
    const audioSettings = vi.spyOn(api, "audioSettings").mockResolvedValue({ ok: true, state: audio() });
    const colorSettings = vi.spyOn(api, "colorSettings").mockResolvedValue({ ok: true, state: color() });
    const storyboardPreview = vi.spyOn(api, "storyboardPreview").mockResolvedValue({ ok: true, url: "/preview.mp4" });
    const refreshProject = vi.fn(async () => []);
    render(<StoryboardWorkspaceController detail={detail()} setMessage={vi.fn()} refreshProject={refreshProject} />);

    fireEvent.change(screen.getByLabelText("原音角色"), { target: { value: "keep" } });
    await waitFor(() => expect(audioSettings).toHaveBeenCalledWith(1, { segments: { a: { role: "keep" } } }));

    fireEvent.click(screen.getByRole("button", { name: "停用此片段" }));
    await waitFor(() => expect(colorSettings).toHaveBeenCalled());
    expect(colorSettings.mock.calls[0][1].segments.a?.enabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "產生 5 秒預覽" }));
    await waitFor(() => expect(storyboardPreview).toHaveBeenCalledWith(1, expect.objectContaining({ mode: "segment", segmentId: "a", durationSeconds: 5 })));
  });

  it("resets local state when switching to another project", () => {
    const first = detail(1, storyboard("project one"));
    const second = detail(2, storyboard("project two"));
    const view = render(<StoryboardWorkspaceController detail={first} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "本地修改" } });
    view.rerender(<StoryboardWorkspaceController detail={second} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    expect((screen.getByLabelText("分鏡備註") as HTMLTextAreaElement).value).toBe("project two");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });
});
