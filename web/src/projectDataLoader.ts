import { Job, ProjectDetail } from "./api";

export type ProjectDataLoadOptions = {
  forceFresh?: boolean;
  throwOnError?: boolean;
  timeoutMs?: number;
};

export type ProjectLoadResult =
  | { ok: true; applied: true; projectRevision?: number; jobs: Job[] }
  | { ok: true; applied: false; reason: "stale" | "aborted" | "unmounted"; jobs: Job[] }
  | { ok: false; kind: "network" | "timeout" | "conflict"; error: Error; currentRevision?: number };

export type ProjectDataClient = {
  project: (projectId: number, signal?: AbortSignal) => Promise<ProjectDetail>;
  jobs: (projectId: number, signal?: AbortSignal) => Promise<Job[]>;
};

type PendingRequest = {
  projectId: number;
  generation: number;
  promise: Promise<Job[]>;
  controller: AbortController;
  errorReported: boolean;
  hasThrowingCaller: boolean;
};

export class ProjectDataLoader {
  private generation = 0;
  private pending: PendingRequest | null = null;

  constructor(
    private readonly client: ProjectDataClient,
    private readonly isCurrentProject: (projectId: number) => boolean,
    private readonly isMounted: () => boolean,
    private readonly apply: (project: ProjectDetail, jobs: Job[]) => void,
    private readonly reportError: (error: unknown) => void,
  ) {}

  load(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    if (!projectId) return Promise.resolve([]);
    const pending = this.pending;
    if (options.forceFresh === true && pending?.projectId === projectId) {
      this.generation += 1;
      pending.controller.abort();
      return pending.promise.then(
        () => this.loadFresh(projectId, options),
        () => this.loadFresh(projectId, options),
      );
    }
    if (pending?.projectId === projectId) return this.settle(pending, options);
    return this.loadFresh(projectId, options);
  }

  async loadResult(projectId: number, options: ProjectDataLoadOptions = {}): Promise<ProjectLoadResult> {
    if (!projectId) return { ok: true, applied: false, reason: "stale", jobs: [] };
    try {
      const jobs = await this.load(projectId, { ...options, throwOnError: true });
      if (!this.isMounted()) return { ok: true, applied: false, reason: "unmounted", jobs };
      if (!this.isCurrentProject(projectId)) return { ok: true, applied: false, reason: "stale", jobs };
      return { ok: true, applied: true, projectRevision: undefined, jobs };
    } catch (error) {
      const value = error instanceof Error ? error : new Error(String(error));
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
  }

  private loadFresh(projectId: number, options: ProjectDataLoadOptions): Promise<Job[]> {
    return this.settle(this.start(projectId, options.timeoutMs), options);
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
        const request = Promise.all([this.client.project(projectId, controller.signal), this.client.jobs(projectId, controller.signal)]);
        const [project, jobs] = await Promise.race([request, timeout]);
        if (this.isMounted() && generation === this.generation && this.isCurrentProject(projectId)) {
          this.apply(project, jobs);
          return jobs;
        }
        return [];
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
      () => {
        if (this.pending?.promise === promise) this.pending = null;
      },
      () => {
        if (this.pending?.promise === promise) this.pending = null;
      },
    );
    return request;
  }

  private settle(request: PendingRequest, options: ProjectDataLoadOptions): Promise<Job[]> {
    const throwOnError = options.throwOnError === true;
    if (throwOnError) request.hasThrowingCaller = true;
    return request.promise.catch((error) => {
      if (throwOnError) throw error;
      if (
        !request.hasThrowingCaller
        && !request.errorReported
        && this.isMounted()
        && request.generation === this.generation
        && this.isCurrentProject(request.projectId)
      ) {
        request.errorReported = true;
        this.reportError(error);
      }
      return [];
    });
  }
}
