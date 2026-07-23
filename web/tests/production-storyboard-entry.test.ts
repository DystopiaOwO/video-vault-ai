import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  fileURLToPath(new URL("../src/main.tsx", import.meta.url)),
  "utf8",
);

describe("production storyboard entry", () => {
  it("mounts the new controller exactly once", () => {
    expect(source).toContain('import { StoryboardWorkspaceController } from "./workspaces/storyboard/StoryboardWorkspaceController";');
    expect(source.match(/<StoryboardWorkspaceController\b/g)).toHaveLength(1);
  });

  it("does not render or define the duplicate legacy storyboard editors", () => {
    expect(source).not.toContain("<StoryboardPanel");
    expect(source).not.toContain("function StoryboardPanel");
    expect(source).not.toContain("<SegmentTable");
    expect(source).not.toContain("function SegmentTable");
  });
});
