import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { UiPrototypeApp } from "../src/prototype/UiPrototypeApp";

afterEach(cleanup);

describe("React UI prototype", () => {
  it("switches between the main workspaces", () => {
    render(<UiPrototypeApp />);
    expect(screen.getByText("目前剪輯狀態")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /分鏡審核/ }));
    expect(screen.getByText("分鏡片段")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /調色與音訊/ }));
    expect(screen.getByText("SHORT PREVIEW")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /核准與輸出/ }));
    expect(screen.getByText("輸出前檢查")).toBeTruthy();
  });

  it("selecting a segment updates the single inspector", () => {
    render(<UiPrototypeApp />);
    fireEvent.click(screen.getByRole("button", { name: /分鏡審核/ }));
    fireEvent.click(screen.getByRole("button", { name: /巷弄散步與街景/ }));

    expect((screen.getByLabelText("片段標題") as HTMLInputElement).value).toBe("巷弄散步與街景");
    expect((screen.getByLabelText("片段速度") as HTMLSelectElement).value).toBe("1.15");
    expect(screen.getAllByText("降低原音").length).toBeGreaterThan(0);
  });

  it("include and exclude state updates the project totals", () => {
    render(<UiPrototypeApp />);
    fireEvent.click(screen.getByRole("button", { name: /分鏡審核/ }));
    expect(screen.getByText("納入 3 段")).toBeTruthy();

    const included = screen.getByLabelText("納入成片") as HTMLInputElement;
    expect(included.checked).toBe(true);
    fireEvent.click(included);
    expect(screen.getByText("納入 2 段")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /儀表板/ }));
    expect(screen.getByText("2", { selector: ".ux-metric strong" })).toBeTruthy();
  });

  it("requires human confirmations before the render gate unlocks", () => {
    render(<UiPrototypeApp />);
    fireEvent.click(screen.getByRole("button", { name: /核准與輸出/ }));

    const renderButton = screen.getByRole("button", { name: "建立正式輸出工作" }) as HTMLButtonElement;
    expect(renderButton.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "核准目前版本" }));
    expect(renderButton.disabled).toBe(true);
    expect(screen.getByText(/請先完成兩項人工確認/)).toBeTruthy();

    fireEvent.click(screen.getByLabelText(/我已確認分鏡、剪點與片段順序/));
    fireEvent.click(screen.getByLabelText(/我已確認 BGM 授權與輸出設定/));
    fireEvent.click(screen.getByRole("button", { name: "核准目前版本" }));

    expect((screen.getByRole("button", { name: "建立正式輸出工作" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getAllByText("已核准").length).toBeGreaterThan(0);
  });

  it("switches preview and tuning tabs without duplicate editors", () => {
    render(<UiPrototypeApp />);
    fireEvent.click(screen.getByRole("button", { name: /調色與音訊/ }));

    fireEvent.click(screen.getByRole("tab", { name: "Before" }));
    expect(screen.getByText("原始 D-Log 畫面")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "音訊" }));
    expect(screen.getByText("Audio Mixing")).toBeTruthy();
    expect(screen.queryByText("Color Consistency")).toBeNull();
  });

  it("does not show fake storage, upgrade, or global search controls", () => {
    render(<UiPrototypeApp />);
    expect(screen.queryByText(/1.42 TB/)).toBeNull();
    expect(screen.queryByText(/升級方案/)).toBeNull();
    expect(screen.queryByPlaceholderText(/搜尋素材/)).toBeNull();
  });
});
