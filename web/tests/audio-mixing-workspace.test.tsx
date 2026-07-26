import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ColorState, type ProjectDetail } from "../src/api";
import { AudioMixingWorkspace } from "../src/workspaces/audio/AudioMixingWorkspace";

const adjustment = {
  mode: "none",
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

function audio(volume = -18): AudioState {
  return {
    schema_version: 1,
    enabled: true,
    bgm: {
      bgm_id: 1,
      enabled: true,
      volume_db: volume,
      start_seconds: 0,
      loop: true,
      fade_in_seconds: 1,
      fade_out_seconds: 1,
    },
    original_audio: {
      default_role: "lower",
      default_volume_db: 0,
      lower_volume_db: -8,
      fade_in_seconds: .1,
      fade_out_seconds: .1,
    },
    normalization: {
      enabled: true,
      target_lufs: -14,
      true_peak_db: -1,
    },
    segments: {},
  };
}

function legacyAudio(state: "legacy_single" | "legacy_multiple" | "legacy_empty"): AudioState {
  const selected = state === "legacy_single" ? { id: 1, title: "既有配樂" } : undefined;
  return {
    ...audio(),
    source: "legacy",
    settings_exists: false,
    bgm: { ...audio().bgm, bgm_id: selected?.id ?? null, enabled: Boolean(selected) },
    effective_selected_track: selected,
    migration: {
      state,
      warning: state === "legacy_multiple" ? "請明確選擇一首配樂。" : "",
    },
  };
}

function color(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: adjustment,
    applied: adjustment,
    segments: {},
  };
}

function detail(projectId = 1, audioState = audio()): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "needs_review" },
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
    workflow: { style: "test", current: "audio", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "待核准",
    color: color(),
    audio: audioState,
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
  };
}

function renderWorkspace(input = detail()) {
  const setMessage = vi.fn();
  const refreshProject = vi.fn(async () => []);
  const view = render(<AudioMixingWorkspace
    detail={input}
    bgmTracks={[{ id: 1, title: "Diary Theme" }, { id: 2, title: "City Walk" }]}
    setMessage={setMessage}
    refreshProject={refreshProject}
  />);
  return { ...view, setMessage, refreshProject };
}

