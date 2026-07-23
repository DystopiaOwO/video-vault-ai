import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { vi, afterEach, describe, expect, it } from "vitest";
import { api, Job } from "../src/api";
import { RenderJobPanel } from "../src/components/render/RenderJobPanel";

const baseJob: Job = {
  kind: "內容感知",
  status: "running",
  message: "處理中",
  percent: 40,
};

function renderPanel(jobs: Job[], setMessage = vi.fn(), refreshProject = vi.fn().mockResolvedValue([])) {
  const view = render(
    <RenderJobPanel
      jobs={jobs}
      projectId={7}
      setMessage={setMessage}
      refreshProject={refreshProject}
    />,
  );
  return { ...view, setMessage, refreshProject };
}

describe("RenderJobPanel", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("uses the precise legacy cancel endpoint instead of stopping the whole project", async () => {
    const cancelLegacyJob = vi.spyOn(api, "cancelLegacyJob").mockResolvedValue({ ok: true, message: "已停止指定背景工作" });
    const stopJobs = vi.spyOn(api, "stopJobs").mockResolvedValue({ ok: true });
    renderPanel([{ ...baseJob, legacy_job_key: "analyze" }]);

    fireEvent.click(screen.getByRole("button", { name: "停止此工作" }));

    await vi.waitFor(() => expect(cancelLegacyJob).toHaveBeenCalledWith(7, "analyze"));
    expect(stopJobs).not.toHaveBeenCalled();
  });

  it("shows legacy cancellation errors instead of a success message", async () => {
    const setMessage = vi.fn();
    const refreshProject = vi.fn().mockResolvedValue([]);
    vi.spyOn(api, "cancelLegacyJob").mockResolvedValue({ ok: false, error: "找不到指定背景工作" });
    renderPanel([{ ...baseJob, legacy_job_key: "analyze" }], setMessage, refreshProject);

    fireEvent.click(screen.getByRole("button", { name: "停止此工作" }));

    await vi.waitFor(() => expect(setMessage).toHaveBeenCalledWith("停止失敗：找不到指定背景工作"));
    expect(setMessage).not.toHaveBeenCalledWith("停止要求已送出");
    expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true });
  });

  it("shows formal cancellation reason when the API returns ok=false", async () => {
    const setMessage = vi.fn();
    const refreshProject = vi.fn().mockResolvedValue([]);
    vi.spyOn(api, "cancelRenderJob").mockResolvedValue({ ok: false, reason: "job is already finished" });
    renderPanel([{ ...baseJob, job_id: "formal-1" }], setMessage, refreshProject);

    fireEvent.click(screen.getByRole("button", { name: "停止此 Render" }));

    await vi.waitFor(() => expect(setMessage).toHaveBeenCalledWith("停止失敗：job is already finished"));
    expect(setMessage).not.toHaveBeenCalledWith("停止要求已送出");
    expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true });
  });

  it("shows cache only after a formal render succeeds", () => {
    renderPanel([
      { ...baseJob, status: "running", job_id: "running-job", cache_hit: false },
      { ...baseJob, status: "succeeded", job_id: "done-job", cache_hit: true },
    ]);

    expect(screen.getByText("Final Cache：命中")).toBeTruthy();
    expect(screen.queryByText("Final Cache：本次建立")).toBeNull();
  });

  it("marks legacy done as completed and only shows current segment for active formal jobs", () => {
    renderPanel([
      { ...baseJob, status: "done", legacy_job_key: "analyze" },
      { ...baseJob, status: "succeeded", job_id: "finished-job", current_segment_id: "clip_001" },
      { ...baseJob, status: "running", job_id: "active-job", current_segment_id: "clip_002" },
    ]);

    expect(screen.getAllByText("已完成").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/目前片段：clip_002/)).toBeTruthy();
    expect(screen.queryByText(/目前片段：clip_001/)).toBeNull();
  });
});
