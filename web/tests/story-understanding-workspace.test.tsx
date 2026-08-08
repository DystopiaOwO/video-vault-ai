import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { createProjectMutationControls, ProjectMutationCoordinator } from "../src/projectMutation";
import { StoryUnderstandingWorkspace } from "../src/workspaces/story/StoryUnderstandingWorkspace";

function detail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    project: { id: 1, name: "旅行故事", status: "needs_review" },
    project_revision: 7,
    clips: [],
    segments: [{
      segment_id: "segment-a",
      clip_id: "clip-a",
      title: "抵達車站",
      group: "travel",
      start_seconds: 0,
      end_seconds: 4,
      score: .9,
      suggested_use: "main",
      scene_role: "arrival",
      story_position: "opening",
      manual_order: 1,
      audio_role: "keep",
      speed: 1,
      include: true,
      user_notes: "",
      media_url: "/api/project/media?project_id=1&media_id=clip-a",
    }, {
      segment_id: "segment-b",
      clip_id: "clip-a",
      title: "巷弄散步",
      group: "travel",
      start_seconds: 4,
      end_seconds: 8,
      score: .8,
      suggested_use: "transition",
      scene_role: "walk",
      story_position: "middle",
      manual_order: 2,
      audio_role: "keep",
      speed: 1,
      include: true,
      user_notes: "",
      media_url: "/api/project/media?project_id=1&media_id=clip-a",
    }],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "story", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "待核准",
    color: {} as ProjectDetail["color"],
    audio: {} as ProjectDetail["audio"],
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
    story: {
      settings: { profile_id: "travel_diary", profile_version: 5, project_intent: "原始意圖", desired_pacing: "自然" },
      creator_profile: { profile_version: 3, wording_style: "簡潔", visual_style: "自然" },
      story_profile: { profile_id: "travel_diary", label: "旅行日記" },
      generations: [],
      current_story_generation_uuid: "gen-1",
      current_input_hash: "current-hash",
      current_generation_is_stale: false,
      current_generation: {
        story_generation_uuid: "gen-1",
        project_id: 1,
        generation: 1,
        status: "succeeded",
        input_hash: "current-hash",
        provider: "mock",
        model: "mock",
        normalized_response: {
          project_summary: "摘要",
          chapters: [{ chapter_id: "chapter-a", title: "抵達", purpose: "開場", segment_uuids: ["segment-a", "segment-b"], confidence: .9 }],
          suppressed_segments: [{ segment_uuid: "segment-duplicate", representative_segment_uuid: "segment-a", reason: "duplicate" }],
        },
        review_state: { project_summary: "摘要", chapters: [{ chapter_id: "chapter-a", title: "抵達", purpose: "開場", segment_uuids: ["segment-a", "segment-b"], confidence: .9 }] },
        validation: { status: "valid" },
        provider_audit: { calls: 2, retries: 1, total_latency_ms: 42, strict_schema: true },
        story_audit: {
          raw: { provider: "mock", model: "mock", input_hash: "current-hash", schema_version: 1 },
          normalized: { chapter_count: 1, segment_count: 2, validation_status: "valid" },
          effective: { source: "normalized", locked: false, chapter_count: 1, segment_count: 2 },
        },
      },
      calibration: { profile_id: "travel_diary", status: "ready", sample_count: 2, source: "approved outputs and render metadata" },
    },
    ...overrides,
  };
}

