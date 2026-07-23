export type ProjectMutation =
  | "approve"
  | "reject"
  | "revise"
  | "upload"
  | "analyze"
  | "storyboard"
  | "timing"
  | "audio"
  | "color"
  | "clip-summary"
  | "render"
  | "render-cancel"
  | "export";

export type ProjectMutationChannel = "review" | "media" | "output";

export type ProjectMutationToken = {
  projectId: number;
  mutation: ProjectMutation;
  channel: ProjectMutationChannel;
  generation: number;
};

export type ProjectMutationSnapshot = ProjectMutationToken;

const CHANNELS: Record<ProjectMutation, ProjectMutationChannel> = {
  approve: "review",
  reject: "review",
  revise: "review",
  upload: "media",
  analyze: "media",
  storyboard: "review",
  timing: "review",
  audio: "media",
  color: "media",
  "clip-summary": "media",
  render: "output",
  "render-cancel": "output",
  export: "output",
};

export function mutationChannel(mutation: ProjectMutation): ProjectMutationChannel {
  return CHANNELS[mutation];
}

export function mutationLabel(mutation: ProjectMutation): string {
  const labels: Record<ProjectMutation, string> = {
    approve: "核准",
    reject: "退回",
    revise: "故事重建",
    upload: "素材匯入",
    analyze: "內容感知",
    storyboard: "分鏡",
    timing: "片段剪點",
    audio: "音訊設定",
    color: "調色設定",
    "clip-summary": "內容感知描述",
    render: "正式輸出",
    "render-cancel": "停止背景工作",
    export: "素材輸出",
  };
  return labels[mutation];
}

export type ProjectMutationControls = {
  beginProjectMutation: (projectId: number, mutation: ProjectMutation) => ProjectMutationToken | null;
  finishProjectMutation: (token: ProjectMutationToken) => void;
  isProjectMutationBusy: (projectId: number) => boolean;
  currentProjectMutation: (projectId?: number) => ProjectMutationSnapshot | null;
};

export function refreshFailureMessage(successMessage: string, error: unknown, subject = "畫面"): string {
  const reason = error instanceof Error ? error.message : "未知錯誤";
  return `${successMessage}，但${subject}更新失敗：${reason}`;
}

export class ProjectMutationCoordinator {
  private generation = 0;
  private active: ProjectMutationToken | null = null;

  begin(projectId: number, mutation: ProjectMutation): ProjectMutationToken | null {
    if (this.active?.projectId === projectId) return null;
    const token: ProjectMutationToken = {
      projectId,
      mutation,
      channel: mutationChannel(mutation),
      generation: ++this.generation,
    };
    this.active = token;
    return token;
  }

  finish(token: ProjectMutationToken): void {
    if (this.isCurrent(token, token.projectId)) this.active = null;
  }

  switchProject(): void {
    this.generation += 1;
    this.active = null;
  }

  current(): ProjectMutationSnapshot | null {
    return this.active;
  }

  isBusy(projectId: number): boolean {
    return this.active?.projectId === projectId;
  }

  isCurrent(token: ProjectMutationToken, projectId: number): boolean {
    return Boolean(
      this.active
      && this.active.generation === token.generation
      && this.active.projectId === projectId
      && token.projectId === projectId,
    );
  }
}

export function createProjectMutationControls(
  coordinator: ProjectMutationCoordinator,
  onBegin?: (token: ProjectMutationToken) => void,
  onFinish?: (token: ProjectMutationToken) => void,
): ProjectMutationControls {
  return {
    beginProjectMutation: (projectId, mutation) => {
      const token = coordinator.begin(projectId, mutation);
      if (token) onBegin?.(token);
      return token;
    },
    finishProjectMutation: (token) => {
      coordinator.finish(token);
      onFinish?.(token);
    },
    isProjectMutationBusy: (projectId) => coordinator.isBusy(projectId),
    currentProjectMutation: (projectId) => {
      const current = coordinator.current();
      return !current || projectId === undefined || current.projectId === projectId ? current : null;
    },
  };
}
