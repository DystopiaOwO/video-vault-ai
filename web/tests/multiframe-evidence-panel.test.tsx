import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api } from "../src/api";
import { MultiFrameEvidencePanel } from "../src/components/project/MultiFrameEvidencePanel";

describe("MultiFrameEvidencePanel", () => {
  it("renders inspectable evidence and a manual review entry point", () => {
    render(<MultiFrameEvidencePanel clip={{
      filename: "travel.mp4",
      perception_run: {
        current_status: "succeeded",
        current_window_validation: { status: "pass" },
        current_cloud_review: { status: "failed", error: "timeout" },
        multi_frame_contract: { provider: "mock", model: "rules" },
        current_window_results: [{
          window_uuid: "window_1",
          segment_uuid: "segment_1",
          ordinal: 1,
          start_seconds: 0,
          end_seconds: 4,
          frame_timestamps: [0, 2, 4],
          summary: "車站入口連續畫面",
          action: "走入車站",
          shot_role: "context",
          confidence: 0.9,
          technical_quality: { score: 0.8, issues: [] },
          natural_audio_recommendation: "keep",
          evidence_urls: {
            contact_sheet: "/evidence.jpg",
            window: "/window.json",
            normalized: "/normalized.json",
          },
        }],
      },
    }} />);

    expect(screen.getByText("車站入口連續畫面")).toBeTruthy();
    expect(screen.getByText(/已保留本地結果/)).toBeTruthy();
    expect(screen.getByAltText("車站入口連續畫面 contact sheet")).toBeTruthy();
    expect(screen.getByRole("link", { name: "前往分鏡審核修改" }).getAttribute("href")).toBe("#workspace-storyboard");
    expect(screen.getByRole("link", { name: "查看視窗 JSON" }).getAttribute("href")).toBe("/window.json");
  });

  it("saves a manual evidence correction by stable segment UUID", async () => {
    const save = vi.spyOn(api, "saveSegmentEvidence").mockResolvedValue({ ok: true, path: "segment_review.json" });
    render(<MultiFrameEvidencePanel clip={{
      filename: "travel.mp4",
      perception_run: {
        current_status: "succeeded",
        current_window_validation: { status: "pass" },
        current_window_results: [{ window_uuid: "window_1", segment_uuid: "segment_1", ordinal: 1, summary: "畫面" }],
      },
    }} projectId={7} projectRevision={3} />);

    const actionInput = screen.getAllByLabelText("動作").at(-1);
    if (!actionInput) throw new Error("action input not found");
    fireEvent.change(actionInput, { target: { value: "走路" } });
    const saveButton = screen.getAllByRole("button", { name: "儲存人工修正" }).at(-1);
    if (!saveButton) throw new Error("save button not found");
    fireEvent.click(saveButton);
    await waitFor(() => expect(save).toHaveBeenCalledWith(7, "segment_1", expect.objectContaining({ action: "走路" }), 3));
    save.mockRestore();
  });
});
