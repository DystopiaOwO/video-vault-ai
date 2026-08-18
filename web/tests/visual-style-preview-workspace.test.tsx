import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { ProjectMutationCoordinator, createProjectMutationControls } from "../src/projectMutation";
import { VisualStylePreviewWorkspace } from "../src/workspaces/creative/VisualStylePreviewWorkspace";

const option = (id: string, label = id) => ({ id, label, enabled: true, capability: {} });

function detail(): ProjectDetail {
  return {
    project: { id: 1, name: "Coffee", status: "draft" },
    project_revision: 4,
    clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "", current: "", stages: [] },
    review: {}, script: "", folder: "", can_render: false, render_gate_reason: "",
    color: {} as ProjectDetail["color"], audio: {} as ProjectDetail["audio"], storyboard: {} as ProjectDetail["storyboard"],
    creative_brief: { status: "approved", approved: { output: { orientation: "landscape" } } } as ProjectDetail["creative_brief"],
    visual_style: {
      status: "approved",
      options: {
        styles: [
          { style_id: "diary_natural", default_title_style_id: "diary_natural_overlay", composition: "overlay", enabled_for_round1_ui: true },
          { style_id: "cinematic", default_title_style_id: "cinematic_overlay", composition: "overlay", enabled_for_round1_ui: true },
        ],
        control_defaults: [
          { visual_style_id: "diary_natural", title_style_id: "diary_natural_overlay", is_default_title_style: true, role: "chapter_title", aspect: "landscape", font_family: "Noto Sans CJK TC", weight: 600, size_preset: "normal", anchor: "bottom-left", composition: "overlay", readability: { surface: "translucent" }, motion: { preset: "fade" } },
          { visual_style_id: "cinematic", title_style_id: "cinematic_overlay", is_default_title_style: true, role: "chapter_title", aspect: "landscape", font_family: "Segoe UI", weight: 700, size_preset: "normal", anchor: "top-right", composition: "overlay", readability: { surface: "solid" }, motion: { preset: "fade_rise" } },
        ],
        title_font_families: [option("Noto Sans CJK TC")],
        title_weight_values: [option("600")],
        title_size_presets: [option("normal", "標準")],
        title_anchors: [option("bottom-left", "左下"), option("top-center", "上中"), option("top-right", "右上")],
        compositions: [option("overlay")],
        readability_surfaces: [option("translucent", "半透明"), option("solid", "實色")],
        title_motion_presets: [option("fade"), option("fade_rise"), option("slide_fade")],
      },
    },
  } as ProjectDetail;
}

describe("VisualStylePreviewWorkspace resolved controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps nested readability and motion overrides visible and invalidates preview evidence", async () => {
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({
      ok: true,
      variants: [{ visual_style: { visual_style_id: "diary_natural" }, preview_plan_hash: "plan", preview_variant_id: "variant", title_role: "chapter_title" }],
    });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false));

    const readability = screen.getByLabelText("可讀性") as HTMLSelectElement;
    const motion = screen.getByLabelText("動畫") as HTMLSelectElement;
    expect(readability.value).toBe("translucent");
    expect(motion.value).toBe("fade");
    fireEvent.change(readability, { target: { value: "solid" } });
    fireEvent.change(motion, { target: { value: "fade_rise" } });
    expect(readability.value).toBe("solid");
    expect(motion.value).toBe("fade_rise");
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("uses backend defaults on style switch while preserving supported explicit overrides", () => {
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    const selects = screen.getAllByRole("combobox");
    const styleSelect = selects[0] as HTMLSelectElement;
    const anchor = screen.getByLabelText("位置") as HTMLSelectElement;
    expect(anchor.value).toBe("bottom-left");
    fireEvent.change(anchor, { target: { value: "top-center" } });
    fireEvent.change(styleSelect, { target: { value: "cinematic" } });
    expect(anchor.value).toBe("top-center");
    fireEvent.change(anchor, { target: { value: "top-right" } });
    expect(anchor.value).toBe("top-right");
  });
});
