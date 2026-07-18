export type Segment = {
  segment_id: string;
  clip_id: string;
  title: string;
  source_file?: string;
  source_url?: string;
  start_seconds: number;
  end_seconds: number;
  score: number;
  include: boolean;
  scene_role: string;
  suggested_use: string;
  speed: number;
  audio_role: string;
  user_notes: string;
};

export type Job = {
  job_id?: string;
  kind: string;
  status: string;
  stage?: string;
  percent: number;
  current_segment?: string;
  total_segments?: number;
  cache_hit?: number;
  cache_miss?: number;
  encoder?: string;
  output?: string;
  error?: string;
  message: string;
  log_path?: string;
};

export type RenderSettings = {
  previewProfile: string;
  finalProfile: string;
  colorMode: string;
  audioCrossfade: number;
  bgmVolume: number;
  bgmFadeIn: number;
  bgmFadeOut: number;
  overlayEnabled: boolean;
  encoder: string;
};

export type RenderOutput = { label: string; path: string; kind: string };
export type Project = { name: string; status: "needs_review" | "approved"; canRender: boolean; gateReason?: string };

export const mockProject: Project = { name: "Render v2 UI 示範專案", status: "needs_review", canRender: false, gateReason: "專案尚未核准，正式輸出前需要完成片段審核。" };
export const mockSegments: Segment[] = [
  { segment_id: "seg_001", clip_id: "clip_001", title: "抵達南港車站", source_file: "南港車站.mp4", start_seconds: 0, end_seconds: 12.37, score: .92, include: true, scene_role: "開場", suggested_use: "建立地點", speed: 1, audio_role: "keep_original", user_notes: "" },
  { segment_id: "seg_002", clip_id: "clip_002", title: "咖啡廳內部畫面", source_file: "咖啡廳.mp4", start_seconds: 5.125, end_seconds: 19.5, score: .87, include: true, scene_role: "細節", suggested_use: "氣氛補畫面", speed: 1, audio_role: "lower_original", user_notes: "" }
];

export const mockJobs: Job[] = [{ job_id: "job_demo", kind: "rough_preview", status: "completed", stage: "quality_check", percent: 100, current_segment: "", total_segments: 2, cache_hit: 2, cache_miss: 0, encoder: "h264_nvenc", output: "08_projects/demo/output/rough_preview.mp4", message: "已完成" }];
