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
        title_font_families: [option("Noto Sans CJK TC"), option("Segoe UI")],
        title_weight_values: [option("600"), option("700")],
        title_size_presets: [option("normal", "標準")],
        title_anchors: [option("bottom-left", "左下"), option("top-center", "上中"), option("top-right", "右上"), option("top-left", "左上")],
        compositions: [option("overlay")],
        readability_surfaces: [option("translucent", "半透明"), option("solid", "實色")],
        title_motion_presets: [option("fade"), option("fade_rise"), option("slide_fade")],
      },
    },
  } as ProjectDetail;
}

function heroVariant(styleId: string, suffix = styleId): Record<string, unknown> {
  return { visual_style: { visual_style_id: styleId }, url: `${suffix}.png`, preview_kind: "static", preview_plan_hash: `${suffix}-plan`, preview_variant_id: `${suffix}-variant`, title_role: "chapter_title", representative_frame: { title_role: "chapter_title", selection_reason: "bright_high_luma_representative" } };
}

describe("VisualStylePreviewWorkspace resolved controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps nested readability and motion overrides visible and invalidates preview evidence", async () => {
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({
      ok: true,
      variants: [heroVariant("diary_natural", "diary"), heroVariant("cinematic")],
    });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy();

    const readability = screen.getByLabelText("可讀性") as HTMLSelectElement;
    const motion = screen.getByLabelText("動畫") as HTMLSelectElement;
    expect(readability.value).toBe("translucent");
    expect(motion.value).toBe("fade");
    fireEvent.change(readability, { target: { value: "solid" } });
    fireEvent.change(motion, { target: { value: "fade_rise" } });
    expect(readability.value).toBe("solid");
    expect(motion.value).toBe("fade_rise");
    expect(screen.queryByAltText("diary_natural 主要預覽")).toBeNull();
    expect(screen.queryByText(/真實畫面預覽已完成/)).toBeNull();
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("clears scalar stale variants and requires a new exact preview before approval", async () => {
    const preview = vi.spyOn(api, "previewVisualStyles")
      .mockResolvedValueOnce({
        ok: true,
        variants: [heroVariant("diary_natural", "old"), heroVariant("cinematic", "old-cinematic")],
      })
      .mockResolvedValueOnce({
        ok: true,
        variants: [{ ...heroVariant("diary_natural", "new"), preview_plan_hash: "plan-b", preview_variant_id: "variant-b" }, { ...heroVariant("cinematic", "new-cinematic"), preview_plan_hash: "plan-b-cinematic", preview_variant_id: "variant-b-cinematic" }],
      });
    const approve = vi.spyOn(api, "approveVisualStyle").mockResolvedValue({ ok: true });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy());
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.change(screen.getByLabelText("位置"), { target: { value: "top-center" } });
    expect(screen.queryByAltText("diary_natural 主要預覽")).toBeNull();
    expect(screen.queryByText(/真實畫面預覽已完成/)).toBeNull();
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(1, false, { anchor: "top-center" }));
    await waitFor(() => expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy());
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "核准選定 Visual Style" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(1, "diary_natural", 4, "plan-b", "variant-b", "chapter_title", { anchor: "top-center" }));
  });

  it("clears preview evidence when Creative Brief currentity refreshes", async () => {
    const input = detail();
    const preview = vi.spyOn(api, "previewVisualStyles").mockResolvedValue({
      ok: true,
      variants: [heroVariant("diary_natural", "current"), heroVariant("cinematic", "current-cinematic")],
    });
    const { rerender } = render(<VisualStylePreviewWorkspace detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(preview).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy());

    rerender(<VisualStylePreviewWorkspace detail={{ ...input, project_revision: 5 }} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    await waitFor(() => expect(screen.queryByAltText("diary_natural 主要預覽")).toBeNull());
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

  it("materializes the persisted approved style, overrides, and preview identity after refresh", () => {
    const input = detail();
    input.visual_style = {
      ...input.visual_style,
      status: "approved",
      approved: {
        visual_style_id: "cinematic",
        visual_style_version: "1",
        overrides: { anchor: "top-right" },
        title_style: { role: "chapter_title" },
        approved_preview_variant_id: "persisted-variant",
        approved_preview_plan_hash: "persisted-plan",
      },
    };
    render(<VisualStylePreviewWorkspace detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect((screen.getByRole("combobox", { name: "選擇視覺風格" }) as HTMLSelectElement).value).toBe("cinematic");
    expect((screen.getByLabelText("位置") as HTMLSelectElement).value).toBe("top-right");
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("renders sparse inherited child defaults from API without a child-specific UI branch", async () => {
    const input = detail();
    input.visual_style = {
      ...input.visual_style,
      options: {
        ...input.visual_style?.options,
        styles: [{ style_id: "test_inherited_visual", default_title_style_id: "child_title", enabled_for_round1_ui: true }],
        control_defaults: [{ visual_style_id: "test_inherited_visual", title_style_id: "child_title", is_default_title_style: true, role: "chapter_title", aspect: "landscape", font_family: "Segoe UI", weight: 700, size_preset: "normal", anchor: "top-left", composition: "overlay", readability: { surface: "solid" }, motion: { preset: "fade_rise" } }],
      },
    };
    const preview = vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: true, variants: [] });
    render(<VisualStylePreviewWorkspace detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);
    expect((screen.getByLabelText("字型") as HTMLSelectElement).value).toBe("Segoe UI");
    expect((screen.getByLabelText("字重") as HTMLSelectElement).value).toBe("700");
    expect((screen.getByLabelText("位置") as HTMLSelectElement).value).toBe("top-left");
    expect((screen.getByLabelText("動畫") as HTMLSelectElement).value).toBe("fade_rise");
    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(preview).toHaveBeenCalledWith(1, false, {}));
  });

  it("groups the VID-27 variant matrix into exactly three public primary cards", async () => {
    const input = detail();
    input.visual_style = {
      ...input.visual_style,
      options: {
        ...input.visual_style?.options,
        styles: [
          { style_id: "diary_natural", label: "Diary Natural", composition: "overlay", enabled_for_round1_ui: true },
          { style_id: "clean_minimal", label: "Clean Minimal", composition: "overlay", enabled_for_round1_ui: true },
          { style_id: "cinematic", label: "Cinematic", composition: "overlay", enabled_for_round1_ui: true },
          { style_id: "standalone_card_compare", label: "Standalone Card Compare", composition: "standalone", enabled_for_round1_ui: true },
        ],
      },
    };
    const labels: Record<string, string> = { diary_natural: "Diary Natural", clean_minimal: "Clean Minimal", cinematic: "Cinematic", standalone_card_compare: "Standalone Card Compare" };
    const matrix = Object.keys(labels).flatMap((style) => [
      { visual_style: { visual_style_id: style, label: labels[style] }, url: `${style}-bright.png`, preview_variant_id: `${style}-bright`, preview_plan_hash: `${style}-plan`, title_role: "chapter_title", preview_kind: "static", representative_frame: { title_role: "chapter_title", selection_reason: "bright_high_luma_representative", role_label: "Chapter" } },
      { visual_style: { visual_style_id: style, label: labels[style] }, url: `${style}-dark.png`, preview_variant_id: `${style}-dark`, preview_plan_hash: `${style}-dark-plan`, title_role: "location_title", preview_kind: "static", representative_frame: { title_role: "location_title", selection_reason: "dark_complex_low_luma_representative", role_label: "Location" } },
      { visual_style: { visual_style_id: style, label: labels[style] }, url: `${style}-animated.mp4`, preview_variant_id: `${style}-animated`, preview_plan_hash: `${style}-animated-plan`, title_role: "chapter_title", preview_kind: "animated", representative_frame: { title_role: "chapter_title", selection_reason: "bright_high_luma_representative", role_label: "Chapter" } },
    ]);
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: true, variants: matrix });
    render(<VisualStylePreviewWorkspace detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} compact />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(document.querySelectorAll('[aria-label="主要視覺風格選擇"] > article')).toHaveLength(3));
    expect(document.querySelector('[aria-label="主要視覺風格選擇"] img[alt*="Standalone Card Compare"]')).toBeNull();
    expect(screen.queryByLabelText("字型")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看更多預覽" }));
    expect(await screen.findByText("Standalone Card Compare · Location")).toBeTruthy();
    expect(screen.getAllByText(/Diary Natural ·/).length).toBeGreaterThan(1);
  });

  it("shows shared running feedback and prevents duplicate preview requests", async () => {
    let resolvePreview: (value: { ok: true; variants: Array<Record<string, unknown>> }) => void = () => undefined;
    const preview = vi.spyOn(api, "previewVisualStyles").mockImplementation(() => new Promise((resolve) => { resolvePreview = resolve as typeof resolvePreview; }));
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    const cta = screen.getByRole("button", { name: "產生真實畫面預覽" });
    fireEvent.click(cta);
    expect(screen.getByRole("status").textContent).toContain("正在以真實素材產生預覽");
    expect((cta as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(cta);
    expect(preview).toHaveBeenCalledTimes(1);

    resolvePreview({ ok: true, variants: [heroVariant("diary_natural", "coffee"), heroVariant("cinematic", "coffee-cinematic")] });
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("真實畫面預覽已完成"));
  });

  it("shows a local retryable error when the product endpoint rejects preview", async () => {
    const preview = vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: false, error: "source_changed" });
    const setMessage = vi.fn();
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={setMessage} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("真實畫面預覽失敗");
    expect(alert.textContent).toContain("source_changed");
    expect(setMessage).toHaveBeenCalledWith(expect.stringContaining("source_changed"));
    expect((alert.querySelector("button") as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(alert.querySelector("button") as HTMLButtonElement);
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(2));
  });

  it("fails visibly and clears approval evidence when the endpoint returns no variants", async () => {
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: true, variants: [] });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("沒有回傳任何可供核准的真實預覽畫面");
    expect(screen.queryByAltText("diary_natural 主要預覽")).toBeNull();
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("fails closed when a public primary registry style is missing from the response", async () => {
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: true, variants: [heroVariant("diary_natural", "partial-diary")] });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("部分視覺風格預覽沒有成功產生");
    expect(alert.textContent).toContain("cinematic");
    expect(screen.queryByText(/真實畫面預覽已完成/)).toBeNull();
    expect(screen.queryByAltText("diary_natural 主要預覽")).toBeNull();
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("uses registry-derived completeness for a future public sibling style", async () => {
    const input = detail();
    input.visual_style = {
      ...input.visual_style,
      options: {
        ...input.visual_style?.options,
        styles: [
          ...(input.visual_style?.options?.styles || []),
          { style_id: "test_future_visual_style", label: "Future Visual", composition: "overlay", enabled_for_round1_ui: true },
        ],
      },
    };
    vi.spyOn(api, "previewVisualStyles").mockResolvedValue({ ok: true, variants: [heroVariant("diary_natural"), heroVariant("cinematic"), heroVariant("test_future_visual_style", "future")] });
    render(<VisualStylePreviewWorkspace detail={input} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(screen.getByText(/真實畫面預覽已完成/)).toBeTruthy());
    expect(screen.getByAltText("Future Visual 主要預覽")).toBeTruthy();
    expect((screen.getByRole("button", { name: "核准選定 Visual Style" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("loads extended evidence lazily and keeps primary heroes visible", async () => {
    const primary = [heroVariant("diary_natural", "primary-diary"), heroVariant("cinematic", "primary-cinematic")];
    const extended = [
      ...primary,
      { ...heroVariant("diary_natural", "location"), title_role: "location_title", representative_frame: { title_role: "location_title", selection_reason: "dark_complex_low_luma_representative" } },
    ];
    const preview = vi.spyOn(api, "previewVisualStyles")
      .mockResolvedValueOnce({ ok: true, variants: primary })
      .mockResolvedValueOnce({ ok: true, variants: extended });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "查看更多預覽" }));
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(1, false, {}, "extended"));
    expect((await screen.findAllByAltText("diary_natural 補充預覽")).length).toBe(2);
    expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy();
  });

  it("keeps primary evidence when the lazy extended request fails", async () => {
    const preview = vi.spyOn(api, "previewVisualStyles")
      .mockResolvedValueOnce({ ok: true, variants: [heroVariant("diary_natural"), heroVariant("cinematic")] })
      .mockResolvedValueOnce({ ok: false, error: "preview_extended_failed" });
    render(<VisualStylePreviewWorkspace detail={detail()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={createProjectMutationControls(new ProjectMutationCoordinator())} />);

    fireEvent.click(screen.getByRole("button", { name: "產生真實畫面預覽" }));
    await waitFor(() => expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "查看更多預覽" }));
    expect((await screen.findByRole("alert")).textContent).toContain("preview_extended_failed");
    expect(screen.getByAltText("diary_natural 主要預覽")).toBeTruthy();
    expect(preview).toHaveBeenLastCalledWith(1, false, {}, "extended");
  });
});
