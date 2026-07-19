import { fireEvent, render, screen } from "@testing-library/react";
import { vi, describe, expect, it } from "vitest";
import { api, Job } from "../src/api";
import { RenderJobPanel } from "../src/components/render/RenderJobPanel";

const baseJob: Job = {
  kind: "內容感知",
  status: "running",
  message: "處理中",
  percent: 40,
};

function renderPanel(jobs: Job[]) {
  return render(
    <RenderJobPanel
      jobs={jobs}
      projectId={7}
      setMessage={vi.fn()}
      refreshProject={vi.fn().mockResolvedValue(undefined)}
    />,
  );
}

describe("RenderJobPanel", () => {
  it("uses the precise legacy cancel endpoint instead of stopping the whole project", async () => {
    const cancelLegacyJob = vi.spyOn(api, "cancelLegacyJob").mockResolvedValue({ ok: true, message: "已停止指定背景工作" });
    const stopJobs = vi.spyOn(api, "stopJobs").mockResolvedValue({ ok: true });
    renderPanel([{ ...baseJob, legacy_job_key: "analyze" }]);

    fireEvent.click(screen.getByRole("button", { name: "停止此工作" }));

    await vi.waitFor(() => expect(cancelLegacyJob).toHaveBeenCalledWith(7, "analyze"));
    expect(stopJobs).not.toHaveBeenCalled();
    cancelLegacyJob.mockRestore();
    stopJobs.mockRestore();
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
