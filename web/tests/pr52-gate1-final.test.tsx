import { cleanup, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { api, type AudioState, type ColorState, type Job, type ProjectDetail } from "../src/api";
import { WorkspaceCommandPalette } from "../src/components/WorkspaceCommandPalette";
import { ProjectMutationCoordinator } from "../src/projectMutation";
import { installUnsavedNavigationGuard } from "../src/unsavedNavigationGuard";

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

function color(): ColorState {
  return { schema_version: 2, enabled: true, reference: {}, references: [], analysis: {}, suggested: { ...adjustment }, applied: { ...adjustment }, segments: {} };
}

function detail(projectId = 1, canRender = true): ProjectDetail {
  return {
    project: { id: projectId, name: `專案-${projectId}`, status: "needs_review" },
    clips: [{ clip_id: "clip-1", video_id: 1, filename: "clip-1.mp4", status: "perceived", segment_count: 1, duration_seconds: 12, detected_category: "travel", time_of_day: "morning", visual_summary: "車站入口" }],
    segments: [{ segment_id: "segment-1", clip_id: "clip-1", title: "抵達車站", group: "travel", start_seconds: 0, end_seconds: 5, score: .9, suggested_use: "main", scene_role: "arrival", story_position: "opening", manual_order: 1, audio_role: "lower", speed: 1, include: true, user_notes: "" }],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "review", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: canRender,
    render_gate_reason: canRender ? "" : "待核准",
    color: color(),
    audio: audio(),
    storyboard: {
      schema_version: 1,
      exists: true,
      groups: [{ group_id: "group-1", title: "早上", category: "travel", order: 1 }],
      segments: { "segment-1": { group_id: "group-1", order: 1, included: true, locked: false, thumbnail_time_ratio: .5, notes: "" } },
    },
  };
}

function rows(ids: number[]) {
  return ids.map((id) => ({ id, name: `專案-${id}`, status: "needs_review", video_count: 1 }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function setupApp(ids = [1], canRender = true, initialJobs: Job[] = []) {
  vi.spyOn(api, "projects").mockResolvedValue(rows(ids));
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "jobs").mockResolvedValue(initialJobs);
  vi.spyOn(api, "project").mockImplementation(async (projectId) => detail(projectId, canRender));
}

async function openApp(ids = [1], canRender = true, initialJobs: Job[] = []) {
  setupApp(ids, canRender, initialJobs);
  render(<App />);
  await screen.findByRole("heading", { name: `專案-${ids[0]}` });
}

function nativeKeyDown(target: EventTarget, keyCode: number) {
  const event = createEvent.keyDown(target, { key: "Enter", code: "Enter", bubbles: true, cancelable: true });
  Object.defineProperty(event, "keyCode", { configurable: true, value: keyCode });
  fireEvent(target, event);
  return event;
}

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("PR #52 Gate 1 final behavior contracts", () => {
  it.each([
    ["approve", "核准專案"],
    ["reject", "退回修改"],
    ["revise", "依備註重建故事"],
  ] as const)("%s completion never clears a newer notes edit", async (action, buttonName) => {
    const request = deferred<{ ok: boolean }>();
    await openApp();
    vi.spyOn(api, action).mockReturnValue(request.promise);

    const notes = screen.getByLabelText("審核與重建備註") as HTMLTextAreaElement;
    fireEvent.change(notes, { target: { value: "送出前備註" } });
    fireEvent.click(screen.getByRole("button", { name: buttonName }));
    fireEvent.change(notes, { target: { value: "送出後的新備註" } });

    request.resolve({ ok: true });
    await waitFor(() => expect(notes.value).toBe("送出後的新備註"));
  });

  it.each([
    ["approve", "核准專案"],
    ["reject", "退回修改"],
    ["revise", "依備註重建故事"],
  ] as const)("%s completion from the previous project never clears notes after switching", async (action, buttonName) => {
    const request = deferred<{ ok: boolean }>();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await openApp([1, 2]);
    vi.spyOn(api, action).mockReturnValue(request.promise);

    const notes = screen.getByLabelText("審核與重建備註") as HTMLTextAreaElement;
    if (action === "revise") fireEvent.change(notes, { target: { value: "舊專案備註" } });
    fireEvent.click(screen.getByRole("button", { name: buttonName }));
    fireEvent.click(screen.getByRole("button", { name: /專案-2/ }));
    await screen.findByRole("heading", { name: "專案-2" });

    const newNotes = screen.getByLabelText("審核與重建備註") as HTMLTextAreaElement;
    fireEvent.change(newNotes, { target: { value: "新專案備註" } });
    request.resolve({ ok: true });
    await waitFor(() => expect(newNotes.value).toBe("新專案備註"));
  });

  it("native keyCode 229 cannot execute a command palette command", async () => {
    render(<WorkspaceCommandPalette />);
    fireEvent.click(screen.getByRole("button", { name: "開啟命令面板" }));
    await screen.findByRole("dialog", { name: "工作區命令面板" });

    nativeKeyDown(window, 229);
    expect(screen.getByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();

    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull());
  });

  it("native keyCode 229 is blocked by the unsaved project-creation guard", () => {
    document.body.innerHTML = `
      <section id="workspace-review"><textarea placeholder="記錄核准理由、退回項目或重建故事需求">尚未儲存</textarea></section>
      <div class="new-project"><input id="new-project-name" /></div>
    `;
    const cleanupGuard = installUnsavedNavigationGuard(() => false);
    const input = document.querySelector("#new-project-name") as HTMLInputElement;

    try {
      const imeEvent = nativeKeyDown(input, 229);
      expect(imeEvent.defaultPrevented).toBe(true);
      const committedEvent = nativeKeyDown(input, 13);
      expect(committedEvent.defaultPrevented).toBe(true);
    } finally {
      cleanupGuard();
    }
  });

  it.each([
    ["Storyboard", "storyboard"],
    ["Audio", "audio"],
    ["Color", "color"],
    ["Clip Summary", "clip-summary"],
  ] as const)("shared project mutation busy blocks App review while a %s save is pending", async (_label, workspace) => {
    const save = deferred<any>();
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ ok: true });
    const renderJob = vi.spyOn(api, "createRenderJob").mockResolvedValue({ ok: true, created: true });
    await openApp();

    if (workspace === "storyboard") {
      vi.spyOn(api, "updateStoryboard").mockReturnValue(save.promise);
      fireEvent.change(screen.getByLabelText("分鏡備註"), { target: { value: "新的分鏡備註" } });
      fireEvent.click(screen.getByRole("button", { name: "儲存分鏡" }));
      await waitFor(() => expect(api.updateStoryboard).toHaveBeenCalled());
    } else if (workspace === "audio") {
      vi.spyOn(api, "audioSettings").mockReturnValue(save.promise);
      fireEvent.change(screen.getAllByLabelText("音量 dB")[0], { target: { value: "-12" } });
      fireEvent.click(screen.getByRole("button", { name: "儲存音訊設定" }));
      await waitFor(() => expect(api.audioSettings).toHaveBeenCalled());
    } else if (workspace === "color") {
      vi.spyOn(api, "colorSettings").mockReturnValue(save.promise);
      fireEvent.change(screen.getByLabelText("專案曝光"), { target: { value: ".5" } });
      fireEvent.click(screen.getByRole("button", { name: "儲存調色設定" }));
      await waitFor(() => expect(api.colorSettings).toHaveBeenCalled());
    } else {
      vi.spyOn(api, "saveClipSummary").mockReturnValue(save.promise);
      fireEvent.change(screen.getByLabelText("clip-1.mp4 內容感知描述"), { target: { value: "新的內容感知描述" } });
      fireEvent.click(screen.getByRole("button", { name: "儲存描述" }));
      await waitFor(() => expect(api.saveClipSummary).toHaveBeenCalled());
    }

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    expect((screen.getByRole("button", { name: "正式輸出（Render Job）" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "正式輸出（Render Job）" }));
    expect(approve).not.toHaveBeenCalled();
    expect(renderJob).not.toHaveBeenCalled();
    save.resolve({ ok: true });
  });

  it("a stale mutation finally cannot clear a newer project mutation token", () => {
    const coordinator = new ProjectMutationCoordinator();
    const oldToken = coordinator.begin(7, "approve");
    coordinator.switchProject();
    const newToken = coordinator.begin(8, "render");

    coordinator.finish(oldToken!);

    expect(coordinator.current()).toEqual(newToken);
  });

  it("busy file selection clears its value, skips upload, and can upload the same file later", async () => {
    const reviewRequest = deferred<{ ok: boolean }>();
    const upload = vi.spyOn(api, "uploadProject").mockResolvedValue({ ok: true, files: ["clip-1.mp4"] });
    vi.spyOn(api, "approve").mockReturnValue(reviewRequest.promise);
    await openApp();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["video"], "clip-1.mp4", { type: "video/mp4" });
    Object.defineProperty(input, "value", { configurable: true, writable: true, value: "C:\\fakepath\\clip-1.mp4" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(upload).not.toHaveBeenCalled();
    expect(input.value).toBe("");

    reviewRequest.resolve({ ok: true });
    await waitFor(() => expect(screen.getByRole("button", { name: "核准專案" })).not.toBeDisabled());
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
  });

  it("workspace saves and Render cancel share the App mutation busy boundary", async () => {
    const cancel = deferred<{ ok: boolean; message: string }>();
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ ok: true });
    vi.spyOn(api, "cancelRenderJob").mockReturnValue(cancel.promise);
    await openApp([1], true, [{ job_id: "job-1", kind: "正式輸出", status: "running", message: "執行中", percent: 20 }]);

    fireEvent.click(screen.getByRole("button", { name: "停止此 Render" }));
    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    expect(approve).not.toHaveBeenCalled();

    cancel.resolve({ ok: true, message: "已停止" });
  });
});
