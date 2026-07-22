import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, AudioState, ColorState, ProjectDetail, StoryboardState } from "../src/api";
import { colorResetPatch, colorTogglePatch, effectiveSegmentColorEnabled, normalizeStoryboardOrders, reorderStoryboardSegments, StoryboardPanel } from "../src/main";

function color(enabled = true): ColorState {
  const adjustment = { mode: "manual", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 };
  return { schema_version: 2, enabled, reference: {}, references: [], analysis: {}, suggested: adjustment, applied: adjustment, segments: {} };
}

function audio(): AudioState {
  return { schema_version: 1, enabled: true, bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 }, original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 }, normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 }, segments: {} };
}

function storyboard(): StoryboardState {
  return { schema_version: 1, exists: true, groups: [{ group_id: "g1", title: "第一段", category: "travel", order: 1 }, { group_id: "g2", title: "第二段", category: "travel", order: 2 }], segments: {
    a: { group_id: "g1", order: 1, included: true, locked: false, thumbnail_time_ratio: .5, notes: "" },
    b: { group_id: "g1", order: 2, included: true, locked: false, thumbnail_time_ratio: .5, notes: "" },
    c: { group_id: "g2", order: 1, included: true, locked: false, thumbnail_time_ratio: .5, notes: "" },
  } };
}

function detailWith(state = storyboard(), projectId = 1): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "needs_review" },
    clips: [],
    segments: [
      { segment_id: "a", clip_id: "clip_a", title: "A", group: "travel", start_seconds: 0, end_seconds: 4, score: .9, suggested_use: "main", scene_role: "arrival", story_position: "opening", manual_order: 1, audio_role: "lower", speed: 1, include: true, user_notes: "" },
      { segment_id: "b", clip_id: "clip_b", title: "B", group: "travel", start_seconds: 4, end_seconds: 8, score: .8, suggested_use: "main", scene_role: "walk", story_position: "middle", manual_order: 2, audio_role: "lower", speed: 1, include: true, user_notes: "" },
      { segment_id: "c", clip_id: "clip_c", title: "C", group: "travel", start_seconds: 8, end_seconds: 12, score: .7, suggested_use: "detail", scene_role: "meal", story_position: "end", manual_order: 3, audio_role: "lower", speed: 1, include: true, user_notes: "" },
    ],
    bgm: [], plan: {}, workflow: { style: "test", current: "review", stages: [] }, review: {}, script: "", folder: "", can_render: false, render_gate_reason: "needs review", color: color(), audio: audio(), storyboard: state,
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Storyboard state behavior", () => {
  it("polling_does_not_replace_dirty_storyboard", () => {
    const detail = detailWith();
    const refreshed = detailWith({ ...storyboard(), segments: { ...storyboard().segments, a: { ...storyboard().segments.a, notes: "server" } } });
    const refreshProject = vi.fn(async () => []);
    const view = render(<StoryboardPanel detail={detail} setMessage={vi.fn()} refreshProject={refreshProject} />);
    fireEvent.change(screen.getAllByPlaceholderText("分鏡備註")[0], { target: { value: "本地修改" } });
    view.rerender(<StoryboardPanel detail={refreshed} setMessage={vi.fn()} refreshProject={refreshProject} />);
    expect((screen.getAllByPlaceholderText("分鏡備註")[0] as HTMLTextAreaElement).value).toBe("本地修改");
  });

  it("successful_save_clears_dirty_state", async () => {
    const detail = detailWith();
    vi.spyOn(api, "updateStoryboard").mockResolvedValue({ ok: true, storyboard: detail.storyboard });
    const setMessage = vi.fn();
    const view = render(<StoryboardPanel detail={detail} setMessage={setMessage} refreshProject={vi.fn(async () => [])} />);
    fireEvent.change(screen.getAllByPlaceholderText("分鏡備註")[0], { target: { value: "已儲存" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));
    await vi.waitFor(() => expect(screen.queryByText("有未儲存變更")).toBeNull());
    expect(setMessage).toHaveBeenCalledWith("分鏡已儲存，這次未修改輸出內容，既有核准仍有效。");
    view.unmount();
  });

  it("shows reapproval message when storyboard save changes output", async () => {
    const detail = detailWith();
    vi.spyOn(api, "updateStoryboard").mockResolvedValue({ ok: true, storyboard: detail.storyboard, approval_invalidated: true });
    const setMessage = vi.fn();
    render(<StoryboardPanel detail={detail} setMessage={setMessage} refreshProject={vi.fn(async () => [])} />);
    fireEvent.change(screen.getAllByPlaceholderText("分鏡備註")[0], { target: { value: "改變剪輯內容" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));
    await vi.waitFor(() => expect(setMessage).toHaveBeenCalledWith("分鏡已儲存，輸出內容有變更，請重新核准後再正式輸出。"));
  });

  it("quick_audio_refresh_preserves_dirty_storyboard", async () => {
    const detail = detailWith();
    vi.spyOn(api, "audioSettings").mockResolvedValue({ ok: true, state: detail.audio });
    const refreshProject = vi.fn(async () => []);
    const view = render(<StoryboardPanel detail={detail} setMessage={vi.fn()} refreshProject={refreshProject} />);
    fireEvent.change(screen.getAllByPlaceholderText("分鏡備註")[0], { target: { value: "音訊調整前的本地編輯" } });
    fireEvent.change(screen.getAllByLabelText("快速原音")[0], { target: { value: "keep" } });
    await vi.waitFor(() => expect(refreshProject).toHaveBeenCalled());
    view.rerender(<StoryboardPanel detail={detailWith()} setMessage={vi.fn()} refreshProject={refreshProject} />);
    expect((screen.getAllByPlaceholderText("分鏡備註")[0] as HTMLTextAreaElement).value).toBe("音訊調整前的本地編輯");
  });
});

