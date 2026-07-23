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
  const morning = [
    segment("a", "抵達車站"),
    segment("b", "巷弄散步", { order: 2, included: false, locked: true, sceneRole: "walk" }),
  ];
  return {
    exists: true,
    valid: true,
    errors: [],
    warnings: [],
    groups: [
      { id: "morning", title: "早上", category: "travel", order: 1, segments: morning },
      { id: "empty", title: "備用", category: "custom", order: 2, segments: [] },
    ],
    segments: morning,
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

  it("selects rows and supports keyboard navigation without embedding editors in every row", () => {
    const onSelect = vi.fn();
    render(<StoryboardReviewWorkspace {...props({ onSelect })} />);

    fireEvent.click(screen.getByRole("button", { name: /巷弄散步/ }));
    expect(onSelect).toHaveBeenCalledWith("b");

    fireEvent.keyDown(screen.getByRole("button", { name: /抵達車站/ }), { key: "ArrowDown" });
    expect(onSelect).toHaveBeenLastCalledWith("b");
    expect(screen.getAllByLabelText("片段起點")).toHaveLength(1);
    expect(screen.getAllByLabelText("原音角色")).toHaveLength(1);
  });

  it("filters, searches, collapses, and restores the segment list", () => {
    render(<StoryboardReviewWorkspace {...props()} />);

    fireEvent.change(screen.getByLabelText("搜尋片段"), { target: { value: "巷弄" } });
    expect(screen.queryByRole("button", { name: /抵達車站/ })).toBeNull();
    expect(screen.getByRole("button", { name: /巷弄散步/ })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋片段"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "已排除" }));
    expect(screen.queryByRole("button", { name: /抵達車站/ })).toBeNull();
    expect(screen.getByRole("button", { name: /巷弄散步/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "全部" }));
    fireEvent.click(screen.getByRole("button", { name: "早上 收合" }));
    expect(screen.queryByRole("button", { name: /抵達車站/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "早上 展開" }));
    expect(screen.getByRole("button", { name: /抵達車站/ })).toBeTruthy();
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
      timingDirty: { a: true },
      onTimingChange,
      onSaveTiming,
    })} />);

    expect(screen.getByRole("alert").textContent).toContain("片段終點必須大於起點");
    expect((screen.getByRole("button", { name: "儲存剪點" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("片段終點"), { target: { value: "8" } });
    expect(onTimingChange).toHaveBeenCalledWith("a", { endSeconds: 8 });
    expect(onSaveTiming).not.toHaveBeenCalled();
  });

  it("shows timing dirty state, allows reset, and blocks stale previews", () => {
    const onResetTiming = vi.fn();
    const onSaveTiming = vi.fn();
    const onPreview = vi.fn();
    render(<StoryboardReviewWorkspace {...props({ timingDirty: { a: true }, onResetTiming, onSaveTiming, onPreview })} />);

    expect(screen.getAllByText("剪點未儲存").length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "產生 5 秒預覽" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "放棄剪點變更" }));
    expect(onResetTiming).toHaveBeenCalledWith("a");

    fireEvent.click(screen.getByRole("button", { name: "儲存剪點" }));
    expect(onSaveTiming).toHaveBeenCalledWith("a");
    expect(onPreview).not.toHaveBeenCalled();
  });

  it("routes ordering, group management, and thumbnail actions through callbacks", () => {
    const onMoveSegment = vi.fn();
    const onAddGroup = vi.fn();
    const onRenameGroup = vi.fn();
    const onMoveGroup = vi.fn();
    const onDeleteGroup = vi.fn();
    const onGenerateThumbnail = vi.fn();
    render(<StoryboardReviewWorkspace {...props({
      onMoveSegment,
      onAddGroup,
      onRenameGroup,
      onMoveGroup,
      onDeleteGroup,
      onGenerateThumbnail,
    })} />);

    fireEvent.click(screen.getByRole("button", { name: "片段下移" }));
    expect(onMoveSegment).toHaveBeenCalledWith("a", 1);

    fireEvent.change(screen.getByLabelText("新增分組名稱"), { target: { value: "晚上" } });
    fireEvent.click(screen.getByRole("button", { name: "新增分組" }));
    expect(onAddGroup).toHaveBeenCalledWith("晚上");

    fireEvent.change(screen.getByLabelText("早上 分組名稱"), { target: { value: "上午" } });
    expect(onRenameGroup).toHaveBeenCalledWith("morning", "上午");

    fireEvent.click(screen.getByRole("button", { name: "早上 分組下移" }));
    expect(onMoveGroup).toHaveBeenCalledWith("morning", 1);

    fireEvent.click(screen.getByRole("button", { name: "刪除" }));
    expect(onDeleteGroup).toHaveBeenCalledWith("empty");

    fireEvent.click(screen.getByRole("button", { name: "產生代表畫格" }));
    expect(onGenerateThumbnail).toHaveBeenCalledWith("a", 0.5, false);

    fireEvent.click(screen.getByLabelText("忽略快取並強制重跑"));
    fireEvent.click(screen.getByRole("button", { name: "產生代表畫格" }));
    expect(onGenerateThumbnail).toHaveBeenLastCalledWith("a", 0.5, true);
  });

  it("shows preview players and routes preview and color actions", () => {
    const onPreview = vi.fn();
    const onToggleColor = vi.fn();
    const onResetColor = vi.fn();
    const customized = model();
    customized.segments[0] = { ...customized.segments[0], colorCustomized: true };
    customized.groups[0] = { ...customized.groups[0], segments: [customized.segments[0], customized.segments[1]] };
    render(<StoryboardReviewWorkspace {...props({
      model: customized,
      onPreview,
      onToggleColor,
      onResetColor,
      previewItems: [{ kind: "segment", url: "/preview.mp4", durationSeconds: 5 }],
    })} />);

    fireEvent.click(screen.getByRole("button", { name: "產生 5 秒預覽" }));
    expect(onPreview).toHaveBeenCalledWith("a", "segment", false);

    fireEvent.click(screen.getByRole("button", { name: "預覽前後銜接" }));
    expect(onPreview).toHaveBeenCalledWith("a", "transition", false);

    fireEvent.click(screen.getByLabelText("忽略快取並強制重跑"));
    fireEvent.click(screen.getByRole("button", { name: "從此片段預覽 8 秒" }));
    expect(onPreview).toHaveBeenLastCalledWith("a", "range", true);

    fireEvent.click(screen.getByRole("button", { name: "停用此片段" }));
    expect(onToggleColor).toHaveBeenCalledWith("a");

    fireEvent.click(screen.getByRole("button", { name: "恢復專案預設" }));
    expect(onResetColor).toHaveBeenCalledWith("a");

    const video = document.querySelector("video");
    expect(video?.getAttribute("src")).toBe("/preview.mp4");
  });

  it("disables mutations while a controller action is busy", () => {
    render(<StoryboardReviewWorkspace {...props({ busy: true })} />);

    expect((screen.getByRole("button", { name: /抵達車站/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("分鏡備註") as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "儲存分鏡" }) as HTMLButtonElement).disabled).toBe(true);
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
