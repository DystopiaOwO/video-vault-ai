import { Job, JobsSnapshot, ProjectDetail } from "./api";

export type ProjectDataLoadOptions = {
  forceFresh?: boolean;
  throwOnError?: boolean;
  timeoutMs?: number;
};

export type ProjectLoadResult =
  | { ok: true; applied: true; projectRevision?: number; jobs: Job[] }
  | { ok: true; applied: false; reason: "stale" | "aborted" | "unmounted"; projectRevision?: number; jobs: Job[] }
  | { ok: false; kind: "network" | "timeout" | "conflict"; error: Error; currentRevision?: number };

export type ProjectDataClient = {
  project: (projectId: number, signal?: AbortSignal) => Promise<ProjectDetail>;
  jobs: (projectId: number, signal?: AbortSignal) => Promise<JobsSnapshot | Job[]>;
};

type LoadedProjectData = {
  project: ProjectDetail;
  jobs: Job[];
  projectRevision?: number;
};

type PendingRequest = {
  projectId: number;
  generation: number;
  promise: Promise<LoadedProjectData>;
  controller: AbortController;
  errorReported: boolean;
  hasThrowingCaller: boolean;
};

type SuccessorRequest = {
  projectId: number;
  promise: Promise<LoadedProjectData>;
};

function isAbortError(error: unknown): boolean {
  return Boolean(error && typeof error === "object" && (error as { name?: unknown }).name === "AbortError");
}

export class ProjectDataLoader {
  private generation = 0;
  private pending: PendingRequest | null = null;
  private successor: SuccessorRequest | null = null;

  constructor(
    private readonly client: ProjectDataClient,
    private readonly isCurrentProject: (projectId: number) => boolean,
    private readonly isMounted: () => boolean,
    private readonly apply: (project: ProjectDetail, jobs: Job[], projectRevision?: number) => void,
    private readonly reportError: (error: unknown) => void,
  ) {}

  load(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return this.loadData(projectId, options).then((data) => data.jobs);
  }

  async loadResult(projectId: number, options: ProjectDataLoadOptions = {}): Promise<ProjectLoadResult> {
    if (!projectId) return { ok: true, applied: false, reason: "stale", jobs: [] };
    try {
      const data = await this.loadData(projectId, { ...options, throwOnError: true });
      if (!this.isMounted()) return { ok: true, applied: false, reason: "unmounted", projectRevision: data.projectRevision, jobs: data.jobs };
      if (!this.isCurrentProject(projectId)) return { ok: true, applied: false, reason: "stale", projectRevision: data.projectRevision, jobs: data.jobs };
      return { ok: true, applied: true, projectRevision: data.projectRevision, jobs: data.jobs };
    } catch (error) {
      const value = error instanceof Error ? error : new Error(String(error));
      if (isAbortError(error)) return { ok: true, applied: false, reason: "aborted", jobs: [] };
      const status = Number((error as { status?: unknown })?.status || 0);
      const payload = (error as { payload?: Record<string, unknown> })?.payload || {};
      return {
        ok: false,
        kind: status === 409 || payload.code === "stale_project_revision" ? "conflict" : value.name === "TimeoutError" ? "timeout" : "network",
        error: value,
        currentRevision: typeof payload.project_revision === "number" ? payload.project_revision : undefined,
      };
    }
  }

  invalidate(): void {
    this.generation += 1;
    this.pending?.controller.abort();
    this.successor = null;
  }

  private loadData(projectId: number, options: ProjectDataLoadOptions): Promise<LoadedProjectData> {
    if (!projectId) return Promise.resolve({ project: {} as ProjectDetail, jobs: [] });
    const pending = this.pending;
    const queued = this.successor;
    if (options.forceFresh === true) {
      if (queued?.projectId === projectId) return this.settle(queued.promise, options);
      if (pending?.projectId === projectId) {
        this.generation += 1;
        pending.controller.abort();
        let successorPromise: Promise<LoadedProjectData>;
        successorPromise = pending.promise.catch(() => undefined).then(() => {
          if (this.successor?.promise !== successorPromise) {
            throw Object.assign(new Error("superseded project load"), { name: "AbortError" });
          }
          this.successor = null;
          return this.start(projectId, options.timeoutMs).promise;
        });
        this.successor = { projectId, promise: successorPromise };
        return this.settle(successorPromise, options);
      }
      if (queued?.projectId === projectId) return this.settle(queued.promise, options);
      return this.settle(this.start(projectId, options.timeoutMs).promise, options);
    }
    if (pending?.projectId === projectId) return this.settle(pending.promise, options);
    if (queued?.projectId === projectId) return this.settle(queued.promise, options);
    return this.settle(this.start(projectId, options.timeoutMs).promise, options);
  }

  private start(projectId: number, timeoutMs = 15000): PendingRequest {
    const generation = ++this.generation;
    const controller = new AbortController();
    const promise = (async () => {
      let timer: number | undefined;
      const timeout = new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => {
          controller.abort();
          const error = new Error("專案狀態請求逾時");
          error.name = "TimeoutError";
          reject(error);
        }, Math.max(1000, timeoutMs));
      });
      try {
        const request = Promise.all([
          this.client.project(projectId, controller.signal),
          this.client.jobs(projectId, controller.signal),
        ]);
        const [project, rawSnapshot] = await Promise.race([request, timeout]);
        const snapshot: JobsSnapshot = Array.isArray(rawSnapshot) ? { jobs: rawSnapshot } : rawSnapshot;
        const projectRevision = snapshot.project_revision ?? project.project_revision;
        const data = { project, jobs: snapshot.jobs || [], projectRevision };
        if (this.isMounted() && generation === this.generation && this.isCurrentProject(projectId)) {
          this.apply(data.project, data.jobs, data.projectRevision);
        }
        return data;
      } finally {
        if (timer !== undefined) window.clearTimeout(timer);
      }
    })();
    const request: PendingRequest = {
      projectId,
      generation,
      promise,
      controller,
      errorReported: false,
      hasThrowingCaller: false,
    };
    this.pending = request;
    void promise.then(
      () => { if (this.pending?.promise === promise) this.pending = null; },
      () => { if (this.pending?.promise === promise) this.pending = null; },
    );
    return request;
  }

  private settle(request: Promise<LoadedProjectData>, options: ProjectDataLoadOptions): Promise<LoadedProjectData> {
    const throwOnError = options.throwOnError === true;
    const pending = this.pending;
    if (pending?.promise === request && throwOnError) pending.hasThrowingCaller = true;
    return request.catch((error) => {
      if (isAbortError(error)) throw error;
      if (throwOnError) throw error;
      if (
        pending
        && !pending.hasThrowingCaller
        && !pending.errorReported
        && this.isMounted()
        && pending.generation === this.generation
        && this.isCurrentProject(pending.projectId)
      ) {
        pending.errorReported = true;
        this.reportError(error);
      }
      return { project: {} as ProjectDetail, jobs: [] };
    });
  }
}
