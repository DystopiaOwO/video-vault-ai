import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  StoryboardReviewWorkspace,
  type StoryboardReviewWorkspaceProps,
} from "../src/workspaces/storyboard/StoryboardReviewWorkspace";
import type { StoryboardSegmentView, StoryboardViewModel } from "../src/workspaces/storyboard/storyboardViewModel";

function segment(id: string, title: string, patch: Partial<StoryboardSegmentView> = {}): StoryboardSegmentView {
  return {
    id,
    title,
    sourceName: `${id}-very-long-source-filename.MP4`,
    groupId: "morning",
    groupTitle: "早上",
    groupOrder: 1,
    order: 1,
    included: true,
    locked: false,
    thumbnailUrl: "",
    thumbnailRatio: 0.5,
    startSeconds: 0,
    endSeconds: 5,
    speed: 1,
    durationSeconds: 5,
    score: 0.9,
    sceneRole: "opening",
    storyPosition: "opening",
    suggestedUse: "main",
    notes: "",
    audioRole: "lower",
    audioLabel: "降低原音",
    audioCustomized: false,
    colorEnabled: true,
    colorCustomized: false,
    colorWarnings: [],
    ...patch,
  };
}

function model(): StoryboardViewModel {
  const segments = [
    segment("a", "抵達車站"),
    segment("b", "巷弄散步", { order: 2, included: false, locked: true, sceneRole: "walk" }),
  ];
  return {
    exists: true,
    valid: true,
    errors: [],
    warnings: [],
    groups: [{ id: "morning", title: "早上", category: "travel", order: 1, segments }],
    segments,
    summary: { totalSegments: 2, includedSegments: 1, excludedSegments: 1, estimatedDurationSeconds: 5 },
  };
}

function props(overrides: Partial<StoryboardReviewWorkspaceProps> = {}): StoryboardReviewWorkspaceProps {
  return {
    model: model(),
    selectedId: "a",
    dirty: true,
    onSelect: vi.fn(),
    onStoryboardChange: vi.fn(),
    onTimingChange: vi.fn(),
    onSaveTiming: vi.fn(),
    onSave: vi.fn(),
    onRegenerate: vi.fn(),
    onPreview: vi.fn(),
    ...overrides,
  };
}

afterEach(cleanup);

describe("StoryboardReviewWorkspace", () => {
  it("renders one compact list and one inspector instead of duplicate editors", () => {
    render(<StoryboardReviewWorkspace {...props()} />);

    expect(screen.getAllByText("片段設定")).toHaveLength(1);
    expect(screen.getAllByLabelText("分鏡備註")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /抵達車站/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /巷弄散步/ })).toBeTruthy();
    expect(screen.getByText("排除 1 段")).toBeTruthy();
  });

  it("selects a row without embedding editing controls in every row", () => {
    const onSelect = vi.fn();
    render(<StoryboardReviewWorkspace {...props({ onSelect })} />);

    fireEvent.click(screen.getByRole("button", { name: /巷弄散步/ }));
    expect(onSelect).toHaveBeenCalledWith("b");
    expect(screen.getAllByLabelText("片段起點")).toHaveLength(1);
    expect(screen.getAllByLabelText("原音角色")).toHaveLength(1);
  });

  it("emits storyboard-only changes through the controlled callback", () => {
    const onStoryboardChange = vi.fn();
    render(<StoryboardReviewWorkspace {...props({ onStoryboardChange })} />);

    fireEvent.click(screen.getByLabelText("納入成片"));
    expect(onStoryboardChange).toHaveBeenCalledWith("a", { included: false });

    fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "保留環境聲" } });
    expect(onStoryboardChange).toHaveBeenCalledWith("a", { notes: "保留環境聲" });
  });

  it("keeps timing edits separate and blocks an invalid timing save", () => {
    const onTimingChange = vi.fn();
    const onSaveTiming = vi.fn();
    render(<StoryboardReviewWorkspace {...props({
      timingDrafts: { a: { startSeconds: 5, endSeconds: 4, speed: 1 } },
      onTimingChange,
      onSaveTiming,
    })} />);

    expect(screen.getByRole("alert").textContent).toContain("片段終點必須大於起點");
    expect((screen.getByRole("button", { name: "儲存剪點" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    expect(onTimingChange).toHaveBeenCalledWith("a", { endSeconds: 8 });
    expect(onSaveTiming).not.toHaveBeenCalled();
  });

  it("routes preview and effective color actions through callbacks", () => {
    const onPreview = vi.fn();
    const onToggleColor = vi.fn();
    render(<StoryboardReviewWorkspace {...props({ onPreview, onToggleColor })} />);

    fireEvent.click(screen.getByRole("button", { name: "產生 5 秒預覽" }));
    expect(onPreview).toHaveBeenCalledWith("a", "segment");

    fireEvent.click(screen.getByRole("button", { name: "停用此片段" }));
    expect(onToggleColor).toHaveBeenCalledWith("a");
  });

  it("shows a single create action when storyboard data does not exist", () => {
    const onRegenerate = vi.fn();
    const empty = { ...model(), exists: false, groups: [], segments: [], summary: { totalSegments: 0, includedSegments: 0, excludedSegments: 0, estimatedDurationSeconds: 0 } };
    render(<StoryboardReviewWorkspace {...props({ model: empty, selectedId: undefined, dirty: false, onRegenerate })} />);

    expect(screen.getByText("尚未建立分鏡")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "建立分鏡" }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });
});
