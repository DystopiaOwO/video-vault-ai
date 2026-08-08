import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectLocation } from "../src/components/project/ProjectLocation";

const originalClipboard = navigator.clipboard;

afterEach(() => {
  cleanup();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: originalClipboard });
  vi.restoreAllMocks();
});

describe("ProjectLocation", () => {
  it("keeps the full local path hidden until requested", () => {
    render(<ProjectLocation projectId={7} folder="D:/Video Vault/福岡旅行" setMessage={vi.fn()} />);

    expect(screen.getByText("專案 #7")).toBeTruthy();
    expect(screen.queryByText("D:/Video Vault/福岡旅行")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "顯示資料夾" }));
    expect(screen.getByText("D:/Video Vault/福岡旅行")).toBeTruthy();
    expect(screen.getByRole("button", { name: "隱藏資料夾" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("copies the folder path without exposing it in the hero", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const setMessage = vi.fn();
    render(<ProjectLocation projectId={7} folder="D:/Video Vault/福岡旅行" setMessage={setMessage} />);

    fireEvent.click(screen.getByRole("button", { name: "複製路徑" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("D:/Video Vault/福岡旅行"));
    expect(setMessage).toHaveBeenCalledWith("專案資料夾路徑已複製。");
    expect(screen.queryByText("D:/Video Vault/福岡旅行")).toBeNull();
  });

  it("does not render folder actions when no folder is available", () => {
    render(<ProjectLocation projectId={8} folder="" setMessage={vi.fn()} />);

    expect(screen.getByText("專案 #8")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("does not crash when the API omits the local folder path", () => {
    render(<ProjectLocation projectId={9} setMessage={vi.fn()} />);

    expect(screen.getByText("專案 #9")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
