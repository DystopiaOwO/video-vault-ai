import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { ProjectMutationCoordinator, createProjectMutationControls } from "../src/projectMutation";
import { CreativeFlowWorkspace } from "../src/workspaces/creative/CreativeFlowWorkspace";

function detail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    project: { id: 1, name: "Coffee", status: "draft" }, project_revision: 4, clips: [], segments: [], bgm: [], plan: {},
    workflow: { style: "", current: "", stages: [] }, review: {}, script: "", folder: "", can_render: false, render_gate_reason: "",
    color: {} as ProjectDetail["color"], audio: {} as ProjectDetail["audio"], storyboard: {} as ProjectDetail["storyboard"],
    creative_brief: {
      status: "needs_confirmation",
      recommendation: { reason: "目前主要是橫向素材，建議使用橫向輸出。", output: { output_contract_id: "landscape_16_9", orientation: "landscape", aspect_ratio: "16:9", width: 1920, height: 1080, render_profile_id: "final_1080p" }, source_orientation_summary: { portrait: 1, landscape: 3, square: 0, unknown: 0 }, framing_intent: { portrait_source_in_landscape: { recommended_strategy: "crop_reframe" } } },
      approved: {},
      options: {
        output_contracts: [
          { output_contract_id: "landscape_16_9", version: "1", orientation: "landscape", aspect_ratio: "16:9", width: 1920, height: 1080, render_profile_id: "final_1080p", label: "橫向 16:9", enabled_for_round1_ui: true },
          { output_contract_id: "portrait_9_16", version: "1", orientation: "portrait", aspect_ratio: "9:16", width: 1080, height: 1920, render_profile_id: "final_1080p_portrait", label: "直向 9:16", enabled_for_round1_ui: true },
        ],
        mismatch_directions: [{ direction_id: "portrait_source_in_landscape", version: "1", source_orientation: "portrait", target_orientation: "landscape", label: "橫向輸出 + 直向素材" }],
        framing_strategies: [{ strategy_id: "crop_reframe", version: "1", supported_direction_ids: ["portrait_source_in_landscape"], label: "裁切填滿" }],
      },
    },
    visual_style: { status: "needs_confirmation", recommendation: { visual_style_id: "diary_natural", label: "Diary Natural" }, options: { styles: [{ style_id: "diary_natural", label: "Diary Natural", enabled_for_round1_ui: true }] } },
    editor_disclosure: { schema_version: "editor-disclosure-v1", registry_version: "editor-disclosure-registry-v1", sections: [{ section_id: "output_direction", version: "1", label: "影片方向", disclosure_level: "primary", order: 10, summary_resolver: "creative_brief.output@1", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only", include_in_final_summary: true, summary_order: 10 }, { section_id: "visual_style", version: "1", label: "視覺風格", disclosure_level: "primary", order: 20, summary_resolver: "visual_style.approved@1", semantic_domain: "visual_style", invalidation_class: "visual_preview_and_render", include_in_final_summary: true, summary_order: 20 }, { section_id: "framing", version: "1", label: "畫面配置", disclosure_level: "advanced", order: 30, summary_resolver: "creative_brief.framing@1", semantic_domain: "creative_brief.visual", invalidation_class: "visual_render_only", action: { type: "open_semantic_editor@1", target: "creative_brief.framing" }, include_in_final_summary: true, summary_order: 30 }, { section_id: "title", version: "1", label: "字卡", disclosure_level: "advanced", order: 50, summary_resolver: "visual_style.title_style@1", semantic_domain: "visual_style.title", invalidation_class: "visual_preview_and_render", action: { type: "open_semantic_editor@1", target: "visual_style.title_style" }, include_in_final_summary: true, summary_order: 50 }, { section_id: "captions", version: "1", label: "字幕", disclosure_level: "advanced", order: 60, summary_resolver: "creative_brief.captions@1", semantic_domain: "caption_policy", invalidation_class: "caption_renderer" }, { section_id: "audio_policy_test", version: "1", label: "音訊偏好（測試）", disclosure_level: "advanced", order: 70, summary_resolver: "audio.policy@1", semantic_domain: "audio_policy", invalidation_class: "audio_render_only", include_in_final_summary: true, summary_order: 70, action: { type: "open_semantic_editor@1", target: "audio.policy" } }] },
    ...overrides,
  } as ProjectDetail;
}

describe("CreativeFlowWorkspace", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("starts with only the direction decision and keeps advanced controls closed", () => {
    render(<CreativeFlowWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect(screen.getByText("先決定影片方向")).toBeTruthy();
    expect(screen.getByRole("button", { name: "採用推薦方向" })).toBeTruthy();
    expect(screen.queryByLabelText("字型")).toBeNull();
    expect(screen.getByText("詳細設定")).toBeTruthy();
    fireEvent.click(screen.getByText("詳細設定"));
    expect(screen.getByText("音訊偏好（測試）")).toBeTruthy();
    expect(screen.getAllByText("使用專案音訊偏好（測試）")).toHaveLength(2);
  });

  it("renders the visual step as preview-first and hides technical controls until advanced is opened", () => {
    const value = detail({
      creative_brief: { ...detail().creative_brief, status: "approved", approved: { output: detail().creative_brief?.recommendation?.output } },
    });
    render(<CreativeFlowWorkspace detail={value} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect(screen.getByText("選一個你喜歡的視覺風格")).toBeTruthy();
    expect(document.querySelector(".creative-flow-advanced")?.hasAttribute("open")).toBe(false);
    expect(screen.getByRole("button", { name: "產生真實畫面預覽" })).toBeTruthy();
    expect(screen.getByText("詳細設定")).toBeTruthy();
    expect(api.previewVisualStyles).toBeDefined();
  });

  it("builds the final summary from approved metadata and keeps one advanced entry", () => {
    const base = detail();
    const value = detail({
      creative_brief: { ...base.creative_brief, status: "approved", approved: { output: base.creative_brief?.recommendation?.output, framing_intent: { portrait_source_in_landscape: { approved_strategy_id: "crop_reframe" } } } },
      visual_style: { ...base.visual_style, status: "approved", approved: { visual_style_id: "diary_natural", composition: "standalone", title_style_id: "diary_natural_overlay" } },
    });
    render(<CreativeFlowWorkspace detail={value} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect(screen.getByText(/Diary Natural · 獨立字卡畫面/)).toBeTruthy();
    expect(screen.getAllByText("詳細設定")).toHaveLength(1);
  });
});
