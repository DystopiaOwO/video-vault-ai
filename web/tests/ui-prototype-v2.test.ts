import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { describe, expect, it } from "vitest";

const html = readFileSync(new URL("../ui-prototype-v2.html", import.meta.url), "utf8");
const css = readFileSync(new URL("../ui-prototype-v2.css", import.meta.url), "utf8");
const script = readFileSync(new URL("../ui-prototype-v2.js", import.meta.url), "utf8");

function prototypeDom() {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://127.0.0.1:5173/ui-prototype-v2.html" });
  Object.defineProperty(dom.window, "scrollTo", { value: () => undefined, configurable: true });
  dom.window.eval(script);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded", { bubbles: true }));
  return dom;
}

describe("interactive UI prototype v2", () => {
  it("contains real workspaces without decorative fake product controls", () => {
    expect(html).toContain('data-workspace="dashboard"');
    expect(html).toContain('data-workspace="storyboard"');
    expect(html).toContain('data-workspace="tuning"');
    expect(html).toContain('data-workspace="output"');
    expect(html).not.toContain("升級方案");
    expect(html).not.toContain("1.42 TB");
    expect(html).not.toContain("⌘ K");
  });

  it("switches workspaces from the navigation", () => {
    const dom = prototypeDom();
    const document = dom.window.document;
    const storyboardButton = document.querySelector<HTMLButtonElement>('[data-workspace-target="storyboard"]');
    storyboardButton?.click();
    expect(document.querySelector<HTMLElement>('[data-workspace="dashboard"]')?.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>('[data-workspace="storyboard"]')?.hidden).toBe(false);
    expect(document.querySelector('.nav-button[data-workspace-target="storyboard"]')?.classList.contains("active")).toBe(true);
  });

  it("selects one segment and updates the inspector", () => {
    const dom = prototypeDom();
    const document = dom.window.document;
    const ramen = document.querySelector<HTMLButtonElement>('[data-segment-id="ramen"]');
    ramen?.click();
    expect(document.querySelector('[data-inspector-title]')?.textContent).toBe("拉麵上桌");
    expect(document.querySelector('[data-inspector-source]')?.textContent).toBe("DJI_20260709_123522.MP4");
    expect((document.querySelector('[data-included-toggle]') as HTMLInputElement).checked).toBe(true);
    expect(document.querySelectorAll('.segment-row.selected')).toHaveLength(1);
  });

  it("updates included state and project totals from the inspector", () => {
    const dom = prototypeDom();
    const document = dom.window.document;
    const included = document.querySelector<HTMLInputElement>('[data-included-toggle]')!;
    included.checked = false;
    included.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    expect(document.querySelector('[data-segment-id="station"]')?.classList.contains("excluded")).toBe(true);
    expect(document.querySelector('[data-included-total]')?.textContent).toBe("4");
    expect(document.querySelector('[data-unsaved]')?.hasAttribute("hidden")).toBe(false);
  });

  it("keeps formal render locked until both confirmations and approval", () => {
    const dom = prototypeDom();
    const document = dom.window.document;
    const checks = Array.from(document.querySelectorAll<HTMLInputElement>('[data-gate-check]'));
    const approve = document.querySelector<HTMLButtonElement>('[data-approve-button]')!;
    const render = document.querySelector<HTMLButtonElement>('[data-render-button]')!;
    expect(approve.disabled).toBe(true);
    expect(render.disabled).toBe(true);
    checks.forEach((check) => {
      check.checked = true;
      check.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    });
    expect(approve.disabled).toBe(false);
    approve.click();
    expect(render.disabled).toBe(false);
    expect(document.querySelector('[data-approval-title]')?.textContent).toBe("目前版本已核准");
  });

  it("defines desktop and mobile responsive layouts", () => {
    expect(css).toContain("@media (max-width: 1180px)");
    expect(css).toContain("@media (max-width: 900px)");
    expect(css).toContain("@media (max-width: 680px)");
    expect(css).toContain("@media (max-width: 430px)");
    expect(css).toContain("grid-template-columns: minmax(0,1fr) 370px");
  });
});
