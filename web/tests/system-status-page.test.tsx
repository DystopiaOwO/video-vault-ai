import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { api, type DoctorReport } from "../src/api";
import { SystemStatusPage } from "../src/pages/SystemStatusPage";

const report: DoctorReport = {
  schema_version: "doctor-v1",
  mode: "quick",
  generated_at: "2026-08-09T00:00:00Z",
  status: "warning",
  ok: true,
  summary: { pass: 2, warning: 1, blocked: 0, skipped: 3 },
  sensitive_data_redacted: true,
  checks: [
    { check_id: "runtime.python", category: "runtime", status: "pass", summary: "Python pass", evidence: {} },
    { check_id: "provider.active", category: "provider", status: "warning", summary: "local offline", remediation: "啟動 provider", evidence: {} },
  ],
};

describe("SystemStatusPage", () => {
  it("consumes the structured doctor report and exposes rerun modes", async () => {
    const doctor = vi.spyOn(api, "doctor").mockResolvedValue(report);
    render(<SystemStatusPage />);

    await waitFor(() => expect(doctor).toHaveBeenCalledWith("default"));
    expect(screen.getByRole("heading", { name: "本機環境健檢" })).toBeTruthy();
    expect(screen.getAllByText("警告").length).toBeGreaterThan(0);
    expect(screen.getByText("local offline")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "完整檢查" }));
    await waitFor(() => expect(doctor).toHaveBeenLastCalledWith("full"));
    doctor.mockRestore();
  });
});
