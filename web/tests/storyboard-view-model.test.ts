import { describe, expect, it } from "vitest";
import type { AudioState, ColorState, ProjectDetail, StoryboardState } from "../src/api";
import {
  buildStoryboardViewModel,
  updateStoryboardSegment,
  validateSegmentTiming,
} from "../src/workspaces/storyboard/storyboardViewModel";

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

function audio(): AudioState {
  return {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: { b: { role: "keep" } },
  };
}

function color(enabled = true): ColorState {
  return {
    schema_version: 2,
    enabled,
    reference: {},
    references: [],
    analysis: {},
    suggested: adjustment,
    applied: adjustment,
    segments: {
      b: { enabled: false, locked: false, excluded: false, warnings: ["亮度偏低"] },
    },
  };
}

function storyboard(): StoryboardState {
  return {
    schema_version: 1,
    exists: true,
    groups: [
      { group_id: "afternoon", title: "下午", category: "travel", order: 2 },
      { group_id: "morning", title: "早上", category: "travel", order: 1 },
    ],
    segments: {
      a: { group_id: "morning", order: 2, included: true, locked: false, thumbnail_time_ratio: 0.5, notes: "第二段" },
      b: { group_id: "morning", order: 1, included: false, locked: true, thumbnail_time_ratio: 0.25, notes: "第一段", thumbnail_url: "/thumb/b.jpg" },
      c: { group_id: "afternoon", order: 1, included: true, locked: false, thumbnail_time_ratio: 0.75, notes: "" },
    },
    validation: { valid: true, errors: [], warnings: ["尚未核准"] },
  };
}

function detail(): ProjectDetail {
  return {
    project: { id: 1, name: "旅行日記", status: "needs_review" },
    clips: [],
    segments: [
      { segment_id: "a", clip_id: "clip-a", title: "車站", group: "travel", start_seconds: 0, end_seconds: 6, score: 0.9, suggested_use: "main", scene_role: "arrival", story_position: "opening", manual_order: 2, audio_role: "lower", speed: 1, include: true, user_notes: "" },
      { segment_id: "b", clip_id: "clip-b", title: "街景", group: "travel", start_seconds: 6, end_seconds: 14, score: 0.8, suggested_use: "transition", scene_role: "walk", story_position: "middle", manual_order: 1, audio_role: "lower", speed: 2, include: true, user_notes: "", source_filename: "DJI_LONG_FILENAME.MP4" },
      { segment_id: "c", clip_id: "clip-c", title: "咖啡", group: "travel", start_seconds: 14, end_seconds: 24, score: 0.95, suggested_use: "detail", scene_role: "meal", story_position: "ending", manual_order: 3, audio_role: "keep_original", speed: 1, include: true, user_notes: "" },
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
    storyboard: storyboard(),
  };
}

describe("storyboard production view model", () => {
  it("uses storyboard grouping and ordering instead of legacy row order", () => {
    const view = buildStoryboardViewModel(detail());

    expect(view.groups.map((group) => group.title)).toEqual(["早上", "下午"]);
    expect(view.groups[0].segments.map((segment) => segment.id)).toEqual(["b", "a"]);
    expect(view.segments.map((segment) => segment.id)).toEqual(["b", "a", "c"]);
    expect(view.summary).toEqual({
      totalSegments: 3,
      includedSegments: 2,
      excludedSegments: 1,
      estimatedDurationSeconds: 16,
    });
  });

  it("combines storyboard, audio, and color effective state", () => {
    const view = buildStoryboardViewModel(detail());
    const selected = view.segments.find((segment) => segment.id === "b");

    expect(selected).toMatchObject({
      title: "街景",
      sourceName: "DJI_LONG_FILENAME.MP4",
      included: false,
      locked: true,
      thumbnailUrl: "/thumb/b.jpg",
      thumbnailRatio: 0.25,
      durationSeconds: 4,
      notes: "第一段",
      audioRole: "keep",
      audioLabel: "保留原音",
      audioCustomized: true,
      colorEnabled: false,
      colorCustomized: true,
      colorWarnings: ["亮度偏低"],
    });
    expect(view.warnings).toEqual(["尚未核准"]);
  });

  it("allows an explicit segment color enable when the project default is disabled", () => {
    const input = detail();
    input.color = color(false);
    input.color.segments.a = { enabled: true, locked: false, excluded: false };

    const view = buildStoryboardViewModel(input);
    expect(view.segments.find((segment) => segment.id === "a")?.colorEnabled).toBe(true);
    expect(view.segments.find((segment) => segment.id === "c")?.colorEnabled).toBe(false);
  });

  it("keeps unknown groups visible instead of dropping segments", () => {
    const input = detail();
    delete input.storyboard.segments.c;
    input.segments[2].group = "night_market";

    const view = buildStoryboardViewModel(input);
    expect(view.groups.map((group) => group.title)).toContain("night market");
    expect(view.segments.map((segment) => segment.id)).toContain("c");
  });

  it("updates one storyboard segment without mutating the original state", () => {
    const original = storyboard();
    const updated = updateStoryboardSegment(original, "a", { included: false, notes: "改成排除" });

    expect(updated).not.toBe(original);
    expect(updated.segments.a).not.toBe(original.segments.a);
    expect(updated.segments.a.included).toBe(false);
    expect(updated.segments.a.notes).toBe("改成排除");
    expect(original.segments.a.included).toBe(true);
    expect(original.segments.a.notes).toBe("第二段");
  });

  it("validates timing with the same user-facing boundaries", () => {
    expect(validateSegmentTiming(0, 5, 1, 10)).toEqual([]);
    expect(validateSegmentTiming(-1, -2, 0.1, 0.5)).toEqual([
      "片段起點不可小於 0 秒",
      "片段終點必須大於起點",
      "片段速度必須介於 0.25 到 4 倍",
    ]);
    expect(validateSegmentTiming(0, 12, 1, 10)).toEqual(["片段終點超過來源影片長度"]);
  });
});
