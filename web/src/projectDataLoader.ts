import { Job, ProjectDetail } from "./api";

export type ProjectDataLoadOptions = {
  forceFresh?: boolean;
  throwOnError?: boolean;
};

export type ProjectDataClient = {
  project: (projectId: number) => Promise<ProjectDetail>;
  jobs: (projectId: number) => Promise<Job[]>;
};

type PendingRequest = {
  projectId: number;
  generation: number;
  promise: Promise<Job[]>;
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
      return pending.promise.then(
        () => this.loadFresh(projectId, options),
        () => this.loadFresh(projectId, options),
      );
    }
    if (pending?.projectId === projectId) return this.settle(pending, options);
    return this.loadFresh(projectId, options);
  }

  invalidate(): void {
    this.generation += 1;
  }

  private loadFresh(projectId: number, options: ProjectDataLoadOptions): Promise<Job[]> {
    return this.settle(this.start(projectId), options);
  }

  private start(projectId: number): PendingRequest {
    const generation = ++this.generation;
    const promise = (async () => {
      const [project, jobs] = await Promise.all([this.client.project(projectId), this.client.jobs(projectId)]);
      if (this.isMounted() && generation === this.generation && this.isCurrentProject(projectId)) {
        this.apply(project, jobs);
        return jobs;
      }
      return [];
    })();
    const request: PendingRequest = {
      projectId,
      generation,
      promise,
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