describe("Storyboard ordering and color actions", () => {
  it("drag_before_and_after_segment", () => {
    const state = storyboard();
    expect(Object.keys(reorderStoryboardSegments(state, "b", "a", "before").segments).length).toBe(3);
    const before = reorderStoryboardSegments(state, "b", "a", "before");
    expect(Object.entries(before.segments).filter(([, item]) => item.group_id === "g1").sort(([, a], [, b]) => a.order - b.order).map(([id]) => id)).toEqual(["b", "a"]);
    const after = reorderStoryboardSegments(state, "a", "b", "after");
    expect(Object.entries(after.segments).filter(([, item]) => item.group_id === "g1").sort(([, a], [, b]) => a.order - b.order).map(([id]) => id)).toEqual(["b", "a"]);
  });

  it("cross_group_drag_normalizes_orders", () => {
    const moved = reorderStoryboardSegments(storyboard(), "a", "c", "before");
    expect(moved.segments.a.group_id).toBe("g2");
    expect(moved.segments.a.order).toBe(1);
    expect(moved.segments.c.order).toBe(2);
    expect(moved.segments.b.order).toBe(1);
  });

  it("normalizes_group_and_segment_orders", () => {
    const state = storyboard();
    state.groups[0].order = 9;
    state.groups[1].order = 2;
    state.segments.a.order = 9;
    state.segments.b.order = 2;
    const normalized = normalizeStoryboardOrders(state);
    expect(normalized.groups.map((group) => group.order)).toEqual([1, 2]);
    expect(normalized.segments.a.order).toBe(2);
  });

  it("quick_color_uses_effective_enabled_state_and_reset_patch", () => {
    const enabled = color(true);
    expect(effectiveSegmentColorEnabled(enabled.enabled, undefined)).toBe(true);
    expect(colorTogglePatch(enabled, "a").enabled).toBe(false);
    const disabled = color(false);
    expect(effectiveSegmentColorEnabled(disabled.enabled, undefined)).toBe(false);
    expect(colorTogglePatch(disabled, "a").enabled).toBe(true);
    disabled.segments.a = { enabled: false, locked: true, excluded: false };
    expect(colorTogglePatch(disabled, "a").enabled).toBe(true);
    expect(colorResetPatch()).toBeNull();
  });
});
