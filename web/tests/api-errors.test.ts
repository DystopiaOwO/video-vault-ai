import { describe, expect, it } from "vitest";
import { ApiError, formatApiError } from "../src/api";

describe("formatApiError conflict messages", () => {
  it.each([
    ["stale_creator_profile", { profile_version: 4 }, "Creator Profile 目前 version 4"],
    ["stale_story_settings", { profile_version: 6 }, "Story Settings 目前 version 6"],
    ["stale_project_revision", { project_revision: 9 }, "目前 project revision 9"],
  ])("distinguishes %s", (code, payload, expected) => {
    expect(formatApiError(new ApiError(409, { code, ...payload }))).toContain(expected);
  });

  it("does not infer Story Settings from an unknown profile-version conflict", () => {
    expect(formatApiError(new ApiError(409, { profile_version: 4 }))).not.toContain("Story Settings");
  });
});
