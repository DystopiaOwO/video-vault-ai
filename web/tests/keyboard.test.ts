import { describe, expect, it } from "vitest";
import { isCommittedEnter } from "../src/keyboard";

describe("IME-safe Enter", () => {
  it("accepts a normal Enter after composition is complete", () => {
    expect(isCommittedEnter({ key: "Enter" })).toBe(true);
  });

  it("ignores React composition state", () => {
    expect(isCommittedEnter({ key: "Enter", isComposing: true })).toBe(false);
  });

  it("ignores native composition state", () => {
    expect(isCommittedEnter({ key: "Enter", nativeEvent: { isComposing: true } })).toBe(false);
  });

  it("ignores the IME keyCode 229 confirmation event", () => {
    expect(isCommittedEnter({ key: "Enter", nativeEvent: { keyCode: 229 } })).toBe(false);
  });

  it("does not treat another key as Enter", () => {
    expect(isCommittedEnter({ key: "Space" })).toBe(false);
  });
});
