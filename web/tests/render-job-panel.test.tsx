import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type Job } from "../src/api";
import { filterRenderJobs, RenderJobPanel, sortRenderJobs } from "../src/components/render/RenderJobPanel";

function job(overrides: Partial<Job> = {}): Job {
  return {
    kind: "正式輸出",
    status: "succeeded",
    stage: "done",
    message: "輸出完成",
    percent: 100,
    ...overrides,
  };
}

function renderPanel(jobs: Job[]) {
  const setMessage = vi.fn();
  const refreshProject = vi.fn(async () => jobs);
  const view = render(<RenderJobPanel jobs={jobs} projectId={7} setMessage={setMessage} refreshProject={refreshProject} />);
  return { ...view, setMessage, refreshProject };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
});

describe("render job helpers", () => {
  it("sorts active jobs first and then uses the latest update time", () => {
    const jobs = [
      job({ job_id: "old", updated_at: "2026-07-22T10:00:00Z" }),
      job({ job_id: "active", status: "running", updated_at: "2026-07-21T10:00:00Z" }),
      job({ job_id: "new", updated_at: "2026-07-23T10:00:00Z" }),
    ];

    expect(sortRenderJobs(jobs).map((item) => item.job_id)).toEqual(["active", "new", "old"]);
  });

  it("filters active, completed, and failed jobs", () => {
    const jobs = [
      job({ status: "running" }),
      job({ status: "completed" }),
      job({ status: "failed" }),
      job({ status: "cancelled" }),
    ];

    expect(filterRenderJobs(jobs, "active")).toHaveLength(1);
    expect(filterRenderJobs(jobs, "completed")).toHaveLength(1);
    expect(filterRenderJobs(jobs, "failed")).toHaveLength(1);
    expect(filterRenderJobs(jobs, "all")).toHaveLength(4);
  });
});

describe("RenderJobPanel", () => {
  it("limits long history by default and expands on demand", () => {
    const jobs = Array.from({ length: 7 }, (_, index) => job({ job_id: `job-${index}`, message: `工作 ${index}`, updated_at: `2026-07-${String(index + 1).padStart(2, "0")}T10:00:00Z` }));
    renderPanel(jobs);

    expect(screen.getAllByRole("article")).toHaveLength(6);
    fireEvent.click(screen.getByRole("button", { name: "顯示另外 1 項工作" }));
    expect(screen.getAllByRole("article")).toHaveLength(7);
    expect(screen.getByRole("button", { name: "收合歷史工作" })).toBeTruthy();
  });

  it("filters jobs and shows an empty filtered state", () => {
    renderPanel([job({ job_id: "done" }), job({ job_id: "active", status: "running" })]);

    fireEvent.change(screen.getByLabelText("篩選 Render Job"), { target: { value: "active" } });
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByText("執行中")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("篩選 Render Job"), { target: { value: "failed" } });
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.getByText("這個篩選條件目前沒有工作。")).toBeTruthy();
  });

  it("refreshes project jobs on demand", async () => {
    const { refreshProject, setMessage } = renderPanel([]);

    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));

    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true }));
    expect(setMessage).toHaveBeenCalledWith("工作狀態已更新。");
  });

  it("keeps full paths collapsed and copies them on request", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const { setMessage } = renderPanel([job({ job_id: "done", output_path: "D:/exports/travel/final.mp4", log_path: "D:/exports/travel/render-report.json" })]);

    expect(screen.getByText("final.mp4")).toBeTruthy();
    expect(screen.queryByText("D:/exports/travel/final.mp4")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "複製輸出檔案路徑" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("D:/exports/travel/final.mp4"));
    expect(setMessage).toHaveBeenCalledWith("輸出檔案路徑已複製。");
  });

  it("shows a zero-based current segment position instead of dropping it", () => {
    renderPanel([job({ job_id: "active", status: "running", current_segment_id: "segment-a", current_segment_index: 0, segment_count: 12 })]);

    expect(screen.getByText("目前片段：segment-a（0/12）")).toBeTruthy();
  });

  it("cancels only the selected formal render", async () => {
    const cancel = vi.spyOn(api, "cancelRenderJob").mockResolvedValue({ ok: true, message: "已送出取消" });
    const { refreshProject, setMessage } = renderPanel([job({ job_id: "render-1", status: "running" })]);

    fireEvent.click(screen.getByRole("button", { name: "停止此 Render" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledWith("render-1"));
    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({ forceFresh: true, throwOnError: true }));
    expect(setMessage).toHaveBeenCalledWith("已送出取消");
  });
});
