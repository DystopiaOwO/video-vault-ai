import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PerceptionSamplingControls } from "../src/components/project/PerceptionSamplingControls";

describe("PerceptionSamplingControls", () => {
  it("shows auditable run statistics and submits a per-clip override", () => {
    const onAnalyze = vi.fn();
    render(<PerceptionSamplingControls
      clip={{
        filename: "coffee.mp4",
        duration_seconds: 60,
        sampling: {
          default_policy: {
            name: "adaptive-balanced",
            version: 1,
            mode: "adaptive",
            preset: "balanced",
            baseline_interval_seconds: 5,
            max_frames_per_clip: 180,
            max_frames_per_minute: 30,
          },
          estimated_frame_count: 14,
          current: {
            policy: {
              name: "adaptive-balanced",
              version: 1,
              mode: "adaptive",
              preset: "balanced",
              baseline_interval_seconds: 5,
              max_frames_per_clip: 180,
              max_frames_per_minute: 30,
            },
            samples: [{ timestamp_seconds: 0, reasons: ["boundary"] }],
            actual_vision_calls: 1,
            cache_hits: 2,
            sample_reason_counts: { baseline: 1, scene: 2, motion: 3, boundary: 2 },
            visual_dedupe: { status: "applied", removed: 4 },
          },
        },
      }}
      onAnalyze={onAnalyze}
    />);

    expect(screen.getByText(/實際 1 張/)).toBeTruthy();
    expect(screen.getByText(/scene 2、motion 3/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("密度"), { target: { value: "dense" } });
    fireEvent.click(screen.getByRole("button", { name: "依此設定重跑" }));
    expect(onAnalyze).toHaveBeenCalledWith(expect.objectContaining({
      mode: "adaptive",
      preset: "dense",
      baseline_interval_seconds: 5,
      max_frames_per_clip: 180,
    }));
  });
});
