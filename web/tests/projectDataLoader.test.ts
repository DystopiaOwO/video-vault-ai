import { describe, expect, it, vi } from "vitest";
import { Job, ProjectDetail } from "../src/api";
import { ProjectDataLoader } from "../src/projectDataLoader";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function detail(projectId: number): ProjectDetail {
  return {
    project: { id: projectId, name: `project-${projectId}`, status: "needs_review" },
    clips: [],
    segments: [],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "review", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "test",
  };
}

function createClient() {
  const requests: Array<{ project: ReturnType<typeof deferred<ProjectDetail>>; jobs: ReturnType<typeof deferred<Job[]>> }> = [];
  const client = {
    project: vi.fn(() => {
      const request = { project: deferred<ProjectDetail>(), jobs: deferred<Job[]>() };
      requests.push(request);
      return request.project.promise;
    }),
    jobs: vi.fn(() => requests[requests.length - 1].jobs.promise),
  };
  return { client, requests };
}

describe("ProjectDataLoader", () => {
  it("keeps ordinary polling errors resolved and reported", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("project unavailable");

    const request = loader.load(7);
    requests[0].project.reject(error);
    requests[0].jobs.resolve([]);

    await expect(request).resolves.toEqual([]);
    expect(reportError).toHaveBeenCalledWith(error);
  });

  it("rejects mutation refresh failures without reporting a duplicate UI error", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("jobs unavailable");

    const request = loader.load(7, { forceFresh: true, throwOnError: true });
    requests[0].project.resolve(detail(7));
    requests[0].jobs.reject(error);

    await expect(request).rejects.toBe(error);
    expect(reportError).not.toHaveBeenCalled();
  });

  it("keeps a forceFresh refresh non-throwing unless throwOnError is explicit", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("fresh refresh unavailable");

    const request = loader.load(7, { forceFresh: true });
    requests[0].project.resolve(detail(7));
    requests[0].jobs.reject(error);

    await expect(request).resolves.toEqual([]);
    expect(reportError).toHaveBeenCalledWith(error);
  });

  it("rejects an explicit throwOnError request", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("project unavailable");

    const request = loader.load(7, { throwOnError: true });
    requests[0].project.reject(error);
    requests[0].jobs.resolve([]);

    await expect(request).rejects.toBe(error);
    expect(reportError).not.toHaveBeenCalled();
  });

  it("starts a throwing fresh request after a pending request fails", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const oldError = new Error("old request failed");
    const freshError = new Error("fresh request failed");

    const requestA = loader.load(7);
    const requestB = loader.load(7, { forceFresh: true, throwOnError: true });
    requests[0].project.reject(oldError);
    requests[0].jobs.resolve([]);
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    requests[1].project.reject(freshError);
    requests[1].jobs.resolve([]);

    await expect(requestA).resolves.toEqual([]);
    await expect(requestB).rejects.toBe(freshError);
    expect(reportError).not.toHaveBeenCalled();
  });

  it("waits for request A, then starts fresh request B after a mutation", async () => {
    const { client, requests } = createClient();
    const applied: Job[][] = [];
    const loader = new ProjectDataLoader(client, () => true, () => true, (_project, jobs) => applied.push(jobs), vi.fn());
    const oldJobs: Job[] = [];
    const activeJob: Job = { kind: "正式輸出", job_id: "job-1", status: "running", message: "執行中", percent: 10 };

    const requestA = loader.load(7);
    const forcedRefresh = loader.load(7, { forceFresh: true });
    expect(requests).toHaveLength(1);

    requests[0].project.resolve(detail(7));
    requests[0].jobs.resolve(oldJobs);
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    expect(applied).toEqual([]);

    requests[1].project.resolve(detail(7));
    requests[1].jobs.resolve([activeJob]);
    await expect(Promise.all([requestA, forcedRefresh])).resolves.toEqual([[], [activeJob]]);
    expect(applied).toEqual([[activeJob]]);
  });

  it("does not apply an old project response after switching projects", async () => {
    const { client, requests } = createClient();
    let currentProjectId = 7;
    const applied: number[] = [];
    const loader = new ProjectDataLoader(client, (projectId) => projectId === currentProjectId, () => true, (project) => applied.push(project.project.id), vi.fn());

    const oldRequest = loader.load(7);
    currentProjectId = 8;
    const currentRequest = loader.load(8);
    expect(requests).toHaveLength(2);

    requests[0].project.resolve(detail(7));
    requests[0].jobs.resolve([]);
    requests[1].project.resolve(detail(8));
    requests[1].jobs.resolve([]);
    await Promise.all([oldRequest, currentRequest]);

    expect(applied).toEqual([8]);
  });

  it("does not leave the UI at request A running after cancellation refresh B", async () => {
    const { client, requests } = createClient();
    const applied: Job[][] = [];
    const loader = new ProjectDataLoader(client, () => true, () => true, (_project, jobs) => applied.push(jobs), vi.fn());
    const running: Job = { kind: "正式輸出", job_id: "job-1", status: "running", message: "執行中", percent: 40 };
    const cancelling: Job = { ...running, status: "cancelling", message: "停止中" };

    const requestA = loader.load(7);
    const cancellationRefresh = loader.load(7, { forceFresh: true });
    requests[0].project.resolve(detail(7));
    requests[0].jobs.resolve([running]);
    await vi.waitFor(() => expect(requests).toHaveLength(2));
    requests[1].project.resolve(detail(7));
    requests[1].jobs.resolve([cancelling]);
    await Promise.all([requestA, cancellationRefresh]);

    expect(applied).toEqual([[cancelling]]);
    expect(applied.flat()).not.toContainEqual(running);
  });

  it("deduplicates ordinary polling for the same project", async () => {
    const { client, requests } = createClient();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), vi.fn());

    const first = loader.load(7);
    const second = loader.load(7);
    expect(requests).toHaveLength(1);
    requests[0].project.resolve(detail(7));
    requests[0].jobs.resolve([]);

    await expect(Promise.all([first, second])).resolves.toEqual([[], []]);
  });

  it("keeps an ordinary polling caller non-throwing when it joins a strict refresh", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("shared request failed");

    const strictRefresh = loader.load(7, { throwOnError: true });
    const polling = loader.load(7);
    const strictResult = expect(strictRefresh).rejects.toBe(error);
    const pollingResult = expect(polling).resolves.toEqual([]);

    requests[0].project.reject(error);
    requests[0].jobs.resolve([]);

    await strictResult;
    await pollingResult;
    expect(reportError).not.toHaveBeenCalled();

    const nextPolling = loader.load(7);
    expect(requests).toHaveLength(2);
    requests[1].project.resolve(detail(7));
    requests[1].jobs.resolve([]);
    await expect(nextPolling).resolves.toEqual([]);
  });

  it("preserves strict rejection when it joins an ordinary pending poll", async () => {
    const { client, requests } = createClient();
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(client, () => true, () => true, vi.fn(), reportError);
    const error = new Error("coalesced poll failed");

    const polling = loader.load(7);
    const strictRefresh = loader.load(7, { throwOnError: true });
    const pollingResult = expect(polling).resolves.toEqual([]);
    const strictResult = expect(strictRefresh).rejects.toBe(error);

    requests[0].project.reject(error);
    requests[0].jobs.resolve([]);

    await pollingResult;
    await strictResult;
    expect(reportError).not.toHaveBeenCalled();
  });
});