function renderWorkspace(input = detail(), refreshProject = vi.fn(async () => [])) {
  const setMessage = vi.fn();
  const mutationControls = createProjectMutationControls(new ProjectMutationCoordinator());
  const merged = { ...detail(), ...input, story: input.story || detail().story };
  const view = render(<StoryUnderstandingWorkspace detail={merged} setMessage={setMessage} refreshProject={refreshProject} mutationControls={mutationControls} />);
  return { ...view, setMessage, refreshProject };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("StoryUnderstandingWorkspace", () => {
  it("keeps a dirty text draft when polling supplies newer server data", () => {
    const view = renderWorkspace();
    fireEvent.change(screen.getByLabelText("專案故事意圖"), { target: { value: "本地尚未儲存的故事意圖" } });

    view.rerender(<StoryUnderstandingWorkspace detail={detail({ story: { ...detail().story!, settings: { ...detail().story!.settings, project_intent: "伺服器新值" } } })} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    expect((screen.getByLabelText("專案故事意圖") as HTMLTextAreaElement).value).toBe("本地尚未儲存的故事意圖");
    expect(document.querySelector('[data-unsaved-text-draft="true"]')).toBeTruthy();
  });

  it("saves Creator separately from project story settings", async () => {
    const creatorSave = vi.spyOn(api, "saveCreatorProfile").mockResolvedValue({ ok: true, profile_version: 4 });
    const settingsSave = vi.spyOn(api, "saveStorySettings").mockResolvedValue({ ok: true, settings: {} });
    renderWorkspace();

    fireEvent.change(screen.getByLabelText("Creator wording style"), { target: { value: "更有畫面感" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存 Creator" }));
    await waitFor(() => expect(creatorSave).toHaveBeenCalledWith(expect.objectContaining({ wording_style: "更有畫面感" }), 3));
    await waitFor(() => expect((screen.getByRole("button", { name: "儲存設定" }) as HTMLButtonElement).disabled).toBe(false));
    expect(settingsSave).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("專案故事意圖"), { target: { value: "新的意圖" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存設定" }));
    await waitFor(() => expect(settingsSave).toHaveBeenCalledWith(1, expect.objectContaining({ project_intent: "新的意圖" }), 7, 5));
  });

  it("submits the app-owned chapter id after a human title edit", async () => {
    const reviewSave = vi.spyOn(api, "updateStoryReview").mockResolvedValue({ ok: true });
    renderWorkspace();

    fireEvent.change(screen.getByLabelText("標題"), { target: { value: "抵達車站（人工命名）" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存故事修改" }));
    await waitFor(() => expect(reviewSave).toHaveBeenCalled());
    expect(reviewSave.mock.calls[0]?.[2]).toEqual(expect.objectContaining({
      chapters: [expect.objectContaining({ chapter_id: "chapter-a", title: "抵達車站（人工命名）" })],
    }));
  });

  it("reorders visual segment cards inside the story draft", async () => {
    const reviewSave = vi.spyOn(api, "updateStoryReview").mockResolvedValue({ ok: true });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "片段 segment-a 下移" }));
    fireEvent.click(screen.getByRole("button", { name: "儲存故事修改" }));
    await waitFor(() => expect(reviewSave).toHaveBeenCalled());
    expect(reviewSave.mock.calls[0]?.[2]).toEqual(expect.objectContaining({
      chapters: [expect.objectContaining({ segment_uuids: ["segment-b", "segment-a"] })],
    }));
  });

  it("shows stale, suppression, and provider audit evidence", () => {
    renderWorkspace({ story: { ...detail().story!, current_generation_is_stale: true } });

    expect(screen.getByRole("alert").textContent).toContain("Apply 會 fail closed");
    expect(screen.getByLabelText("故事 audit").textContent).toContain("raw provider calls：2");
    expect(screen.getByLabelText("故事 audit").textContent).toContain("corrective retry：1");
    expect(screen.getByLabelText("duplicate suppression evidence").textContent).toContain("segment-duplicate → representative segment-a");
    expect(screen.getByLabelText("raw normalized effective audit").textContent).toContain("Raw provider audit");
    expect(screen.getByLabelText("章節 1 視覺片段 cards").querySelector('[data-segment-card="segment-a"]')).toBeTruthy();
  });

  it.each([
    ["refresh rejection", new Error("refresh failed")],
    ["refresh abort", Object.assign(new Error("signal is aborted without reason"), { name: "AbortError" })],
  ])("keeps Apply success semantics when %s follows a successful API call", async (_label, refreshError) => {
    const apply = vi.spyOn(api, "applyStory").mockResolvedValue({ ok: true, approval_invalidated: true });
    const refreshProject = vi.fn(async () => { throw refreshError; });
    const { setMessage } = renderWorkspace(detail(), refreshProject);

    fireEvent.click(screen.getByRole("button", { name: "套用到既有分鏡" }));

    await waitFor(() => expect(apply).toHaveBeenCalledWith(1, "gen-1", 7));
    await waitFor(() => expect(setMessage).toHaveBeenLastCalledWith(expect.stringContaining("故事已套用到分鏡")));
    expect(setMessage).not.toHaveBeenLastCalledWith(expect.stringContaining("套用分鏡失敗"));
    expect(setMessage).toHaveBeenLastCalledWith(expect.stringContaining("請重新整理"));
    expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true });
  });

  it("marks story lock dirty and preserves it across polling until review save", async () => {
    const view = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "鎖定故事修改" }));
    expect(document.querySelector('[data-unsaved-text-draft="true"]')).toBeTruthy();

    view.rerender(<StoryUnderstandingWorkspace detail={detail({ story: { ...detail().story!, current_generation: { ...detail().story!.current_generation!, review_state: { ...detail().story!.current_generation!.review_state!, locked: false } } } })} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect(screen.getByRole("button", { name: "解除鎖定故事" })).toBeTruthy();

    const reviewSave = vi.spyOn(api, "updateStoryReview").mockResolvedValue({ ok: true });
    fireEvent.click(screen.getByRole("button", { name: "儲存故事修改" }));
    await waitFor(() => expect(reviewSave).toHaveBeenCalledWith(1, "gen-1", expect.objectContaining({ locked: true }), 7));
  });

  it("wires calibration recalculate and reset actions", async () => {
    const recalculate = vi.spyOn(api, "recalculateStoryCalibration").mockResolvedValue({ ok: true });
    const reset = vi.spyOn(api, "resetStoryCalibration").mockResolvedValue({ ok: true });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "重新計算" }));
    await waitFor(() => expect(recalculate).toHaveBeenCalledWith(1, "travel_diary"));
    fireEvent.click(screen.getByRole("button", { name: "重設" }));
    await waitFor(() => expect(reset).toHaveBeenCalledWith(1, "travel_diary"));
  });
});
