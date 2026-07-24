import { describe, expect, it } from "vitest";
import type { Job, ProjectDetail } from "../src/api";
import { projectWorkflowSteps } from "../src/components/project/ProjectWorkflow";

function detail(overrides: Partial<ProjectDetail> = {}): ProjectDetail {
  return {
    project: { id: 1, name: "福岡旅行", status: "needs_review" },
    clips: [],
    segments: [],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "project", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: false,
    render_gate_reason: "待核准",
    color: {} as ProjectDetail["color"],
    audio: {} as ProjectDetail["audio"],
    storyboard: { schema_version: 1, groups: [], segments: {}, exists: false },
    ...overrides,
  };
}

function stepState(input: ProjectDetail, jobs: Job[] = []) {
  return Object.fromEntries(projectWorkflowSteps(input, jobs).map((step) => [step.label, step.done]));
}

describe("projectWorkflowSteps", () => {
  it("requires every imported clip to finish perception", () => {
    const input = detail({
      clips: [
        { clip_id: "a", video_id: 1, filename: "a.mp4", status: "perceived", segment_count: 2, duration_seconds: 4, detected_category: "travel", time_of_day: "morning", visual_summary: "" },
        { clip_id: "b", video_id: 2, filename: "b.mp4", status: "pending", segment_count: 0, duration_seconds: 5, detected_category: "travel", time_of_day: "morning", visual_summary: "" },
      ],
    });

    expect(stepState(input)["匯入素材"]).toBe(true);
    expect(stepState(input)["內容感知"]).toBe(false);

    input.clips[1].segment_count = 1;
    expect(stepState(input)["內容感知"]).toBe(true);
  });

  it("requires included storyboard content and a generated story", () => {
    const input = detail({
      script: "早上抵達車站。",
      storyboard: {
        schema_version: 1,
        groups: [],
        exists: true,
        segments: { a: { group_id: "g", order: 1, included: false, locked: false, thumbnail_time_ratio: .5, notes: "" } },
      },
    });

    expect(stepState(input)["故事整理"]).toBe(false);
    input.storyboard.segments.a.included = true;
    expect(stepState(input)["故事整理"]).toBe(true);
  });

  it("does not mark output complete for unrelated successful jobs", () => {
    const jobs: Job[] = [{ kind: "內容感知", status: "completed", stage: "analyze", message: "分析完成", percent: 100, output_path: "/tmp/perception.json" }];

    expect(stepState(detail(), jobs)["輸出"]).toBe(false);
  });

  it("marks output complete only after a successful output job or stage", () => {
    const running: Job[] = [{ kind: "正式輸出", status: "running", stage: "render", message: "輸出中", percent: 72 }];
    const completed: Job[] = [{ kind: "正式輸出", status: "completed", stage: "render", message: "輸出完成", percent: 100, output_path: "D:/exports/fukuoka.mp4" }];

    expect(stepState(detail(), running)["輸出"]).toBe(false);
    expect(stepState(detail(), completed)["輸出"]).toBe(true);
    expect(stepState(detail({ workflow: { style: "test", current: "output", stages: [{ id: "final-output", label: "正式輸出", status: "succeeded", artifacts: [] }] } }))["輸出"]).toBe(true);
  });

  it("accepts approval from the review state, render gate, or project status", () => {
    expect(stepState(detail({ review: { approved_by_user: true } }))["核准"]).toBe(true);
    expect(stepState(detail({ can_render: true }))["核准"]).toBe(true);
    expect(stepState(detail({ project: { id: 1, name: "福岡旅行", status: "approved" } }))["核准"]).toBe(true);
  });
});
