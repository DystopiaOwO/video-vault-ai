import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildWorkspaceCommands, WorkspaceCommandPalette } from "../src/components/WorkspaceCommandPalette";

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("WorkspaceCommandPalette", () => {
  it("opens with Ctrl+K and filters commands", async () => {
    render(<WorkspaceCommandPalette />);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(await screen.findByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋命令"), { target: { value: "音訊" } });
    expect(screen.getByRole("option", { name: /前往音訊/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /前往分鏡/ })).toBeNull();
  });

  it("supports keyboard selection, execution, and Escape", async () => {
    const audio = document.createElement("section");
    audio.id = "workspace-audio";
    audio.tabIndex = -1;
    audio.scrollIntoView = vi.fn();
    document.body.appendChild(audio);
    render(<WorkspaceCommandPalette />);

    fireEvent.click(screen.getByRole("button", { name: "開啟命令面板" }));
    await screen.findByRole("dialog", { name: "工作區命令面板" });
    fireEvent.change(screen.getByLabelText("搜尋命令"), { target: { value: "音訊" } });
    fireEvent.keyDown(window, { key: "Enter" });

    await waitFor(() => expect(audio.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" }));
    expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull();

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull();
  });

  it("moves through results with arrow keys", async () => {
    render(<WorkspaceCommandPalette />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    await screen.findByRole("dialog", { name: "工作區命令面板" });

    const options = screen.getAllByRole("option");
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(window, { key: "ArrowDown" });
    expect(options[1].getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(window, { key: "ArrowUp" });
    expect(options[0].getAttribute("aria-selected")).toBe("true");
  });

  it("builds a project-search command that focuses the existing field", () => {
    document.body.innerHTML = '<input aria-label="搜尋專案" />';
    const input = document.querySelector("input") as HTMLInputElement;
    const close = vi.fn();
    const commands = buildWorkspaceCommands(close);

    commands.find((command) => command.id === "project-search")?.run();
    window.dispatchEvent(new Event("resize"));

    expect(close).toHaveBeenCalledOnce();
    return new Promise<void>((resolve) => window.requestAnimationFrame(() => {
      expect(document.activeElement).toBe(input);
      resolve();
    }));
  });
});
