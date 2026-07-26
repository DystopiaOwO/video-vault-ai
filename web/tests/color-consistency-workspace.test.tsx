import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ColorAdjustment, type ColorState, type ProjectDetail } from "../src/api";
import { ColorConsistencyWorkspace } from "../src/workspaces/color/ColorConsistencyWorkspace";

function adjustment(exposure = 0): ColorAdjustment {
  return {
    mode: "manual",
    lut_path: "",
    lut_kind: "",
    exposure,
    temperature: 0,
    tint: 0,
    contrast: 1,
    saturation: 1,
    gamma: 1,
    highlights: 0,
    shadows: 0,
  };
}

function color(exposure = 0): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {
      id: "segment:a:0.5",
      type: "segment",
      clip_id: "clip-a",
      video_id: 1,
      segment_id: "a",
      time_seconds: 2.5,
      label: "抵達車站",
      score: .9,
      frame_url: "/reference.jpg",
      metadata: {},
    },
    references: [
      {
        id: "segment:a:0.5",
        type: "segment",
        clip_id: "clip-a",
        video_id: 1,
        segment_id: "a",
        time_seconds: 2.5,
        label: "抵達車站",
        score: .9,
        frame_url: "/reference.jpg",
        metadata: {},
      },
      {
        id: "segment:b:0.5",
        type: "segment",
        clip_id: "clip-b",
        video_id: 2,
        segment_id: "b",
        time_seconds: 7.5,
        label: "巷弄散步",
        score: .8,
        frame_url: "/reference-b.jpg",
        metadata: {},
      },
    ],
    analysis: {
      basis_text: "以抵達車站作為亮度與白平衡基準",
      confidence: "high",
      warnings: [],
      luma: { average: 48, highlight_ratio: .04 },
    },
    suggested: adjustment(.2),
    applied: adjustment(exposure),
    segments: {},
  };
}

function audio(): AudioState {
  return {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: {},
  };
}

function detail(projectId = 1, colorState = color()): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "needs_review" },
    project_revision: 7,
    clips: [],
    segments: [
      {
        segment_id: "a",
        clip_id: "clip-a",
        title: "抵達車站",
        group: "travel",
        start_seconds: 0,
        end_seconds: 5,
        score: .9,
        suggested_use: "main",
        scene_role: "arrival",
        story_position: "opening",
        manual_order: 1,
        audio_role: "lower",
        speed: 1,
        include: true,
        user_notes: "",
      },
      {
        segment_id: "b",
        clip_id: "clip-b",
        title: "巷弄散步",
        group: "travel",
        start_seconds: 5,
        end_seconds: 10,
        score: .8,
        suggested_use: "transition",
        scene_role: "walk",
        story_position: "middle",
        manual_order: 2,
        audio_role: "lower",
        speed: 1,
        include: true,
        user_notes: "",
      },
    ],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "color", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "待核准",
    color: colorState,
    audio: audio(),
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
  };
}

function renderWorkspace(input = detail()) {
  const setMessage = vi.fn();
  const refreshProject = vi.fn(async () => []);
  const view = render(<ColorConsistencyWorkspace detail={input} setMessage={setMessage} refreshProject={refreshProject} />);
  return { ...view, setMessage, refreshProject };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ColorConsistencyWorkspace", () => {
  it("marks project adjustments dirty and restores the server baseline", () => {
    const { setMessage } = renderWorkspace();

    fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".5" } });
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
    expect((screen.getByRole("button", { name: "儲存調色設定" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "分析核心畫面" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "產生 Before / After 預覽" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "放棄變更" }));
    expect((screen.getByLabelText("專案曝光") as HTMLInputElement).value).toBe("0");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
    expect(setMessage).toHaveBeenCalledWith("已放棄尚未儲存的調色設定。");
  });

  it("protects a dirty draft from polling and accepts clean server updates", async () => {
    const view = renderWorkspace();

    view.rerender(<ColorConsistencyWorkspace detail={detail(1, color(.3))} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(Number((screen.getByLabelText("專案曝光") as HTMLInputElement).value)).toBe(.3));

    fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".8" } });
    view.rerender(<ColorConsistencyWorkspace detail={detail(1, color(1.2))} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(Number((screen.getByLabelText("專案曝光") as HTMLInputElement).value)).toBe(.8));
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
  });

  it("saves a normalized color patch and clears dirty state", async () => {
    const saved = color(.4);
    const save = vi.spyOn(api, "colorSettings").mockResolvedValue({ ok: true, state: saved });
    const { refreshProject } = renderWorkspace();

    fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".4" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存調色設定" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({ applied: expect.objectContaining({ exposure: .4 }) }), 7));
    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true }));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("analyzes and changes the reference only while the workspace is clean", async () => {
    const analyzed = color(.1);
    analyzed.analysis.basis_text = "重新分析完成";
    const analyze = vi.spyOn(api, "colorAnalyze").mockResolvedValue({ ok: true, state: analyzed });
    const referenceState = color(.15);
    referenceState.reference = referenceState.references[1]!;
    const reference = vi.spyOn(api, "colorReference").mockResolvedValue({ ok: true, state: referenceState });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "分析核心畫面" }));
    await waitFor(() => expect(analyze).toHaveBeenCalledWith(1, false, 7));
    await waitFor(() => expect(screen.getByText("重新分析完成")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("色彩基準"), { target: { value: "segment:b:0.5" } });
    await waitFor(() => expect(reference).toHaveBeenCalledWith(1, "segment:b:0.5", 7));
  });

  it("renders saved previews and clears them after any local edit", async () => {
    vi.spyOn(api, "colorPreviewDirect").mockResolvedValue({
      ok: true,
      previews: [{ video_id: 1, segment_id: "a", before_url: "/before.mp4", after_url: "/after.mp4", cache_hit: false }],
    });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "產生 Before / After 預覽" }));
    await waitFor(() => expect(document.querySelectorAll("video")).toHaveLength(2));

    fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".6" } });
    expect(document.querySelectorAll("video")).toHaveLength(0);
  });

  it("searches segments and removes a segment override", () => {
    const customized = color();
    customized.segments.a = { enabled: true, locked: false, excluded: false, applied: adjustment(.7), confidence: .9, warnings: [] };
    renderWorkspace(detail(1, customized));

    expect(screen.getByText(/片段自訂/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "恢復專案預設" })[0]);
    expect(screen.getByText("有未儲存變更")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋調色片段"), { target: { value: "巷弄" } });
    expect(screen.queryByText("抵達車站")).toBeNull();
    expect(screen.getByText("巷弄散步")).toBeTruthy();
  });

  it("resets draft and search on project switch", async () => {
    const view = renderWorkspace();

    fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".9" } });
    fireEvent.change(screen.getByLabelText("搜尋調色片段"), { target: { value: "巷弄" } });

    view.rerender(<ColorConsistencyWorkspace detail={detail(2, color(-.2))} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(Number((screen.getByLabelText("專案曝光") as HTMLInputElement).value)).toBe(-.2));
    await waitFor(() => expect((screen.getByLabelText("搜尋調色片段") as HTMLInputElement).value).toBe(""));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("requires a LUT path before saving custom DJI LUT mode", () => {
    renderWorkspace();

    fireEvent.change(screen.getByLabelText("技術 LUT 模式"), { target: { value: "dji_lut" } });
    expect(screen.getByText(/需要有效的 .cube 路徑/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "儲存調色設定" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("LUT 路徑"), { target: { value: "C:/LUTs/DJI.cube" } });
    expect((screen.getByRole("button", { name: "儲存調色設定" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
