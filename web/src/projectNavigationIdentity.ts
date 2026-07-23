export type ProjectOperationToken = {
  projectId: number;
  navigationVersion: number;
};

/**
 * Gives async project actions the same identity boundary as project loading.
 * Switching projects invalidates every action started for the previous view.
 */
export class ProjectNavigationIdentity {
  private navigationVersion = 0;

  begin(projectId: number): ProjectOperationToken {
    return { projectId, navigationVersion: this.navigationVersion };
  }

  switchProject(): void {
    this.navigationVersion += 1;
  }

  isCurrent(token: ProjectOperationToken, currentProjectId: number): boolean {
    return token.projectId === currentProjectId && token.navigationVersion === this.navigationVersion;
  }
}
