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
  promise: Promise<Job[]>;
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
    const requiresFreshRequest = options.forceFresh === true || options.throwOnError === true;
    if (requiresFreshRequest && pending?.projectId === projectId) {
      this.generation += 1;
      return pending.promise.then(
        () => this.start(projectId, options),
        () => this.start(projectId, options),
      );
    }
    if (pending?.projectId === projectId) return pending.promise;
    return this.start(projectId, options);
  }

  invalidate(): void {
    this.generation += 1;
  }

  private start(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    const generation = ++this.generation;
    const throwOnError = options.throwOnError === true || options.forceFresh === true;
    const promise = (async () => {
      try {
        const [project, jobs] = await Promise.all([this.client.project(projectId), this.client.jobs(projectId)]);
        if (this.isMounted() && generation === this.generation && this.isCurrentProject(projectId)) {
          this.apply(project, jobs);
          return jobs;
        }
      } catch (error) {
        if (throwOnError) throw error;
        if (this.isMounted() && generation === this.generation && this.isCurrentProject(projectId)) {
          this.reportError(error);
        }
      }
      return [];
    })();
    this.pending = { projectId, promise };
    void promise.then(
      () => {
        if (this.pending?.promise === promise) this.pending = null;
      },
      () => {
        if (this.pending?.promise === promise) this.pending = null;
      },
    );
    return promise;
  }
}
