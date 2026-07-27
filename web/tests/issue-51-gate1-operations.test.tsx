import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ColorAdjustment, type ProjectDetail } from "../src/api";
import { RenderJobPanel } from "../src/components/render/RenderJobPanel";
import { App } from "../src/main";

const adjustment: ColorAdjustment = {
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

function detail(projectId: number): ProjectDetail {
  return {
    project: { id: projectId, name: `專案-${projectId}`, status: "needs_review" },
    clips: [],
    segments: [],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "review", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: true,
    render_gate_reason: "",
    color: {
      schema_version: 2,
      enabled: true,
      reference: {},
      references: [],
      analysis: {},
      suggested: { ...adjustment },
      applied: { ...adjustment },
      segments: {},
    },
    audio: audio(),
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
  };
}

function projectRows(ids: number[]) {
  return ids.map((id) => ({ id, name: `專案-${id}`, status: "needs_review", video_count: 0 }));
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

function setup(ids = [1]) {
  vi.spyOn(api, "projects").mockResolvedValue(projectRows(ids));
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "project").mockImplementation(async (projectId) => detail(projectId));
}

async function openProject(projectId = 1) {
  render(<App />);
  await screen.findByRole("heading", { name: `專案-${projectId}` });
}

function projectNameInput() {
  return screen.getByLabelText("建立專案") as HTMLInputElement;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Issue #51 Gate 1 專案操作行為", () => {
  it("建立專案 A 成功後仍可建立專案 B", async () => {
    let listed = [1];
    setup();
    vi.mocked(api.projects).mockImplementation(async () => projectRows(listed));
    const create = vi.spyOn(api, "createProject")
      .mockImplementationOnce(async () => { listed = [1, 2]; return { ok: true, id: 2 }; })
      .mockImplementationOnce(async () => { listed = [1, 2, 3]; return { ok: true, id: 3 }; });
    await openProject();

    fireEvent.change(projectNameInput(), { target: { value: "旅程 A" } });
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    await screen.findByRole("heading", { name: "專案-2" });

    fireEvent.change(projectNameInput(), { target: { value: "旅程 B" } });
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    await screen.findByRole("heading", { name: "專案-3" });

    expect(create).toHaveBeenCalledTimes(2);
  });

  it("第一次建立失敗後可以重試並成功", async () => {
    let listed = [1];
    setup();
    vi.mocked(api.projects).mockImplementation(async () => projectRows(listed));
    const create = vi.spyOn(api, "createProject")
      .mockRejectedValueOnce(new Error("服務暫時忙碌"))
      .mockImplementationOnce(async () => { listed = [1, 2]; return { ok: true, id: 2 }; });
    await openProject();

    fireEvent.change(projectNameInput(), { target: { value: "可重試專案" } });
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    await screen.findByText("專案建立失敗：服務暫時忙碌");

    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    await screen.findByRole("heading", { name: "專案-2" });
    expect(create).toHaveBeenCalledTimes(2);
  });

  it("建立期間切換專案，請求完成後仍可建立新專案", async () => {
    let listed = [1, 2];
    setup(listed);
    vi.mocked(api.projects).mockImplementation(async () => projectRows(listed));
    const request = deferred<{ ok: boolean; id: number }>();
    const create = vi.spyOn(api, "createProject").mockReturnValue(request.promise);
    await openProject();

    fireEvent.change(projectNameInput(), { target: { value: "延遲建立" } });
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    fireEvent.click(screen.getByRole("button", { name: /專案-2/ }));
    await screen.findByRole("heading", { name: "專案-2" });

    listed = [1, 2, 3, 4];
    request.resolve({ ok: true, id: 3 });
    await waitFor(() => expect(screen.getByRole("heading", { name: "專案-2" })).toBeTruthy());

    fireEvent.change(projectNameInput(), { target: { value: "切換後建立" } });
    const second = deferred<{ ok: boolean; id: number }>();
    create.mockReturnValueOnce(second.promise);
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    second.resolve({ ok: true, id: 4 });
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
  });

  it("Enter 與按鈕同時觸發時只送出一次建立請求", async () => {
    setup();
    const request = deferred<{ ok: boolean; id: number }>();
    const create = vi.spyOn(api, "createProject").mockReturnValue(request.promise);
    await openProject();

    fireEvent.change(projectNameInput(), { target: { value: "去重測試" } });
    fireEvent.keyDown(projectNameInput(), { key: "Enter", code: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "建立中…" }));
    expect(create).toHaveBeenCalledTimes(1);
    request.resolve({ ok: true, id: 2 });
  });

  it("approve 成功但 detail refresh 失敗時保留成功並明確標示畫面更新失敗", async () => {
    setup();
    const project = vi.mocked(api.project);
    project.mockReset().mockResolvedValueOnce(detail(1)).mockRejectedValue(new Error("GET failed"));
    vi.spyOn(api, "approve").mockResolvedValue({ ok: true });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    await screen.findByText("專案已核准，但畫面更新失敗：GET failed");
  });

  it("reject 成功但 detail refresh 失敗時不顯示退回操作失敗", async () => {
    setup();
    const project = vi.mocked(api.project);
    project.mockReset().mockResolvedValueOnce(detail(1)).mockRejectedValue(new Error("GET failed"));
    vi.spyOn(api, "reject").mockResolvedValue({ ok: true });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "退回修改" }));
    await screen.findByText("專案已退回修改，但畫面更新失敗：GET failed");
  });

  it("upload 成功但 refresh 失敗時保留匯入成功資訊", async () => {
    setup();
    const project = vi.mocked(api.project);
    project.mockReset().mockResolvedValueOnce(detail(1)).mockRejectedValue(new Error("GET failed"));
    vi.spyOn(api, "uploadProject").mockResolvedValue({ ok: true, files: ["clip.mp4"] });
    await openProject();

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(["video"], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    await screen.findByText("已匯入 1 支素材，但畫面更新失敗：GET failed");
  });

  it("正式 Render Job 成功但 jobs refresh 失敗時保留排程成功資訊", async () => {
    setup();
    const project = vi.mocked(api.project);
    project.mockReset().mockResolvedValueOnce(detail(1)).mockRejectedValue(new Error("GET failed"));
    vi.spyOn(api, "createRenderJob").mockResolvedValue({ ok: true, created: true });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "正式輸出（Render Job）" }));
    await screen.findByText("正式輸出已排入佇列，但工作狀態更新失敗：GET failed");
  });

  it.each([
    ["HyperFrames", "產生初剪專案", "hyperframesJob", "HyperFrames 工作已建立"],
    ["OpenCut", "OpenCut 素材包", "opencutJob", "OpenCut 工作已建立"],
  ] as const)("%s 成功但 jobs refresh 失敗時不改報為工作啟動失敗", async (_label, button, method, success) => {
    setup();
    const project = vi.mocked(api.project);
    project.mockReset().mockResolvedValueOnce(detail(1)).mockRejectedValue(new Error("GET failed"));
    vi.spyOn(api, method).mockResolvedValue({ ok: true, message: success });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: button }));
    await screen.findByText(`${success}，但工作狀態更新失敗：GET failed`);
  });

  it("mutation 本身失敗時顯示真正的操作錯誤", async () => {
    setup();
    vi.spyOn(api, "approve").mockRejectedValue(new Error("approve failed"));
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    await screen.findByText("審核操作失敗：approve failed");
  });

  it("缺少 storyboard 時顯示可操作的核准錯誤與建立分鏡引導", async () => {
    setup();
    vi.spyOn(api, "approve").mockResolvedValue({
      ok: false,
      code: "storyboard_required",
      error: "尚未建立 storyboard.json",
    });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));

    await screen.findByText("核准失敗：尚未建立 storyboard.json 請先到「分鏡審核」執行「建立分鏡」，完成後再核准。");
    expect(screen.queryByText("專案已核准")).toBeNull();
  });

  it("切換專案後，舊 approve 的成功或 refresh 錯誤不污染新專案", async () => {
    setup([1, 2]);
    const approveRequest = deferred<{ ok: boolean }>();
    const project = vi.mocked(api.project);
    project.mockImplementation(async (projectId) => detail(projectId));
    vi.spyOn(api, "approve").mockReturnValue(approveRequest.promise);
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    fireEvent.click(screen.getByRole("button", { name: /專案-2/ }));
    await screen.findByRole("heading", { name: "專案-2" });
    approveRequest.resolve({ ok: true });
    await waitFor(() => expect(screen.getByRole("heading", { name: "專案-2" })).toBeTruthy());
    expect(screen.queryByText(/已核准|審核操作失敗|畫面更新失敗/)).toBeNull();
  });

  it("同一專案 approve 進行中時不允許 reject 競跑", async () => {
    setup();
    const approveRequest = deferred<{ ok: boolean }>();
    const approve = vi.spyOn(api, "approve").mockReturnValue(approveRequest.promise);
    const reject = vi.spyOn(api, "reject").mockResolvedValue({ ok: true });
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    fireEvent.click(screen.getByRole("button", { name: "退回修改" }));
    expect(approve).toHaveBeenCalledTimes(1);
    expect(reject).not.toHaveBeenCalled();
    approveRequest.resolve({ ok: true });
  });

  it("正式輸出進行中不能重複建立同類 Render Job", async () => {
    setup();
    const request = deferred<{ ok: boolean; created: boolean }>();
    const create = vi.spyOn(api, "createRenderJob").mockReturnValue(request.promise);
    await openProject();

    fireEvent.click(screen.getByRole("button", { name: "正式輸出（Render Job）" }));
    fireEvent.click(screen.getByRole("button", { name: "正在建立正式輸出…" }));
    expect(create).toHaveBeenCalledTimes(1);
    request.resolve({ ok: true, created: true });
  });

  it("舊 cancel finally 不得解除較新的同專案工作 cancelling", async () => {
    const first = deferred<{ ok: boolean; message: string }>();
    const second = deferred<{ ok: boolean; message: string }>();
    vi.spyOn(api, "cancelRenderJob").mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const refreshProject = vi.fn().mockResolvedValue([]);
    render(<RenderJobPanel
      jobs={[
        { job_id: "first", kind: "正式輸出", status: "running", message: "執行中", percent: 10 },
        { job_id: "second", kind: "正式輸出", status: "running", message: "執行中", percent: 20 },
      ]}
      projectId={1}
      setMessage={vi.fn()}
      refreshProject={refreshProject}
    />);

    let stopButtons = screen.getAllByRole("button", { name: "停止此 Render" });
    fireEvent.click(stopButtons[0]);
    stopButtons = screen.getAllByRole("button", { name: "停止此 Render" });
    fireEvent.click(stopButtons[0]);
    expect(screen.getAllByRole("button", { name: "停止中..." })).toHaveLength(2);

    first.resolve({ ok: true, message: "第一個已停止" });
    await waitFor(() => expect(screen.getAllByRole("button", { name: "停止中..." })).toHaveLength(1));
    expect(refreshProject).toHaveBeenCalled();
    second.resolve({ ok: true, message: "第二個已停止" });
  });
});
