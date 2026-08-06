import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MultiFrameEvidencePanel } from "../src/components/project/MultiFrameEvidencePanel";

describe("MultiFrameEvidencePanel", () => {
  it("renders inspectable evidence and a manual review entry point", () => {
    render(<MultiFrameEvidencePanel clip={{
      filename: "travel.mp4",
      perception_run: {
        current_status: "succeeded",
        current_window_validation: { status: "pass" },
        multi_frame_contract: { provider: "mock", model: "rules" },
        current_window_results: [{
          window_uuid: "window_1",
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
    expect(screen.getByAltText("車站入口連續畫面 contact sheet")).toBeTruthy();
    expect(screen.getByRole("link", { name: "前往分鏡審核修改" }).getAttribute("href")).toBe("#workspace-storyboard");
    expect(screen.getByRole("link", { name: "查看視窗 JSON" }).getAttribute("href")).toBe("/window.json");
  });
});
