import { describe, expect, it, vi } from "vitest";
import type { Job, ProjectDetail } from "../src/api";
import { ProjectDataLoader } from "../src/projectDataLoader";

function detail(): ProjectDetail {
  return {
    project: { id: 7, name: "test", status: "needs_review" },
    clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "test", current: "review", stages: [] },
    review: {}, script: "", folder: "", can_render: false, render_gate_reason: "待核准",
  };
}

describe("ProjectDataLoader explicit result", () => {
  it("returns a structured network failure instead of an empty jobs success", async () => {
    const loader = new ProjectDataLoader(
      { project: () => Promise.reject(new Error("project unavailable")), jobs: () => Promise.resolve<Job[]>([]) },
      () => true,
      () => true,
      vi.fn(),
      vi.fn(),
    );
    const result = await loader.loadResult(7);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.kind).toBe("network");
  });

  it("does not apply a response after the project is switched", async () => {
    let current = 7;
    const apply = vi.fn();
    const loader = new ProjectDataLoader(
      { project: () => Promise.resolve(detail()), jobs: () => Promise.resolve<Job[]>([]) },
      (projectId) => projectId === current,
      () => true,
      apply,
      vi.fn(),
    );
    const request = loader.loadResult(7);
    current = 8;
    const result = await request;
    expect(result.ok).toBe(true);
    expect(apply).not.toHaveBeenCalled();
  });
});
