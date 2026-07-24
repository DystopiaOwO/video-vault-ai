import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RenderJobPanel } from "../src/components/render/RenderJobPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("RenderJobPanel manual refresh", () => {
  it("uses a throwing fresh load and reports the real refresh failure", async () => {
    const setMessage = vi.fn();
    const refreshProject = vi.fn().mockRejectedValue(new Error("GET failed"));
    render(
      <RenderJobPanel
        jobs={[]}
        projectId={7}
        setMessage={setMessage}
        refreshProject={refreshProject}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));

    await waitFor(() => expect(refreshProject).toHaveBeenCalledWith({
      forceFresh: true,
      throwOnError: true,
    }));
    await waitFor(() => expect(setMessage).toHaveBeenCalledWith("工作狀態更新失敗：GET failed"));
    expect(setMessage).not.toHaveBeenCalledWith("工作狀態已更新。");
  });
});
