import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, Job, ProjectDetail } from "../src/api";
import { App } from "../src/main";

function detail(projectId: number): ProjectDetail {
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function setupProjects(projectIds = [7]) {
  vi.spyOn(api, "projects").mockResolvedValue(projectIds.map((id) => ({ id, name: `project-${id}`, status: "approved" })));
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "project").mockImplementation(async (projectId) => detail(projectId));
}

function formalButton() {
  return screen.getByRole("button", { name: "正式輸出（Render Job）" }) as HTMLButtonElement;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("正式輸出 submitting lifecycle", () => {
  it.each([
    ["succeeded", "已完成"],
    ["failed", "失敗"],
  ] as const)("clears submitting when fresh jobs return %s", async (status, label) => {
    setupProjects();
    let jobsCalls = 0;
    const jobs = vi.spyOn(api, "jobs").mockImplementation(async () => {
      jobsCalls += 1;
      const job: Job = { job_id: "job-1", kind: "正式輸出", status, message: label, percent: 100 };
      return jobsCalls === 1 ? [] : [job];
    });
    vi.spyOn(api, "createRenderJob").mockResolvedValue({ ok: true, created: true });

    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });
    fireEvent.click(formalButton());

    await vi.waitFor(() => expect(jobs).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(formalButton().disabled).toBe(false));
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });

  it("clears submitting when the fresh jobs response is empty", async () => {
    setupProjects();
    let jobsCalls = 0;
    const jobs = vi.spyOn(api, "jobs").mockImplementation(async () => {
      jobsCalls += 1;
      return jobsCalls === 1 ? [] : [];
    });
    vi.spyOn(api, "createRenderJob").mockResolvedValue({ ok: true, created: true });

    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });
    fireEvent.click(formalButton());

    await vi.waitFor(() => expect(jobs).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(formalButton().disabled).toBe(false));
    expect(screen.queryByRole("button", { name: "正在建立正式輸出…" })).toBeNull();
  });

  it("keeps the mutation target when switching projects during POST", async () => {
    setupProjects([7, 8]);
    const create = deferred<{ ok: boolean; created: boolean }>();
    const project7FreshJobs = deferred<Job[]>();
    let project7JobsCalls = 0;
    const jobs = vi.spyOn(api, "jobs").mockImplementation(async (projectId) => {
      if (projectId === 7) {
        project7JobsCalls += 1;
        if (project7JobsCalls === 1) return [];
        if (project7JobsCalls === 2) return project7FreshJobs.promise;
      }
      return [];
    });
    vi.spyOn(api, "createRenderJob").mockReturnValue(create.promise);

    render(<App />);
    await screen.findByRole("heading", { name: "project-7" });
    fireEvent.click(formalButton());
    fireEvent.click(screen.getByRole("button", { name: /project-8/ }));
    await screen.findByRole("heading", { name: "project-8" });

    create.resolve({ ok: true, created: true });
    await vi.waitFor(() => expect(jobs.mock.calls.some(([projectId]) => projectId === 7)).toBe(true));
    project7FreshJobs.resolve([{ job_id: "job-7", kind: "正式輸出", status: "running", message: "執行中", percent: 5 }]);
    await vi.waitFor(() => expect(jobs.mock.calls.some(([projectId]) => projectId === 8)).toBe(true));

    expect(screen.getByRole("heading", { name: "project-8" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "正在建立正式輸出…" })).toBeNull();
    expect(formalButton().disabled).toBe(false);
    expect(project7JobsCalls).toBe(2);
  });
});
