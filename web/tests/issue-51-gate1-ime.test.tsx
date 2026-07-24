import { cleanup, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type AudioState, type ProjectDetail } from "../src/api";
import { App } from "../src/main";

function detail(): ProjectDetail {
  const adjustment = {
    mode: "none",
    lut_path: "",
    lut_kind: "",
    exposure: 0,
    temperature: 0,
    tint: 0,
    contrast: 1,
    saturation: 1,
    gamma: 1,
    highlights: 0,
    shadows: 0,
  };
  const audio: AudioState = {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1, fade_out_seconds: 1 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: {},
  };
  return {
    project: { id: 1, name: "IME 測試專案", status: "needs_review" },
    clips: [],
    segments: [],
    bgm: [],
    plan: {},
    workflow: { style: "test", current: "review", stages: [] },
    review: {},
    script: "",
    folder: "",
    can_render: true,
    render_gate_reason: "",
    color: { schema_version: 2, enabled: true, reference: {}, references: [], analysis: {}, suggested: adjustment, applied: adjustment, segments: {} },
    audio,
    storyboard: { schema_version: 1, exists: false, groups: [], segments: {} },
  };
}

function setup() {
  vi.spyOn(api, "projects").mockResolvedValue([{ id: 1, name: "IME 測試專案", status: "needs_review", video_count: 0 }]);
  vi.spyOn(api, "bgm").mockResolvedValue([]);
  vi.spyOn(api, "jobs").mockResolvedValue([]);
  vi.spyOn(api, "project").mockResolvedValue(detail());
}

async function openApp() {
  render(<App />);
  await screen.findByRole("heading", { name: "IME 測試專案" });
}

function input() {
  return screen.getByLabelText("建立專案") as HTMLInputElement;
}

function nativeKeyDown(target: HTMLInputElement, init: KeyboardEventInit, properties: Record<string, unknown> = {}) {
  const event = createEvent.keyDown(target, init);
  Object.entries(properties).forEach(([key, value]) => Object.defineProperty(event, key, { configurable: true, value }));
  fireEvent(target, event);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Issue #51 Gate 1 IME Enter 行為", () => {
  it("event.isComposing 為 true 時按 Enter 不建立專案", async () => {
    setup();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ok: true, id: 2 });
    await openApp();
    fireEvent.change(input(), { target: { value: "尚在組字" } });

    nativeKeyDown(input(), { key: "Enter", code: "Enter" }, { isComposing: true });
    await waitFor(() => expect(create).not.toHaveBeenCalled());
  });

  it("nativeEvent.isComposing 為 true 時按 Enter 不建立專案", async () => {
    setup();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ok: true, id: 2 });
    await openApp();
    fireEvent.change(input(), { target: { value: "注音組字中" } });

    nativeKeyDown(input(), { key: "Enter", code: "Enter" }, { isComposing: true });
    await waitFor(() => expect(create).not.toHaveBeenCalled());
  });

  it("nativeEvent.keyCode 229 時按 Enter 不建立專案", async () => {
    setup();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ok: true, id: 2 });
    await openApp();
    fireEvent.change(input(), { target: { value: "倉頡組字中" } });

    nativeKeyDown(input(), { key: "Enter", code: "Enter" }, { keyCode: 229 });
    await waitFor(() => expect(create).not.toHaveBeenCalled());
  });

  it("composition 結束後按 Enter 可以正常建立一次", async () => {
    setup();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ok: true, id: 2 });
    await openApp();
    fireEvent.change(input(), { target: { value: "完整中文名稱" } });

    fireEvent.compositionStart(input());
    fireEvent.compositionEnd(input());
    nativeKeyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 }, { isComposing: false, keyCode: 13 });
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
  });

  it("一般英文輸入按 Enter 仍可建立專案", async () => {
    setup();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ok: true, id: 2 });
    await openApp();
    fireEvent.change(input(), { target: { value: "English project" } });

    nativeKeyDown(input(), { key: "Enter", code: "Enter", keyCode: 13 }, { isComposing: false, keyCode: 13 });
    await waitFor(() => expect(create).toHaveBeenCalledWith("English project"));
  });
});
