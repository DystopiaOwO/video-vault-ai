import { describe, expect, it, vi } from "vitest";
import type { Job, ProjectDetail } from "../src/api";
import { ProjectDataLoader, type ProjectDataLoadOptions } from "../src/projectDataLoader";

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
    render_gate_reason: "待核准",
    color: {
      schema_version: 2,
      enabled: true,
      reference: {},
      references: [],
      analysis: {},
      suggested: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
      applied: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
      segments: {},
    },
    audio: {
      schema_version: 1,
      enabled: true,
      bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 },
      original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 },
      normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
      segments: {},
    },
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
  };
}

function loaderWith(client: { project: () => Promise<ProjectDetail>; jobs: () => Promise<Job[]> }) {
  return new ProjectDataLoader(
    client,
    () => true,
    () => true,
    vi.fn(),
    vi.fn(),
  );
}

const throwOnError = { throwOnError: true } as ProjectDataLoadOptions & { throwOnError: true };

describe("ProjectDataLoader throwOnError contract", () => {
  it.each([
    ["project", () => Promise.reject(new Error("project GET failed")), () => Promise.resolve<Job[]>([])],
    ["jobs", () => Promise.resolve(detail(7)), () => Promise.reject(new Error("jobs GET failed"))],
  ] as const)("rejects when the %s request fails with throwOnError", async (_name, project, jobs) => {
    const loader = loaderWith({ project, jobs });

    await expect(loader.load(7, throwOnError)).rejects.toThrow(/GET failed/);
  });

  it("keeps ordinary polling non-throwing and reports a failed request", async () => {
    const reportError = vi.fn();
    const loader = new ProjectDataLoader(
      { project: () => Promise.reject(new Error("poll failed")), jobs: () => Promise.resolve<Job[]>([]) },
      () => true,
      () => true,
      vi.fn(),
      reportError,
    );

    await expect(loader.load(7)).resolves.toEqual([]);
    expect(reportError).toHaveBeenCalledWith(expect.objectContaining({ message: "poll failed" }));
  });
});
