import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("consumes the structured doctor report and exposes rerun modes", async () => {
    cleanup();
    const doctor = vi.spyOn(api, "doctor").mockResolvedValue(report);
    render(<SystemStatusPage />);

    await waitFor(() => expect(doctor).toHaveBeenCalledWith("default", undefined));
    expect(screen.getByRole("heading", { name: "本機環境健檢" })).toBeTruthy();
    expect(screen.getAllByText("警告").length).toBeGreaterThan(0);
    expect(screen.getByText("local offline")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "完整檢查" }));
    await waitFor(() => expect(doctor).toHaveBeenLastCalledWith("full", undefined));
    doctor.mockRestore();
  });

  it("reruns one check through the shared API contract and merges the result", async () => {
    cleanup();
    const updated = { ...report, generated_at: "2026-08-09T00:01:00Z", checks: [report.checks[1]] };
    const doctor = vi.spyOn(api, "doctor").mockResolvedValueOnce(report).mockResolvedValueOnce(updated);
    render(<SystemStatusPage />);

    await waitFor(() => expect(doctor).toHaveBeenCalledWith("default", undefined));
    const rerunButtons = screen.getAllByRole("button", { name: "重新檢查" });
    fireEvent.click(rerunButtons[rerunButtons.length - 1]);
    await waitFor(() => expect(doctor).toHaveBeenLastCalledWith("default", "provider.active"));
    expect(screen.getByText("provider.active")).toBeTruthy();
    doctor.mockRestore();
  });
});
