import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildWorkspaceCommands, WorkspaceCommandPalette } from "../src/components/WorkspaceCommandPalette";

beforeEach(() => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn(() => ({ matches: false })),
  });
  history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  document.body.style.overflow = "";
  history.replaceState(null, "", "/");
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
    expect(window.location.hash).toBe("#workspace-audio");
    expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull();

    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(await screen.findByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull();
  });

  it("does not execute the active command for an IME Enter event", async () => {
    const audio = document.createElement("section");
    audio.id = "workspace-audio";
    audio.tabIndex = -1;
    audio.scrollIntoView = vi.fn();
    document.body.appendChild(audio);
    render(<WorkspaceCommandPalette />);

    fireEvent.click(screen.getByRole("button", { name: "開啟命令面板" }));
    await screen.findByRole("dialog", { name: "工作區命令面板" });
    fireEvent.keyDown(window, { key: "Enter", isComposing: true });
    expect(screen.getByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();

    fireEvent.keyDown(window, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull());
  });

  it("does not execute the active command for native keyCode 229", async () => {
    const audio = document.createElement("section");
    audio.id = "workspace-audio";
    audio.tabIndex = -1;
    audio.scrollIntoView = vi.fn();
    document.body.appendChild(audio);
    render(<WorkspaceCommandPalette />);

    fireEvent.click(screen.getByRole("button", { name: "開啟命令面板" }));
    await screen.findByRole("dialog", { name: "工作區命令面板" });
    fireEvent.keyDown(window, { key: "Enter", keyCode: 229, which: 229 });

    expect(screen.getByRole("dialog", { name: "工作區命令面板" })).toBeTruthy();
    expect(audio.scrollIntoView).not.toHaveBeenCalled();
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

  it("locks background scrolling, traps focus, and restores the trigger", async () => {
    render(<WorkspaceCommandPalette />);
    const trigger = screen.getByRole("button", { name: "開啟命令面板" });
    trigger.focus();
    fireEvent.click(trigger);

    const input = await screen.findByLabelText("搜尋命令");
    await waitFor(() => expect(document.activeElement).toBe(input));
    expect(document.body.style.overflow).toBe("hidden");

    const options = screen.getAllByRole("option");
    const last = options[options.length - 1] as HTMLButtonElement;
    last.focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(input);

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(document.body.style.overflow).toBe("");
  });

  it("builds a project-search command that focuses and selects the existing field", async () => {
    document.body.innerHTML = '<input aria-label="搜尋專案" value="福岡" />';
    const input = document.querySelector("input") as HTMLInputElement;
    const close = vi.fn();
    const commands = buildWorkspaceCommands(close);

    commands.find((command) => command.id === "project-search")?.run();

    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
    expect(close).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
  });

  it("closes from the backdrop and exposes the shortcut on the trigger", async () => {
    render(<WorkspaceCommandPalette />);
    const trigger = screen.getByRole("button", { name: "開啟命令面板" });
    expect(trigger.getAttribute("aria-keyshortcuts")).toBe("Control+K Meta+K");

    fireEvent.click(trigger);
    const backdrop = (await screen.findByRole("dialog", { name: "工作區命令面板" })).parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop);
    expect(screen.queryByRole("dialog", { name: "工作區命令面板" })).toBeNull();
  });
});
