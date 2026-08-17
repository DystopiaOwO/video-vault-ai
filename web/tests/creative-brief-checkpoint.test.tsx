import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { CreativeBriefCheckpoint } from "../src/workspaces/creative/CreativeBriefCheckpoint";
import { ProjectMutationCoordinator, createProjectMutationControls } from "../src/projectMutation";

function detail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    project: { id: 1, name: "Coffee", status: "draft" },
    project_revision: 4,
    clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "", current: "", stages: [] },
    review: {}, script: "", folder: "", can_render: false, render_gate_reason: "", color: {} as ProjectDetail["color"],
    audio: {} as ProjectDetail["audio"], storyboard: { schema_version: 1, groups: [], segments: {}, exists: false },
    creative_brief: {
      schema_version: 1,
      contract_version: "creative-brief-v1",
      brief_version: 1,
      status: "needs_confirmation",
      recommendation: {
        reason: "素材幾何摘要：2 支直向、1 支橫向",
        output: { orientation: "portrait", aspect_ratio: "9:16", width: 1080, height: 1920, render_profile_id: "final_1080p_portrait" },
        source_orientation_summary: { portrait: 2, landscape: 1, square: 0, unknown: 0 },
        framing_intent: {
          portrait_source_in_landscape: { recommended_strategy: "crop_reframe" },
          landscape_source_in_portrait: { recommended_strategy: "crop_reframe" },
        },
      },
      approved: {},
      options: {
        registry_version: "creative-brief-registry-v1",
        registry_hash: "registry-hash",
        output_contracts: [
          { output_contract_id: "landscape_16_9", version: "1", orientation: "landscape", aspect_ratio: "16:9", width: 1920, height: 1080, render_profile_id: "final_1080p", enabled_for_round1_ui: true, label: "橫向 16:9" },
          { output_contract_id: "portrait_9_16", version: "1", orientation: "portrait", aspect_ratio: "9:16", width: 1080, height: 1920, render_profile_id: "final_1080p_portrait", enabled_for_round1_ui: true, label: "直向 9:16" },
        ],
        mismatch_directions: [
          { direction_id: "portrait_source_in_landscape", version: "1", source_orientation: "portrait", target_orientation: "landscape", label: "橫向輸出 + 直向素材" },
          { direction_id: "landscape_source_in_portrait", version: "1", source_orientation: "landscape", target_orientation: "portrait", label: "直向輸出 + 橫向素材" },
        ],
        framing_strategies: [
          { strategy_id: "crop_reframe", version: "1", supported_direction_ids: ["portrait_source_in_landscape", "landscape_source_in_portrait"], label: "裁切／重新構圖" },
          { strategy_id: "background_treatment", version: "1", supported_direction_ids: ["portrait_source_in_landscape", "landscape_source_in_portrait"], label: "背景處理（VID-27）" },
          { strategy_id: "preserve_full_frame", version: "1", supported_direction_ids: ["portrait_source_in_landscape", "landscape_source_in_portrait"], label: "保留完整畫面" },
        ],
      },
    },
    ...overrides,
  };
}

describe("CreativeBriefCheckpoint", () => {
  afterEach(() => cleanup());

  it("shows source evidence and persists the recommendation as a human approval", async () => {
    const saved = vi.spyOn(api, "saveCreativeBrief").mockResolvedValue({ ok: true, creative_brief: { status: "approved" } });
    const refresh = vi.fn(async () => []);
    render(<CreativeBriefCheckpoint detail={detail()} setMessage={vi.fn()} refreshProject={refresh} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    expect(screen.getByText("直向素材：2")).toBeTruthy();
    expect(screen.getByText("橫向素材：1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "採用 AI 建議並核准" }));

    await waitFor(() => expect(saved).toHaveBeenCalledWith(1, expect.objectContaining({
      output: expect.objectContaining({ orientation: "portrait", width: 1080, height: 1920 }),
    }), "recommendation", 4));
    expect(refresh).toHaveBeenCalledWith({ forceFresh: true });
    saved.mockRestore();
  });

  it("keeps the checkpoint explicit when the brief is not approved", () => {
    render(<CreativeBriefCheckpoint detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect(screen.getByText("待人工確認")).toBeTruthy();
    expect(screen.getAllByText("背景處理（VID-27）").length).toBe(2);
  });
});
