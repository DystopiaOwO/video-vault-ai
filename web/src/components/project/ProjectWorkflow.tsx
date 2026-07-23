import type { Job, ProjectDetail } from "../../api";

export type ProjectWorkflowStep = {
  label: string;
  done: boolean;
};

const SUCCESS_STATUSES = new Set(["done", "completed", "succeeded", "success"]);

function normalized(value: unknown): string {
  return String(value || "").trim().toLocaleLowerCase();
}

function successful(value: unknown): boolean {
  return SUCCESS_STATUSES.has(normalized(value));
}

function outputStageSucceeded(detail: ProjectDetail): boolean {
  return detail.workflow.stages.some((stage) => {
    const identity = `${normalized(stage.id)} ${normalized(stage.label)}`;
    return successful(stage.status) && /(render|output|export|輸出|成片)/.test(identity);
  });
}

function outputJobSucceeded(jobs: Job[]): boolean {
  return jobs.some((job) => {
    if (!successful(job.status)) return false;
    const identity = [job.kind, job.stage, job.message, job.output_path].map(normalized).join(" ");
    return Boolean(job.output_path) || /(render|output|export|輸出|成片|mp4)/.test(identity);
  });
}

export function projectWorkflowSteps(detail: ProjectDetail, jobs: Job[]): ProjectWorkflowStep[] {
  const clipsImported = detail.clips.length > 0;
  const perceptionDone = clipsImported && detail.clips.every((clip) => clip.segment_count > 0 || normalized(clip.status) === "perceived");
  const includedStoryboardSegments = detail.storyboard?.segments
    ? Object.values(detail.storyboard.segments).filter((segment) => segment.included).length
    : detail.segments.filter((segment) => segment.include !== false).length;
  const storyDone = includedStoryboardSegments > 0 && (Boolean(detail.storyboard?.exists) || Boolean(detail.script.trim()));
  const approved = Boolean(detail.review?.approved_by_user) || detail.can_render || normalized(detail.project.status) === "approved";
  const outputDone = outputStageSucceeded(detail) || outputJobSucceeded(jobs);

  return [
    { label: "新增專案", done: true },
    { label: "匯入素材", done: clipsImported },
    { label: "內容感知", done: perceptionDone },
    { label: "故事整理", done: storyDone },
    { label: "核准", done: approved },
    { label: "輸出", done: outputDone },
  ];
}

export function ProjectWorkflow({ detail, jobs }: { detail: ProjectDetail; jobs: Job[] }) {
  const steps = projectWorkflowSteps(detail, jobs);
  const currentIndex = steps.findIndex((step) => !step.done);

  return <div className="workflow" aria-label="專案流程">
    {steps.map((step, index) => <span
      key={step.label}
      className={step.done ? "step done" : "step"}
      aria-current={index === currentIndex ? "step" : undefined}
      title={step.done ? `${step.label}已完成` : `${step.label}尚未完成`}
    >{step.label}</span>)}
  </div>;
}
