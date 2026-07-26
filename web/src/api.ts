export type Project = {
  id: number;
  name: string;
  status: string;
  category?: string;
  content_type?: string;
  video_count?: number;
};

export type Clip = {
  clip_id: string;
  video_id: number;
  filename: string;
  status: string;
  segment_count: number;
  duration_seconds: number;
  detected_category: string;
  time_of_day: string;
  visual_summary: string;
  ai_visual_summary: string;
  user_summary: string;
  user_summary_updated_at?: string | null;
  user_summary_migration_state?: string;
  effective_summary: string;
  effective_summary_source: "user" | "ai" | "none";
};

export type BgmTrack = {
  id: number;
  title: string;
  artist?: string;
  source_url?: string;
  license_name?: string;
  attribution_text?: string;
  mood?: string;
  duration_seconds?: number;
};

export type AudioSegmentSettings = {
  role: "keep" | "lower" | "mute" | "bgm_only";
  volume_db: number;
  fade_in_seconds: number;
  fade_out_seconds: number;
  locked: boolean;
};

export type AudioSegmentOverride = Partial<AudioSegmentSettings>;

export type AudioState = {
  schema_version: number;
  enabled: boolean;
  bgm: { bgm_id: number | null; enabled: boolean; volume_db: number; start_seconds: number; loop: boolean; fade_in_seconds: number; fade_out_seconds: number; track?: BgmTrack };
  original_audio: { default_role: AudioSegmentSettings["role"]; default_volume_db: number; lower_volume_db: number; fade_in_seconds?: number; fade_out_seconds?: number };
  normalization: { enabled: boolean; target_lufs: number; true_peak_db: number };
  segments: Record<string, AudioSegmentOverride | null>;
};

export type Segment = {
  segment_id: string;
  clip_id: string;
  title: string;
  group: string;
  start_seconds: number;
  end_seconds: number;
  score: number;
  suggested_use: string;
  scene_role: string;
  story_position: string;
  manual_order: number;
  audio_role: string;
  speed: number;
  include: boolean;
  user_notes: string;
  source_file?: string;
  source_filename?: string;
  thumbnail_time_ratio?: number;
  storyboard_group_id?: string;
  storyboard_order?: number;
  storyboard_locked?: boolean;
  storyboard_notes?: string;
};

export type StoryboardGroup = { group_id: string; title: string; category: string; order: number };
export type StoryboardSegment = { group_id: string; order: number; included: boolean; locked: boolean; manual_group?: boolean; manual_order?: boolean; thumbnail_time_ratio: number; notes: string; thumbnail_url?: string; effective_audio_role?: string; effective_color_enabled?: boolean };
export type StoryboardState = {
  schema_version: number;
  groups: StoryboardGroup[];
  segments: Record<string, StoryboardSegment>;
  summary?: { total_segments: number; included_segments: number; excluded_segments: number; estimated_duration_seconds: number; groups: Array<{ group_id: string; count: number; duration_seconds: number }>; audio_roles: Record<string, number> };
  validation?: { valid: boolean; errors: string[]; warnings: string[] };
  exists?: boolean;
};

export type Job = {
  job_id?: string;
  project_id?: number;
  legacy_job_key?: string;
  kind: string;
  status: string;
  stage?: string;
  message: string;
  percent: number;
  current_segment_id?: string;
  current_segment_index?: number;
  segment_count?: number;
  cache_hit?: boolean;
  output_path?: string;
  error?: string;
  log_path?: string;
  updated_at?: string;
};

export type JobsSnapshot = {
  jobs: Job[];
  project_revision?: number;
  project_changed?: boolean;
};

export type ProjectDetail = {
  project: Project;
  project_revision?: number;
  clips: Clip[];
  segments: Segment[];
  bgm: BgmTrack[];
  plan: { status?: string; bgm_recommendations?: BgmRecommendation[] };
  workflow: { style: string; current: string; stages: WorkflowStage[] };
  review: { status?: string; notes?: string; approved_by_user?: boolean };
  script: string;
  folder: string;
  can_render: boolean;
  render_gate_reason: string;
  color: ColorState;
  audio: AudioState;
  storyboard: StoryboardState;
};

