import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, type BgmTrack } from "../src/api";
import {
  BgmLibraryPage,
  filterBgmTracks,
  formatDuration,
  moodTokens,
  requiresAttribution,
  sortBgmTracks,
  trackCredit,
} from "../src/pages/BgmLibraryPage";

type Track = BgmTrack & { license_url?: string; attribution_required?: boolean | number };

function track(overrides: Partial<Track> = {}): Track {
  return {
    id: 1,
    title: "Morning Walk",
    artist: "Dystopia",
    source_url: "https://example.com/source",
    license_name: "Creative Commons",
    license_url: "https://example.com/license",
    attribution_text: '"Morning Walk" by Dystopia',
    attribution_required: true,
    mood: "travel, chill",
    duration_seconds: 125,
    ...overrides,
  };
}

const tracks: Track[] = [
  track(),
  track({ id: 2, title: "Coffee Steam", artist: "Local Artist", mood: "coffee / cozy", attribution_required: false, license_name: "Royalty Free", duration_seconds: 65 }),
  track({ id: 3, title: "Untitled License", artist: "", mood: "ambient", attribution_required: false, license_name: "", attribution_text: "", source_url: "", duration_seconds: 360 }),
];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
});

describe("BGM library helpers", () => {
  it("normalizes mood tags and attribution flags", () => {
    expect(moodTokens(track({ mood: "travel, chill / bright、vlog" }))).toEqual(["travel", "chill", "bright", "vlog"]);
    expect(requiresAttribution(track({ attribution_required: 1 }))).toBe(true);
    expect(requiresAttribution(track({ attribution_required: 0 }))).toBe(false);
  });

  it("builds a fallback credit and formats duration", () => {
    expect(trackCredit(track({ attribution_text: "", title: "Night", artist: "A", license_name: "CC BY", source_url: "https://example.com" })))
      .toBe('"Night" by A (CC BY) https://example.com');
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3661)).toBe("1:01:01");
  });

  it("filters by query, mood, and license state", () => {
    expect(filterBgmTracks(tracks, "coffee", "", "all").map((item) => item.id)).toEqual([2]);
    expect(filterBgmTracks(tracks, "", "chill", "all").map((item) => item.id)).toEqual([1]);
    expect(filterBgmTracks(tracks, "", "", "attribution").map((item) => item.id)).toEqual([1]);
    expect(filterBgmTracks(tracks, "", "", "no-attribution").map((item) => item.id)).toEqual([2]);
    expect(filterBgmTracks(tracks, "", "", "missing").map((item) => item.id)).toEqual([3]);
  });

  it("sorts by duration, artist, and title without mutating input", () => {
    const original = [...tracks];
    expect(sortBgmTracks(tracks, "duration-desc").map((item) => item.id)).toEqual([3, 1, 2]);
    expect(sortBgmTracks(tracks, "artist").map((item) => item.id)).toEqual([1, 2, 3]);
    expect(sortBgmTracks(tracks, "title").map((item) => item.id)).toEqual([2, 1, 3]);
    expect(tracks).toEqual(original);
  });
});

describe("BgmLibraryPage", () => {
  it("loads tracks and supports combined search and filters", async () => {
    vi.spyOn(api, "bgm").mockResolvedValue(tracks);
    render(<BgmLibraryPage />);

    await waitFor(() => expect(screen.getByText("Morning Walk")).toBeTruthy());
    expect(screen.getByText("3 / 3 首曲目")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜尋 BGM"), { target: { value: "coffee" } });
    expect(screen.getByText("Coffee Steam")).toBeTruthy();
    expect(screen.queryByText("Morning Walk")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "清除篩選" }));
    fireEvent.change(screen.getByLabelText("篩選 BGM 情緒"), { target: { value: "chill" } });
    expect(screen.getByText("Morning Walk")).toBeTruthy();
    expect(screen.queryByText("Coffee Steam")).toBeNull();
  });

  it("copies attribution text from a track card", async () => {
    vi.spyOn(api, "bgm").mockResolvedValue([tracks[0]]);
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<BgmLibraryPage />);
    await waitFor(() => expect(screen.getByText("Morning Walk")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "複製署名" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('"Morning Walk" by Dystopia'));
    expect(screen.getByText("「Morning Walk」署名文字已複製。")).toBeTruthy();
  });

  it("shows no-result state and clears all filters", async () => {
    vi.spyOn(api, "bgm").mockResolvedValue(tracks);
    render(<BgmLibraryPage />);
    await waitFor(() => expect(screen.getByText("Morning Walk")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("搜尋 BGM"), { target: { value: "不存在的曲目" } });
    expect(screen.getByText("找不到符合的曲目")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "清除篩選" }));
    expect(screen.getByText("Morning Walk")).toBeTruthy();
  });

  it("shows a retryable error state", async () => {
    const load = vi.spyOn(api, "bgm")
      .mockRejectedValueOnce(new Error("database unavailable"))
      .mockResolvedValueOnce([tracks[0]]);
    render(<BgmLibraryPage />);

    await waitFor(() => expect(screen.getByText("BGM 載入失敗")).toBeTruthy());
    expect(screen.getByText("database unavailable")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重試" }));
    await waitFor(() => expect(screen.getByText("Morning Walk")).toBeTruthy());
    expect(load).toHaveBeenCalledTimes(2);
  });
});
