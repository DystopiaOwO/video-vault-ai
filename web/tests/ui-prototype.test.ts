import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const html = readFileSync(new URL("../ui-prototype.html", import.meta.url), "utf8");
const css = readFileSync(new URL("../ui-prototype.css", import.meta.url), "utf8");

describe("standalone UI prototype", () => {
  it("is explicitly marked as a non-production prototype", () => {
    expect(html).toContain('data-prototype="true"');
    expect(html).toContain("示範資料，不會寫入專案或呼叫正式 API");
    expect(html).not.toContain("<script");
  });

  it("provides four CSS-switchable workspaces", () => {
    expect(html.match(/name="workspace"/g)).toHaveLength(4);
    expect(html).toContain("workspace-dashboard");
    expect(html).toContain("workspace-storyboard");
    expect(html).toContain("workspace-tuning");
    expect(html).toContain("workspace-output");
    expect(css).toContain("#view-storyboard:checked");
    expect(css).toContain("#view-tuning:checked");
  });

  it("uses one storyboard editor with a dedicated inspector", () => {
    expect(html.match(/class="segment-inspector"/g)).toHaveLength(1);
    expect(html).toContain("segment-browser");
    expect(html).not.toContain("SegmentTable");
  });

  it("does not expose the previously identified fake product controls", () => {
    expect(html).not.toContain("1.42 TB");
    expect(html).not.toContain("升級方案");
    expect(html).not.toContain("⌘ K");
    expect(html).not.toContain("專案健康度");
  });

  it("includes desktop, tablet, and mobile layout rules", () => {
    expect(css).toContain("@media (max-width: 1180px)");
    expect(css).toContain("@media (max-width: 900px)");
    expect(css).toContain("@media (max-width: 680px)");
    expect(css).toContain("@media (max-width: 430px)");
    expect(css).toContain("position: sticky");
  });
});