export type ColorAdjustment = {
  mode: string;
  lut_path: string;
  lut_kind: string;
  exposure: number;
  temperature: number;
  tint: number;
  contrast: number;
  saturation: number;
  gamma: number;
  highlights: number;
  shadows: number;
};

export type ColorReference = {
  id: string;
  type: string;
  video_id: number;
  source_name?: string;
  timestamp_seconds: number;
  start_seconds?: number;
  end_seconds?: number;
  label: string;
  score: number;
  frame_url?: string;
};

export type ColorSegmentState = {
  enabled: boolean;
  locked: boolean;
  excluded: boolean;
  reference_candidate?: boolean;
  suggested?: ColorAdjustment;
  applied?: ColorAdjustment;
  confidence?: number;
  warnings?: string[];
};

export type ColorSegmentPatch = Pick<ColorSegmentState, "enabled" | "locked" | "excluded" | "applied">;

export type ColorState = {
  schema_version: number;
  enabled: boolean;
  reference: ColorReference | Record<string, never>;
  references: ColorReference[];
  analysis: { luma?: { average?: number; highlight_ratio?: number; sampled_frames?: number }; confidence?: string; basis_text?: string; warnings?: string[]; statistics?: Record<string, unknown> };
  suggested: ColorAdjustment;
  applied: ColorAdjustment;
  segments: Record<string, ColorSegmentState>;
};

export type ColorStatePatch = {
  schema_version: number;
  enabled: boolean;
  applied: ColorAdjustment;
  segments: Record<string, ColorSegmentPatch | null>;
};

export type BgmRecommendation = {
  group: string;
  activity: string;
  mood: string[];
  track: BgmTrack;
};

export type WorkflowStage = {
  id: string;
  label: string;
  status: string;
  artifacts: string[];
};

export class ApiError extends Error {
  readonly status: number;
  readonly payload: Record<string, unknown>;

  constructor(status: number, payload: Record<string, unknown>) {
    super(String(payload.error || payload.message || `${status} request failed`));
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function formatApiError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    const revision = typeof error.payload.project_revision === "number" ? `目前版本 ${error.payload.project_revision}` : "目前版本已更新";
    return `${revision}，請重新載入後再儲存，未套用這次舊內容。`;
  }
  return error instanceof Error ? error.message : "網路或服務錯誤";
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, payload && typeof payload === "object" ? payload : {});
  return payload as T;
}

