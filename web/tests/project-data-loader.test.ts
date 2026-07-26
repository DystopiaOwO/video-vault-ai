import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectDataLoader } from "../src/projectDataLoader";
import type { Job, ProjectDetail } from "../src/api";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function detail(revision: number): ProjectDetail {
  return { project: { id: 1, name: "p", status: "needs_review" }, project_revision: revision, clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "", current: "", stages: [] }, review: {}, script: "", folder: "", can_render: false, render_gate_reason: "", color: {} as ProjectDetail["color"], audio: {} as ProjectDetail["audio"], storyboard: {} as ProjectDetail["storyboard"] };
}

const emptyJobs: Job[] = [];

afterEach(() => vi.restoreAllMocks());

describe("ProjectDataLoader lifecycle", () => {
  it("coalesces three forceFresh callers into one successor request", async () => {
    const firstProject = deferred<ProjectDetail>();
    const firstJobs = deferred<{ jobs: Job[]; project_revision: number }>();
    const secondProject = deferred<ProjectDetail>();
    const secondJobs = deferred<{ jobs: Job[]; project_revision: number }>();
    let projectCalls = 0;
    let jobsCalls = 0;
    const client = {
      project: vi.fn(async () => (projectCalls++ === 0 ? firstProject.promise : secondProject.promise)),
      jobs: vi.fn(async () => (jobsCalls++ === 0 ? firstJobs.promise : secondJobs.promise)),
    };
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), vi.fn());

    void loader.load(1);
    const one = loader.load(1, { forceFresh: true, throwOnError: true });
    const two = loader.load(1, { forceFresh: true, throwOnError: true });
    const three = loader.load(1, { forceFresh: true, throwOnError: true });
    firstProject.resolve(detail(1));
    firstJobs.resolve({ jobs: emptyJobs, project_revision: 1 });
    await vi.waitFor(() => expect(projectCalls).toBe(2));
    expect(jobsCalls).toBe(2);
    secondProject.resolve(detail(2));
    secondJobs.resolve({ jobs: emptyJobs, project_revision: 2 });
    await expect(Promise.all([one, two, three])).resolves.toEqual([emptyJobs, emptyJobs, emptyJobs]);
  });

  it("passes AbortSignal to both requests and reports navigation abort without a network error", async () => {
    const projectWait = deferred<ProjectDetail>();
    const jobsWait = deferred<{ jobs: Job[]; project_revision: number }>();
    const client = {
      project: vi.fn((_id: number, signal?: AbortSignal) => new Promise<ProjectDetail>((resolve, reject) => {
        signal?.addEventListener("abort", () => { const error = new Error("aborted"); error.name = "AbortError"; reject(error); });
        projectWait.promise.then(resolve, reject);
      })),
      jobs: vi.fn((_id: number, signal?: AbortSignal) => new Promise<{ jobs: Job[]; project_revision: number }>((resolve, reject) => {
        signal?.addEventListener("abort", () => { const error = new Error("aborted"); error.name = "AbortError"; reject(error); });
        jobsWait.promise.then(resolve, reject);
      })),
    };
    const report = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), report);
    const result = loader.loadResult(1, { forceFresh: true });
    loader.invalidate();
    await expect(result).resolves.toMatchObject({ ok: true, applied: false, reason: "aborted" });
    expect(client.project.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal);
    expect(client.jobs.mock.calls[0]?.[1]).toBeInstanceOf(AbortSignal);
    expect(report).not.toHaveBeenCalled();
  });

  it("returns the revision that was actually applied", async () => {
    const apply = vi.fn();
    const client = { project: vi.fn(async () => detail(4)), jobs: vi.fn(async () => ({ jobs: emptyJobs, project_revision: 4 })) };
    const loader = new ProjectDataLoader(client, () => true, () => true, apply, vi.fn());
    await expect(loader.loadResult(1)).resolves.toMatchObject({ ok: true, applied: true, projectRevision: 4 });
    expect(apply).toHaveBeenCalledWith(expect.anything(), emptyJobs, 4);
  });
});
