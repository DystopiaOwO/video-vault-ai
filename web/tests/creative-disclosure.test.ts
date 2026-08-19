import { describe, expect, it } from "vitest";
import type { ProjectDetail } from "../src/api";
import { resolveDisclosureAction, resolveDisclosureSummary } from "../src/workspaces/creative/disclosure";

function baseDetail(): ProjectDetail {
  return {
    project: { id: 1, name: "Coffee", status: "draft" }, project_revision: 4, clips: [], segments: [], bgm: [], plan: {},
    workflow: { style: "", current: "", stages: [] }, review: {}, script: "", folder: "", can_render: false, render_gate_reason: "",
    color: {} as ProjectDetail["color"], audio: {} as ProjectDetail["audio"], storyboard: {} as ProjectDetail["storyboard"],
    creative_brief: {
      status: "approved",
      approved: { output: { output_contract_id: "landscape_16_9", aspect_ratio: "16:9", width: 1920, height: 1080 }, framing_intent: { portrait_source_in_landscape: { approved_strategy_id: "edge_extend_test" } } },
      options: { output_contracts: [{ output_contract_id: "landscape_16_9", label: "橫向 16:9", aspect_ratio: "16:9", width: 1920, height: 1080 }], framing_strategies: [{ strategy_id: "edge_extend_test", label: "邊緣延展", supported_direction_ids: ["portrait_source_in_landscape"], version: "1" }] },
    } as ProjectDetail["creative_brief"],
    visual_style: { status: "approved", approved: { visual_style_id: "cinematic", composition: "standalone", title_style_id: "cinematic_overlay" }, options: { styles: [{ style_id: "cinematic", label: "Cinematic" }], title_styles: [{ title_style_id: "cinematic_overlay", label: "Cinematic Overlay" }] } },
  } as ProjectDetail;
}

const section = (summary_resolver: string, extra: Record<string, unknown> = {}) => ({ section_id: "test", version: "1", label: "測試", disclosure_level: "advanced" as const, order: 1, summary_resolver, semantic_domain: "test", invalidation_class: "visual_render_only", ...extra });

describe("editor disclosure resolver contract", () => {
  it("uses registry labels and approved composition semantics", () => {
    const detail = baseDetail();
    expect(resolveDisclosureSummary(section("creative_brief.framing@1"), detail).text).toBe("邊緣延展");
    expect(resolveDisclosureSummary(section("visual_style.approved@1"), detail).text).toContain("獨立字卡畫面");
    detail.visual_style = { ...detail.visual_style, approved: { ...detail.visual_style?.approved, composition: "overlay" } };
    expect(resolveDisclosureSummary(section("visual_style.approved@1"), detail).text).toContain("疊在影片上");
  });

  it("resolves actions from metadata and rejects unknown resolvers explicitly", () => {
    const action = resolveDisclosureAction(section("creative_brief.framing@1", { action: { type: "open_semantic_editor@1", target: "creative_brief.framing" } }));
    expect(action).toMatchObject({ available: true, step: "direction", section_id: "test", semantic_editor_target: "creative_brief.framing" });
    expect(resolveDisclosureAction(section("visual_style.grading@1", { section_id: "grading", action: { type: "open_semantic_editor@1", target: "visual_style.grading" } }))).toMatchObject({ available: true, step: "style", semantic_editor_target: "visual_style.grading" });
    expect(resolveDisclosureAction(section("visual_style.title_style@1", { section_id: "title", action: { type: "open_semantic_editor@1", target: "visual_style.title_style" } }))).toMatchObject({ available: true, step: "style", semantic_editor_target: "visual_style.title_style" });
    expect(resolveDisclosureSummary(section("unknown@99"), baseDetail()).available).toBe(false);
    expect(resolveDisclosureAction(section("audio.policy@1", { action: { type: "future_action@1", target: "audio.policy" } })).available).toBe(false);
  });
});
