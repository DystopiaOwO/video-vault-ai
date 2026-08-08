import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../src/api";
import { CloudReviewWorkspace } from "../src/components/project/CloudReviewWorkspace";

const plan = {
  contract_version: "cloud-review-v1",
  status: "ready",
  provider: "mock",
  policy: {},
  windows: [{
    project_id: 1,
    video_id: 2,
    run_uuid: "run-1",
    window_uuid: "window-1",
    ordinal: 1,
    frame_count: 3,
    frame_timestamps: [0, 2, 4],
    confidence: 0.2,
    reasons: ["low_confidence"],
    source_paths_exposed: false,
  }],
  rejected_windows: [],
  estimated_calls: 1,
  estimated_frames: 3,
  estimated_cost_usd: 0.03,
  privacy: { full_video_upload: false, payload: "selected_frames_only", source_paths_exposed: false },
} as const;

describe("CloudReviewWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows provider, frame count, cost and selected-window controls before send", async () => {
    const preview = vi.spyOn(api, "cloudReviewPlan").mockResolvedValue({ ok: true, plan, project_revision: 4 });
    render(<CloudReviewWorkspace projectId={1} projectRevision={4} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} />);

    fireEvent.click(screen.getByRole("button", { name: "檢查可複判區段" }));
    await waitFor(() => expect(screen.getByText(/Provider：mock/)).toBeTruthy());
    expect(screen.getByText(/3 張抽幀／1 次呼叫/)).toBeTruthy();
    expect(screen.getByText(/估算成本：\$0.0300 USD/)).toBeTruthy();
    expect(screen.getByText(/不會上傳整支影片/)).toBeTruthy();
    expect(preview).toHaveBeenCalledWith(1);
    preview.mockRestore();
  });

  it("keeps local-result semantics when cloud review fails", async () => {
    vi.spyOn(api, "cloudReviewPlan").mockResolvedValue({ ok: true, plan, project_revision: 4 });
    const review = vi.spyOn(api, "cloudReview").mockResolvedValue({ ok: false, code: "cloud_review_failed", error: "timeout", local_result_preserved: true });
    const refresh = vi.fn(async () => []);
    const setMessage = vi.fn();
    render(<CloudReviewWorkspace projectId={1} projectRevision={4} setMessage={setMessage} refreshProject={refresh} />);

    fireEvent.click(screen.getByRole("button", { name: "檢查可複判區段" }));
    await waitFor(() => screen.getByRole("button", { name: "送出 1 個區段" }));
    fireEvent.click(screen.getByRole("button", { name: "送出 1 個區段" }));
    await waitFor(() => expect(setMessage).toHaveBeenCalledWith(expect.stringContaining("已保留本地結果")));
    expect(review).toHaveBeenCalledWith(1, 4, ["window-1"]);
    expect(refresh).toHaveBeenCalled();
    review.mockRestore();
  });
});