export const api = {
  projects: () => json<Project[]>("/api/projects"),
  project: (id: number, signal?: AbortSignal) => json<ProjectDetail>(`/api/project?id=${id}`, { signal }),
  jobs: (projectId: number, signal?: AbortSignal, sinceRevision?: number) => json<Job[] | JobsSnapshot>(`/api/jobs?project_id=${projectId}&meta=1${sinceRevision === undefined ? "" : `&since_revision=${sinceRevision}`}`, { signal }).then((result) => Array.isArray(result) ? { jobs: result } : result),
  bgm: () => json<BgmTrack[]>("/api/bgm"),
  createProject: (name: string) =>
    json<{ ok: boolean; id: number }>("/api/projects", post({ name, video_ids: [], category: "unknown", content_type: "diary_montage", platform: "YouTube" })),
  uploadProject: (projectId: number, files: ReadonlyArray<File>, baseRevision?: number) => {
    const body = new FormData();
    body.append("project_id", String(projectId));
    if (baseRevision !== undefined) body.append("base_revision", String(baseRevision));
    files.forEach((file) => body.append("file", file));
    return json<{ ok: boolean; files?: string[]; error?: string }>("/api/project/upload", { method: "POST", body });
  },
  analyzeJob: (projectId: number, force = false, baseRevision?: number) =>
    json<{ ok: boolean; message?: string }>("/api/project/analyze-job", post({ project_id: projectId, force, base_revision: baseRevision })),
  analyzeVideo: (projectId: number, videoId: number, baseRevision?: number) =>
    json<{ ok: boolean; message?: string }>("/api/project/analyze-video", post({ project_id: projectId, video_id: videoId, base_revision: baseRevision })),
  saveClipSummary: (projectId: number, videoId: number, userSummary: string, baseRevision?: number) =>
    json<{ ok: boolean; plan_rebuilt?: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/clip-summary", post({ project_id: projectId, video_id: videoId, user_summary: userSummary, base_revision: baseRevision })),
  colorAnalyze: (projectId: number, force = false, baseRevision?: number) =>
    json<{ ok: boolean; state?: ColorState; error?: string }>("/api/project/color-analyze", post({ project_id: projectId, force, base_revision: baseRevision })),
  colorSettings: (projectId: number, state: ColorStatePatch, baseRevision?: number) =>
    json<{ ok: boolean; state?: ColorState; error?: string; code?: string; project_revision?: number }>("/api/project/color-settings", post({ project_id: projectId, state, base_revision: baseRevision })),
  colorReference: (projectId: number, referenceId: string, baseRevision?: number) =>
    json<{ ok: boolean; state?: ColorState; error?: string; code?: string; project_revision?: number }>("/api/project/color-reference", post({ project_id: projectId, reference_id: referenceId, base_revision: baseRevision })),
  colorPreview: (projectId: number, mode = "", baseRevision?: number) =>
    json<{ ok: boolean; message?: string; previews?: Array<{ video_id: number; segment_id: string; before_url: string; after_url: string; cache_hit: boolean }>; error?: string; code?: string }>("/api/project/color-job", post({ project_id: projectId, mode, base_revision: baseRevision })),
  colorPreviewDirect: (projectId: number, force = false, baseRevision?: number) =>
    json<{ ok: boolean; previews?: Array<{ video_id: number; segment_id: string; before_url: string; after_url: string; cache_hit: boolean; confidence?: number; warnings?: string[] }>; error?: string }>("/api/project/color-preview", post({ project_id: projectId, force, base_revision: baseRevision })),
  buildPlan: (projectId: number, baseRevision?: number) =>
    json<{ ok: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/build-plan", post({ project_id: projectId, base_revision: baseRevision })),
  assignBgm: (projectId: number, bgmId: number, baseRevision?: number) =>
    json<{ ok: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/bgm", post({ project_id: projectId, bgm_id: bgmId, base_revision: baseRevision })),
  audioSettings: (projectId: number, patch: Partial<AudioState>, baseRevision?: number) =>
    json<{ ok: boolean; state?: AudioState; error?: string; code?: string; project_revision?: number }>("/api/project/audio-settings", post({ project_id: projectId, patch, base_revision: baseRevision })),
  audioPreview: (projectId: number, options: { segmentId?: string; timelineStartSeconds?: number; durationSeconds?: number; patch?: Partial<AudioState>; force?: boolean } = {}) =>
    json<{ ok: boolean; file?: string; url?: string; cache_hit?: boolean; duration_seconds?: number; timeline_start_seconds?: number; error?: string }>("/api/project/audio-preview", post({ project_id: projectId, segment_id: options.segmentId || null, timeline_start_seconds: options.timelineStartSeconds ?? 0, duration_seconds: options.durationSeconds ?? 12, patch: options.patch, force: options.force ?? false })),
  approve: (projectId: number, notes: string, baseRevision?: number) =>
    json<{ ok: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/approve", post({ project_id: projectId, notes, base_revision: baseRevision })),
  reject: (projectId: number, notes: string, baseRevision?: number) =>
    json<{ ok: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/reject", post({ project_id: projectId, notes, base_revision: baseRevision })),
  revise: (projectId: number, notes: string, baseRevision?: number) =>
    json<{ ok: boolean; error?: string; code?: string; project_revision?: number }>("/api/project/revise", post({ project_id: projectId, notes, base_revision: baseRevision })),
  saveSegments: (projectId: number, segments: Segment[], baseRevision?: number) =>
    json<{ ok: boolean; path?: string; error?: string }>("/api/project/segments", post({ project_id: projectId, segments, base_revision: baseRevision })),
  saveSegmentTiming: (projectId: number, segmentId: string, timing: { start_seconds: number; end_seconds: number; speed: number }, baseRevision?: number) =>
    json<{ ok: boolean; path?: string; error?: string }>("/api/project/segment-timing", post({ project_id: projectId, segment_id: segmentId, ...timing, base_revision: baseRevision })),
  storyboard: (projectId: number) => json<StoryboardState>(`/api/project/storyboard?project_id=${projectId}`),
  generateStoryboard: (projectId: number, force = false, baseRevision?: number) =>
    json<{ ok: boolean; storyboard?: StoryboardState; error?: string }>("/api/project/storyboard/generate", post({ project_id: projectId, force, base_revision: baseRevision })),
  updateStoryboard: (projectId: number, state: StoryboardState, baseRevision?: number) =>
    json<{ ok: boolean; storyboard?: StoryboardState; render_changed?: boolean; approval_invalidated?: boolean; error?: string }>("/api/project/storyboard", post({ project_id: projectId, state, base_revision: baseRevision })),
  storyboardThumbnail: (projectId: number, segmentId: string, ratio = 0.5, force = false) =>
    json<{ ok: boolean; file?: string; url?: string; cache_hit?: boolean; error?: string }>("/api/project/storyboard/thumbnail", post({ project_id: projectId, segment_id: segmentId, ratio, force })),
  storyboardPreview: (projectId: number, options: { mode: "segment" | "transition" | "range"; segmentId?: string; durationSeconds?: number; timelineStartSeconds?: number; storyboardState?: StoryboardState; force?: boolean }) =>
    json<{ ok: boolean; file?: string; url?: string; cache_hit?: boolean; duration_seconds?: number; timeline_start_seconds?: number; previews?: Array<{ kind: string; file: string; url?: string; duration_seconds: number; cache_hit?: boolean }>; error?: string }>("/api/project/storyboard/preview", post({ project_id: projectId, mode: options.mode, segment_id: options.segmentId || null, duration_seconds: options.durationSeconds ?? 8, timeline_start_seconds: options.timelineStartSeconds ?? 0, storyboard_state: options.storyboardState, force: options.force ?? false })),
  opencutExport: (projectId: number, renderClips = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/opencut-export", post({ project_id: projectId, render_clips: renderClips, max_segments: 20 })),
  hyperframesExport: (projectId: number, render = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/hyperframes-export", post({ project_id: projectId, render, max_segments: 20 })),
  opencutJob: (projectId: number, renderClips = false, baseRevision?: number) =>
    json<{ ok: boolean; message?: string; error?: string }>("/api/project/opencut-job", post({ project_id: projectId, render_clips: renderClips, max_segments: 20, base_revision: baseRevision })),
  hyperframesJob: (projectId: number, render = false, baseRevision?: number) =>
    json<{ ok: boolean; message?: string; error?: string }>("/api/project/hyperframes-job", post({ project_id: projectId, render, max_segments: 20, base_revision: baseRevision })),
  createRenderJob: (projectId: number, outputPath = "") =>
    json<{ ok: boolean; created: boolean; job?: Job; error?: string }>("/api/project/render-job", post({ project_id: projectId, output_path: outputPath })),
  cancelRenderJob: (jobId: string) =>
    json<{ ok: boolean; job?: Job; error?: string; reason?: string }>("/api/render-job/cancel", post({ job_id: jobId })),
  cancelLegacyJob: (projectId: number, legacyJobKey: string) =>
    json<{ ok: boolean; message?: string; job?: Job; error?: string }>("/api/project/legacy-job/cancel", post({ project_id: projectId, legacy_job_key: legacyJobKey })),
  stopJobs: (projectId: number) =>
    json<{ ok: boolean; message?: string }>("/api/project/stop-jobs", post({ project_id: projectId }))
};

function post(body: object): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body)
  };
}
