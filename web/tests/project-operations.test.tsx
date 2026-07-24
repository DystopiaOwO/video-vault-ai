import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { App } from "../src/App";

function detail(projectId = 7): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "approved" },
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
  };
}

function setup(projectIds = [7]) {
  vi.spyOn(api, "projects").mockResolvedValue(projectIds.map((id) => ({ id, name: `project-${id}`, status: "approved" })));
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "project").mockResolvedValue(detail());
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("project mutation lifecycle", () => {
  it.each([
    ["approve", "核准專案", "專案已核准"],
    ["reject", "退回修改", "專案已退回修改"],
  ] as const)("keeps the %s success when refresh fails", async (action, button, successMessage) => {
    setup();
    let projectCalls = 0;
    vi.mocked(api.project).mockImplementation(async () => {
      projectCalls += 1;
      if (projectCalls > 1) throw new Error("GET failed");
      return detail();
    });
    const mutation = action === "approve" ? vi.spyOn(api, "approve") : vi.spyOn(api, "reject");
    mutation.mockResolvedValue({ ok: true });
    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });

    fireEvent.click(screen.getByRole("button", { name: button }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain(`${successMessage}，但畫面更新失敗：GET failed`));
    expect(mutation).toHaveBeenCalledOnce();
  });

  it("does not allow review mutations to race in the same project", async () => {
    setup();
    let resolveApprove!: (value: { ok: boolean }) => void;
    vi.spyOn(api, "approve").mockReturnValue(new Promise((resolve) => { resolveApprove = resolve; }));
    const reject = vi.spyOn(api, "reject");
    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    expect((screen.getByRole("button", { name: "退回修改" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "退回修改" }));
    expect(reject).not.toHaveBeenCalled();

    resolveApprove({ ok: true });
    await waitFor(() => expect((screen.getByRole("button", { name: "退回修改" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("reports upload success separately when the project refresh fails", async () => {
    setup();
    let projectCalls = 0;
    vi.mocked(api.project).mockImplementation(async () => {
      projectCalls += 1;
      if (projectCalls > 1) throw new Error("GET failed");
      return detail();
    });
    vi.spyOn(api, "uploadProject").mockResolvedValue({ ok: true, files: ["clip.mp4"] });
    const { container } = render(<App />);
    await screen.findByRole("heading", { name: "project-7" });

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["clip"], "clip.mp4", { type: "video/mp4" })] } });
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("已匯入 1 支素材，但畫面更新失敗：GET failed"));
  });

  it("reports a submitted HyperFrames job separately when refresh fails", async () => {
    setup();
    let projectCalls = 0;
    vi.mocked(api.project).mockImplementation(async () => {
      projectCalls += 1;
      if (projectCalls > 1) throw new Error("GET failed");
      return detail();
    });
    vi.spyOn(api, "hyperframesJob").mockResolvedValue({ ok: true, message: "初剪工作已排入佇列" });
    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });

    fireEvent.click(screen.getByRole("button", { name: "產生初剪專案" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("初剪工作已排入佇列，但工作狀態更新失敗：GET failed"));
  });

  it("does not let an old mutation message overwrite a newly selected project", async () => {
    setup([7, 8]);
    let resolveApprove!: (value: { ok: boolean }) => void;
    vi.spyOn(api, "approve").mockReturnValue(new Promise((resolve) => { resolveApprove = resolve; }));
    vi.mocked(api.project).mockImplementation(async (projectId) => detail(projectId));
    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });

    fireEvent.click(screen.getByRole("button", { name: "核准專案" }));
    fireEvent.click(screen.getByRole("button", { name: /project-8/ }));
    await screen.findByRole("heading", { name: "project-8" });
    resolveApprove({ ok: true });
    await waitFor(() => expect(screen.queryByText(/專案已核准|畫面更新失敗/)).toBeNull());
  });
});
