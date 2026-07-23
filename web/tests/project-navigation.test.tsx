import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type ProjectDetail } from "../src/api";
import { App } from "../src/main";

function detail(projectId: number): ProjectDetail {
  return {
    project: { id: projectId, name: projectId === 7 ? "福岡旅行" : "手沖日記", status: projectId === 7 ? "approved" : "needs_review" },
    clips: [],
    segments: [],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "review", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: projectId === 7,
    render_gate_reason: projectId === 7 ? "" : "待核准",
  };
}

function setup() {
  vi.spyOn(api, "projects").mockResolvedValue([
    { id: 7, name: "福岡旅行", status: "approved", video_count: 3 },
    { id: 8, name: "手沖日記", status: "needs_review", video_count: 2 },
  ]);
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "project").mockImplementation(async (projectId) => detail(projectId));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("project navigation", () => {
  it("filters projects by name, status, and id and can clear an empty result", async () => {
    setup();
    render(<App />);
    await screen.findByRole("heading", { name: "福岡旅行" });

    fireEvent.change(screen.getByLabelText("搜尋專案"), { target: { value: "手沖" } });
    expect(screen.queryByRole("button", { name: /福岡旅行/ })).toBeNull();
    expect(screen.getByRole("button", { name: /手沖日記/ })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋專案"), { target: { value: "approved" } });
    expect(screen.getByRole("button", { name: /福岡旅行/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /手沖日記/ })).toBeNull();

    fireEvent.change(screen.getByLabelText("搜尋專案"), { target: { value: "404" } });
    expect(screen.getByText("找不到符合的專案")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "清除搜尋" }));
    expect(screen.getByRole("button", { name: /福岡旅行/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /手沖日記/ })).toBeTruthy();
  });

  it("prevents blank and duplicate project names before calling the API", async () => {
    setup();
    const create = vi.spyOn(api, "createProject");
    render(<App />);
    await screen.findByRole("heading", { name: "福岡旅行" });

    const input = screen.getByLabelText("建立專案");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByText("請先輸入專案名稱。")).toBeTruthy();
    expect(create).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "  福岡旅行  " } });
    fireEvent.click(screen.getByRole("button", { name: "新增專案" }));
    expect(await screen.findByText("已有同名專案，請使用不同名稱。")).toBeTruthy();
    expect(create).not.toHaveBeenCalled();
  });

  it("switches projects through the filtered list and exposes workspace anchors", async () => {
    setup();
    render(<App />);
    await screen.findByRole("heading", { name: "福岡旅行" });

    fireEvent.click(screen.getByRole("button", { name: /手沖日記/ }));
    await screen.findByRole("heading", { name: "手沖日記" });

    expect(screen.getByRole("navigation", { name: "專案工作區導覽" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "分鏡" }).getAttribute("href")).toBe("#workspace-storyboard");
    expect(screen.getByText("待核准")).toBeTruthy();
  });
});
