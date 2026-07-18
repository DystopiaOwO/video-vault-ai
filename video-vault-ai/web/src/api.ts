export type Segment = {
  segment_id: string; clip_id: string; title: string;
  start_seconds: number; end_seconds: number; score: number;
  include: boolean; scene_role: string; suggested_use: string;
  speed: number; audio_role: string; user_notes: string;
};

export type Job = { kind: string; status: string; percent: number; message: string };

export type RenderSettings = {
  previewProfile: string; finalProfile: string; colorMode: string;
  audioCrossfade: number; bgmVolume: number; overlayEnabled: boolean; encoder: string;
};

export type RenderOutput = { label: string; path: string; kind: string };

export type Project = { name: string; status: "needs_review" | "approved"; canRender: boolean };

export const mockProject: Project = { name: "Render v2 UI 示範專案", status: "needs_review", canRender: false };
export const mockSegments: Segment[] = [
  { segment_id: "seg_001", clip_id: "clip_001", title: "抵達南港車站", start_seconds: 0, end_seconds: 12.37, score: .92, include: true, scene_role: "開場", suggested_use: "建立地點", speed: 1, audio_role: "keep_original", user_notes: "" },
  { segment_id: "seg_002", clip_id: "clip_002", title: "咖啡廳內部畫面", start_seconds: 5.125, end_seconds: 19.5, score: .87, include: true, scene_role: "細節", suggested_use: "氣氛補畫面", speed: 1, audio_role: "lower_original", user_notes: "" }
];

export const mockJobs: Job[] = [{ kind: "rough_preview", status: "completed", percent: 100, message: "UI skeleton mock job" }];