function bgmVolumeInput(): HTMLInputElement {
  return screen.getAllByLabelText("音量 dB")[0] as HTMLInputElement;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AudioMixingWorkspace", () => {
  it("marks local edits dirty and can restore the server baseline", () => {
    const { setMessage } = renderWorkspace();

    fireEvent.change(bgmVolumeInput(), { target: { value: "-12" } });
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
    expect((screen.getByRole("button", { name: "儲存音訊設定" }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "放棄變更" }));
    expect(bgmVolumeInput().value).toBe("-18");
    expect(screen.queryByText("有未儲存變更")).toBeNull();
    expect(setMessage).toHaveBeenCalledWith("已放棄尚未儲存的音訊設定。");
  });

  it("protects a dirty draft from polling but accepts server updates when clean", async () => {
    const cleanServer = detail(1, audio(-16));
    const laterServer = detail(1, audio(-10));
    const view = renderWorkspace();

    view.rerender(<AudioMixingWorkspace detail={cleanServer} bgmTracks={[]} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(bgmVolumeInput().value).toBe("-16"));

    fireEvent.change(bgmVolumeInput(), { target: { value: "-13" } });
    view.rerender(<AudioMixingWorkspace detail={laterServer} bgmTracks={[]} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(bgmVolumeInput().value).toBe("-13"));
    expect(screen.getByText("有未儲存變更")).toBeTruthy();
  });

  it("saves the full draft, refreshes the project, and clears dirty state", async () => {
    const saved = audio(-12);
    const save = vi.spyOn(api, "audioSettings").mockResolvedValue({ ok: true, state: saved });
    const { refreshProject } = renderWorkspace();

    fireEvent.change(bgmVolumeInput(), { target: { value: "-12" } });
    fireEvent.click(screen.getByRole("button", { name: "儲存音訊設定" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(1, expect.objectContaining({ bgm: expect.objectContaining({ volume_db: -12 }) })));
    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true }));
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("clears a stale preview as soon as settings change", async () => {
    vi.spyOn(api, "audioPreview").mockResolvedValue({ ok: true, url: "/audio-preview.mp4", duration_seconds: 12, timeline_start_seconds: 0, cache_hit: false });
    renderWorkspace();

    fireEvent.click(screen.getByRole("button", { name: "產生 12 秒預覽" }));
    await waitFor(() => expect(document.querySelector("video")?.getAttribute("src")).toBe("/audio-preview.mp4"));

    fireEvent.change(bgmVolumeInput(), { target: { value: "-11" } });
    expect(document.querySelector("video")).toBeNull();
  });

  it("resets all draft state and search when switching projects", async () => {
    const view = renderWorkspace();

    fireEvent.change(bgmVolumeInput(), { target: { value: "-9" } });
    fireEvent.change(screen.getByLabelText("搜尋音訊片段"), { target: { value: "巷弄" } });
    expect(screen.queryByText("抵達車站")).toBeNull();

    view.rerender(<AudioMixingWorkspace detail={detail(2, audio(-20))} bgmTracks={[]} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);
    await waitFor(() => expect(bgmVolumeInput().value).toBe("-20"));
    await waitFor(() => expect((screen.getByLabelText("搜尋音訊片段") as HTMLInputElement).value).toBe(""));
    expect(screen.getByText("抵達車站")).toBeTruthy();
    expect(screen.queryByText("有未儲存變更")).toBeNull();
  });

  it("filters segments and restores a segment override to the project default", () => {
    const customized = audio();
    customized.segments.a = { role: "keep", volume_db: -2 };
    renderWorkspace(detail(1, customized));

    expect(screen.getByText(/片段自訂/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "恢復預設" })[0]);
    expect(screen.getByText("有未儲存變更")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋音訊片段"), { target: { value: "巷弄" } });
    expect(screen.queryByText("抵達車站")).toBeNull();
    expect(screen.getByText("巷弄散步")).toBeTruthy();
  });

  it("blocks saving an invalid BGM-only configuration", () => {
    const invalid = audio();
    invalid.bgm = { ...invalid.bgm, bgm_id: null, enabled: false };
    renderWorkspace(detail(1, invalid));

    fireEvent.change(screen.getByLabelText("原音預設角色"), { target: { value: "bgm_only" } });
    expect(screen.getByText(/目前沒有有效 BGM/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "儲存音訊設定" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each([
    ["legacy_single", "既有配樂", "legacy_single"],
    ["legacy_multiple", "尚未選擇", "legacy_multiple"],
    ["legacy_empty", "尚未選擇", "legacy_empty"],
  ] as const)("shows legacy audio state %s", (state, selected, migration) => {
    renderWorkspace(detail(1, legacyAudio(state)));

    expect(screen.getByText("來源：legacy（舊版）")).toBeTruthy();
    expect(screen.getByText("設定：尚未建立，沿用既有資料")).toBeTruthy();
    expect(screen.getByText(new RegExp(`^遷移：${migration}`))).toBeTruthy();
    expect(screen.getByText(`有效選曲：${selected}`)).toBeTruthy();
  });

  it("gives legacy multiple users an actionable selection repair", () => {
    renderWorkspace(detail(1, legacyAudio("legacy_multiple")));

    expect(screen.getByRole("alert").textContent).toContain("請在下方「音樂」選單明確選擇一首");
    const music = screen.getByLabelText("音樂") as HTMLSelectElement;
    fireEvent.change(music, { target: { value: "2" } });
    expect(music.value).toBe("2");
    expect((screen.getByRole("button", { name: "儲存音訊設定" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows a valid selected track from the effective API state", () => {
    renderWorkspace(detail(1, {
      ...audio(),
      source: "new",
      settings_exists: true,
      effective_selected_track: { id: 2, title: "有效配樂", artist: "測試作者" },
      bgm: { ...audio().bgm, bgm_id: 2 },
    }));

    expect(screen.getByText("來源：new（新版）")).toBeTruthy();
    expect(screen.getByText("設定：已建立")).toBeTruthy();
    expect(screen.getByText("有效選曲：有效配樂 · 測試作者")).toBeTruthy();
  });
});
