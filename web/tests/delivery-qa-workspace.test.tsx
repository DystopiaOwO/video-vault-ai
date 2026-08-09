import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type DeliveryQAState } from "../src/api";
import { createProjectMutationControls, ProjectMutationCoordinator } from "../src/projectMutation";
import { DeliveryQAWorkspace } from "../src/workspaces/delivery/DeliveryQAWorkspace";

function state(overrides: Partial<DeliveryQAState> = {}): DeliveryQAState {
  return {
    schema_version: 1,
    exists: true,
    qa_run_uuid: "qa-run-1",
    render_job_uuid: "render-1",
    lifecycle_status: "qa_needs_review",
    automation_status: "warning",
    deliverable_ready: false,
    summary: { pass: 5, warning: 1, blocked: 0, skipped: 0 },
    checks: [{
      check_id: "audio",
      schema_version: 1,
      status: "warning",
      severity: "medium",
      summary: "需要人工聆聽",
      metrics: { events: [{ start_seconds: 2.5, duration_seconds: 1 }] },
      evidence_artifact_ids: ["evidence-1"],
      threshold_source: "delivery-qa-v1",
      remediation: "人工聆聽後填寫理由",
    }],
    evidence_index: [{ artifact_id: "evidence-1", type: "event_strip", check_id: "audio", timestamp_seconds: 2.5 }],
    artifact_urls: { "evidence-1": "/api/evidence-1" },
    output_url: "/api/output",
    human_review: { status: "pending", review_version: 1, warning_acceptances: [] },
    sensitive_data_redacted: true,
    ...overrides,
  };
}

function controls() {
  return createProjectMutationControls(new ProjectMutationCoordinator());
}

describe("DeliveryQAWorkspace", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows findings, evidence and keeps confirmation disabled until preview and warning reasons are complete", () => {
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={state()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={controls()} />);

    expect(screen.getByText("需要人工聆聽")).toBeTruthy();
    expect(screen.getByAltText("audio evidence")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "確認可交付" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText("說明為何這是刻意的創作選擇"), { target: { value: "刻意保留旅行環境音" } });
    fireEvent.click(screen.getByLabelText("我已完整觀看正式成片，並確認 warning 接受理由"));
    expect(confirm.disabled).toBe(false);
  });

  it("never permits blocked or skipped checks to become deliverable ready", () => {
    const blocked = state({
      lifecycle_status: "qa_blocked",
      summary: { pass: 4, warning: 0, blocked: 1, skipped: 1 },
      checks: [{ ...state().checks[0], status: "blocked", summary: "decode failed" }],
    });
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={blocked} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={controls()} />);

    expect(screen.getByText(/blocked 或 skipped 不可直接接受/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "確認可交付" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("preserves an in-progress warning reason when polling refreshes the same QA run", () => {
    const props = { projectId: 1, setMessage: vi.fn(), refreshProject: vi.fn(async () => []), mutationControls: controls() };
    const view = render(<DeliveryQAWorkspace {...props} deliveryQA={state()} />);
    const reason = screen.getByPlaceholderText("說明為何這是刻意的創作選擇") as HTMLTextAreaElement;
    fireEvent.change(reason, { target: { value: "尚未送出的人工判斷" } });

    view.rerender(<DeliveryQAWorkspace {...props} deliveryQA={state({ summary: { pass: 4, warning: 1, blocked: 0, skipped: 0 } })} />);

    expect((screen.getByPlaceholderText("說明為何這是刻意的創作選擇") as HTMLTextAreaElement).value).toBe("尚未送出的人工判斷");
  });

  it.each(["refresh rejected", "refresh aborted"])("keeps review success authoritative when %s", async (caseName) => {
    const review = vi.spyOn(api, "reviewDeliveryQA").mockResolvedValue({ ok: true, delivery_qa: state({ lifecycle_status: "deliverable_ready", deliverable_ready: true }) });
    const refresh = vi.fn(async () => {
      throw caseName === "refresh aborted" ? new DOMException("aborted", "AbortError") : new Error("refresh failed");
    });
    const setMessage = vi.fn();
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={state()} setMessage={setMessage} refreshProject={refresh} mutationControls={controls()} />);
    fireEvent.change(screen.getByPlaceholderText("說明為何這是刻意的創作選擇"), { target: { value: "刻意保留旅行環境音" } });
    fireEvent.click(screen.getByLabelText("我已完整觀看正式成片，並確認 warning 接受理由"));
    fireEvent.click(screen.getByRole("button", { name: "確認可交付" }));

    await waitFor(() => expect(setMessage).toHaveBeenCalledWith(expect.stringContaining("已完成最終預覽確認，成片可交付")));
    expect(setMessage).not.toHaveBeenCalledWith(expect.stringContaining("交付 QA 操作失敗"));
    expect(review).toHaveBeenCalledTimes(1);
    expect(review).toHaveBeenCalledWith(1, "qa-run-1", "confirm", 1, "", { audio: "刻意保留旅行環境音" });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("reruns QA without creating another render call", async () => {
    const rerun = vi.spyOn(api, "rerunDeliveryQA").mockResolvedValue({ ok: true, delivery_qa: state() });
    const renderJob = vi.spyOn(api, "createRenderJob");
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={state()} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={controls()} />);

    fireEvent.click(screen.getByRole("button", { name: "重新檢查（不重送 Render）" }));
    await waitFor(() => expect(rerun).toHaveBeenCalledTimes(1));
    expect(renderJob).not.toHaveBeenCalled();
  });

  it("keeps rerun success authoritative when refresh aborts and never resends QA", async () => {
    const rerun = vi.spyOn(api, "rerunDeliveryQA").mockResolvedValue({ ok: true, delivery_qa: state() });
    const refresh = vi.fn(async () => { throw new DOMException("aborted", "AbortError"); });
    const setMessage = vi.fn();
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={state()} setMessage={setMessage} refreshProject={refresh} mutationControls={controls()} />);

    fireEvent.click(screen.getByRole("button", { name: "重新檢查（不重送 Render）" }));
    await waitFor(() => expect(setMessage).toHaveBeenCalledWith(expect.stringContaining("Delivery QA 已重新檢查")));
    expect(setMessage).not.toHaveBeenCalledWith(expect.stringContaining("重新檢查失敗"));
    expect(rerun).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("renders chapter contact sheets as visual evidence", () => {
    const withChapterEvidence = state({
      evidence_index: [{ artifact_id: "evidence-1", type: "chapter_contact_sheet", check_id: "audio", timestamp_seconds: 0 }],
    });
    render(<DeliveryQAWorkspace projectId={1} deliveryQA={withChapterEvidence} setMessage={vi.fn()} refreshProject={vi.fn(async () => [])} mutationControls={controls()} />);
    expect(screen.getByAltText("audio evidence")).toBeTruthy();
  });
});
