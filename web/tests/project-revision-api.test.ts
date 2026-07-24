import { afterEach, describe, expect, it, vi } from "vitest";

function response(status: number, payload: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 409 ? "Conflict" : "OK",
    json: async () => payload,
  } as Response;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("project revision API", () => {
  it("remembers ProjectDetail revision and advances the token after a mutation", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, {
        project: { id: 71, name: "A", status: "needs_review", project_revision: 4 },
        clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "", current: "", stages: [] },
        review: {}, script: "", folder: "", can_render: false, render_gate_reason: "",
        color: {}, audio: {}, storyboard: {},
      }))
      .mockResolvedValueOnce(response(200, { ok: true, plan_rebuilt: true, project_revision: 5 }))
      .mockResolvedValueOnce(response(200, { ok: true, project_revision: 6 }));
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../src/api");

    await api.project(71);
    await api.saveClipSummary(71, 9, "抵達飯店");
    await api.buildPlan(71);

    const firstMutation = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const secondMutation = JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body));
    expect(firstMutation).toMatchObject({ project_id: 71, video_id: 9, base_revision: 4 });
    expect(secondMutation).toMatchObject({ project_id: 71, base_revision: 5 });
  });

  it("surfaces HTTP 409 project conflicts with the server payload", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(200, {
        project: { id: 72, name: "B", status: "needs_review", project_revision: 8 },
        clips: [], segments: [], bgm: [], plan: {}, workflow: { style: "", current: "", stages: [] },
        review: {}, script: "", folder: "", can_render: false, render_gate_reason: "",
        color: {}, audio: {}, storyboard: {},
      }))
      .mockResolvedValueOnce(response(409, {
        ok: false,
        code: "project_revision_conflict",
        error: "專案已被其他操作更新，請重新整理後再試。",
        expected_revision: 8,
        current_revision: 9,
      }));
    vi.stubGlobal("fetch", fetchMock);
    const { api, ApiError } = await import("../src/api");

    await api.project(72);
    const error = await api.approve(72, "").catch((value) => value);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(409);
    expect(error.message).toContain("重新整理");
    expect(error.data).toMatchObject({
      code: "project_revision_conflict",
      expected_revision: 8,
      current_revision: 9,
    });
  });
});
