export type Project = {
  id: number;
  name: string;
  status: string;
  category?: string;
  content_type?: string;
  video_count?: number;
};

export type PerceptionWindowResult = {
  window_uuid: string;
  segment_uuid?: string;
  include?: boolean;
  user_notes?: string;
  locked?: boolean;
  publish_status?: string;
  ordinal?: number;
  start_seconds?: number;
  end_seconds?: number;
  frame_timestamps?: number[];
  summary?: string;
  action?: string;
  shot_role?: string;
  technical_quality?: { score?: number; issues?: string[] };
  duplicate_group?: string;
  natural_audio_recommendation?: string;
  confidence?: number;
  validation?: { status?: string; needs_review_reasons?: string[] };
  evidence_urls?: {
    contact_sheet?: string;
    window?: string;
    validation?: string;
    normalized?: string;
  };
};

export type PerceptionRunState = {
  current_analysis_run_uuid?: string;
  current_status?: string;
  current_generation?: number;
  current_window_manifest?: Array<{ window_uuid?: string; start_seconds?: number; end_seconds?: number; frames?: Array<{ timestamp_seconds?: number; sample_reasons?: string[] }> }>;
  current_window_results?: PerceptionWindowResult[];
  current_window_validation?: { status?: string; needs_review_reasons?: string[]; checks?: Array<Record<string, unknown>> };
  current_cloud_review?: CloudReviewAudit;
  multi_frame_contract?: Record<string, unknown>;
  [key: string]: unknown;
};

export type CloudReviewWindow = {
  project_id: number;
  video_id: number;
  run_uuid: string;
  window_uuid: string;
  segment_uuid?: string;
  ordinal?: number;
  start_seconds?: number;
  end_seconds?: number;
  frame_timestamps?: number[];
  frame_count: number;
  confidence: number;
  reasons: string[];
  rejected_reason?: string;
  source_paths_exposed: false;
};

export type CloudReviewPlan = {
  contract_version: string;
  status: "ready" | "no_eligible_windows" | "budget_exceeded" | string;
  provider: string;
  policy: Record<string, unknown>;
  windows: CloudReviewWindow[];
  rejected_windows: CloudReviewWindow[];
  estimated_calls: number;
  estimated_frames: number;
  estimated_cost_usd: number;
  privacy: { full_video_upload: false; payload: string; source_paths_exposed: false };
};

export type CloudReviewAudit = {
  contract_version?: string;
  status?: string;
  provider?: string;
  model?: string;
  error?: string;
  completed_count?: number;
  windows?: Array<CloudReviewWindow & { result?: Record<string, unknown> | null }>;
  [key: string]: unknown;
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
  media_url?: string;
  sampling?: SamplingState;
  perception_run?: PerceptionRunState;
};

export type SamplingPolicy = {
  name: string;
  version: number;
  mode: "fixed" | "adaptive";
  preset: "balanced" | "dense";
  baseline_interval_seconds: number;
  max_frames_per_clip: number;
  max_frames_per_minute: number;
};

export type SamplingManifest = {
  policy?: SamplingPolicy;
  estimated_vision_calls?: number;
  actual_vision_calls?: number;
  cache_hits?: number;
  sample_reason_counts?: Record<string, number>;
  candidate_counts?: Record<string, number>;
  visual_dedupe?: { status?: string; removed?: number };
  contract_hash?: string;
  samples?: Array<{ timestamp_seconds: number; reasons: string[] }>;
};

export type SamplingState = {
  default_policy: SamplingPolicy;
  estimated_frame_count: number;
  current?: SamplingManifest;
};

export type SamplingOverride = {
  mode: "fixed" | "adaptive";
  preset?: "balanced" | "dense";
  baseline_interval_seconds?: number;
  max_frames_per_clip?: number;
};

export type BgmTrack = {
  id: number;
  title: string;
  artist?: string;
  source_url?: string;
  license_name?: string;
  attribution_text?: string;
  attribution_status?: "required" | "not_required" | "unknown";
  license_status?: "verified" | "unverified" | "invalid";
  license_verified_at?: string;
  license_source_url?: string;
  verification_source?: string;
  verification_provenance?: string;
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
  source?: "legacy" | "new";
  settings_exists?: boolean;
  effective_selected_track?: BgmTrack;
  migration?: { state: string; warning: string };
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
  media_url?: string;
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
  approval_snapshot_id?: string;
  approval_snapshot_hash?: string;
  encoder_contract?: { implementation?: string; fallback_reason?: string; [key: string]: unknown };
};

export type StorageArtifact = {
  artifact_id: string;
  project_id: number;
  type: string;
  path?: string;
  display_name?: string;
  size: number;
  pinned: boolean;
  lifecycle_state: string;
  deletion_status: string;
  references?: string[];
};

export type StorageSummary = {
  ok: boolean;
  project_id: number;
  artifacts: StorageArtifact[];
  total_bytes: number;
  protected_bytes: number;
  pinned_count: number;
  free_bytes: number;
  recovered_count: number;
};

export type CleanupPlan = {
  plan_id: string;
  candidate_count: number;
  candidate_size: number;
  candidates: Array<{ artifact_id: string; type: string; size: number; reason: string }>;
  protected: Array<{ artifact_id: string; type: string; reason: string }>;
};

export type RenderReport = {
  status: "current" | "historical" | "stale" | string;
  project_id?: number;
  manifest_hash?: string;
  profile_id?: string;
  approval_snapshot?: { snapshot_id?: string; snapshot_hash?: string; schema_version?: number; approved_project_revision?: number };
  encoder_contract?: { implementation?: string; fallback_reason?: string; [key: string]: unknown };
  loudness?: Record<string, unknown>;
  color?: Record<string, unknown>;
  timing?: Record<string, unknown>;
  measurements?: Record<string, unknown>;
  bgm?: Record<string, unknown>;
  output?: { filename?: string; size?: number; sha256?: string; duration_seconds?: number };
  segment_count?: number;
  qc?: { passed?: boolean; errors?: string[]; warnings?: string[] };
  cache?: Record<string, unknown>;
  created_at?: string;
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
  story?: StoryDetail;
};

export type StoryChapter = {
  chapter_id?: string;
  order?: number;
  title?: string;
  purpose?: string;
  segment_uuids?: string[];
  pacing_intent?: string;
  transition_intent?: string;
  natural_audio_intent?: string;
  title_card_suggestion?: string;
  notes?: string;
  confidence?: number;
  needs_review_reasons?: string[];
  locked?: boolean;
};

export type StoryGeneration = {
  story_generation_uuid: string;
  project_id: number;
  generation: number;
  status: string;
  input_hash?: string;
  input_snapshot?: { input_hash?: string; schema_version?: number };
  provider?: string;
  model?: string;
  prompt_version?: string;
  schema_version?: number;
  creator_profile_version?: number;
  project_story_profile_version?: number;
  normalized_response?: { project_summary?: string; chapters?: StoryChapter[]; overall_confidence?: number; needs_review_reasons?: string[]; suppressed_segments?: SuppressedSegment[] };
  review_state?: { chapters?: StoryChapter[]; project_summary?: string; source?: string; locked?: boolean; suppressed_segments?: SuppressedSegment[] };
  validation?: Record<string, unknown>;
  error?: string;
  cache_hit?: boolean;
  provider_audit?: ProviderAudit;
  story_audit?: StoryAudit;
};

export type SuppressedSegment = { segment_uuid: string; representative_segment_uuid: string; reason?: string };
export type ProviderAudit = { calls?: number; retries?: number; call_latencies_ms?: number[]; total_latency_ms?: number; strict_schema?: boolean; error?: string };
export type StoryAudit = {
  raw?: { provider?: string; model?: string; input_hash?: string; schema_version?: number; provider_audit?: ProviderAudit };
  normalized?: { schema_version?: number; project_summary_present?: boolean; chapter_count?: number; segment_count?: number; segment_uuids?: string[]; suppressed_count?: number; validation_status?: string };
  effective?: { source?: string; locked?: boolean; chapter_count?: number; segment_count?: number; segment_uuids?: string[]; suppressed_count?: number };
};
export type StoryCalibration = { schema_version?: number; profile_id?: string; status?: string; sample_count?: number; record_count?: number; metrics?: Record<string, number | null>; source?: string };

export type StoryDetail = {
  settings: {
    schema_version?: number;
    profile_id?: string;
    profile_version?: number;
    project_intent?: string;
    itinerary?: string;
    desired_sequence?: string[];
    desired_pacing?: string;
    title_card_preference_override?: string;
    natural_audio_override?: string;
    must_keep?: string[];
    exclude_guidance?: string[];
  };
  creator_profile: Record<string, unknown>;
  story_profile: { profile_id?: string; label?: string; roles?: string[]; rules?: string[] };
  generations: StoryGeneration[];
  current_generation?: StoryGeneration;
  current_story_generation_uuid?: string;
  last_successful_story_generation_uuid?: string;
  current_input_hash?: string;
  current_generation_is_stale?: boolean;
  calibration?: StoryCalibration;
};

export type ColorAdjustment = {
  mode: string;
  // The API intentionally hides local LUT absolute paths.  A user may enter
  // a replacement path, while an existing server-owned path is represented
  // by ``lut_name`` only.
  lut_path?: string;
  lut_name?: string;
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

export type ColorSegmentAnalysis = Partial<Pick<ColorSegmentState, "reference_candidate" | "suggested" | "confidence" | "warnings">>;
export type ColorSegmentOverride = Partial<Pick<ColorSegmentState, "enabled" | "locked" | "excluded" | "applied">>;
export type ColorSegmentPatch = ColorSegmentOverride;
export type ColorLutModeContract = { requires_lut: boolean; extension: string };
export type ColorLutContract = { version: string; strategy: string; modes: Record<string, ColorLutModeContract> };

export type ColorState = {
  schema_version: number;
  enabled: boolean;
  reference: ColorReference | Record<string, never>;
  references: ColorReference[];
  analysis: { luma?: { average?: number; highlight_ratio?: number; sampled_frames?: number }; confidence?: string; basis_text?: string; warnings?: string[]; statistics?: Record<string, unknown> };
  suggested: ColorAdjustment;
  applied: ColorAdjustment;
  segments: Record<string, ColorSegmentState>;
  segment_analysis?: Record<string, ColorSegmentAnalysis>;
  segment_overrides?: Record<string, ColorSegmentOverride | null>;
  lut_contract?: ColorLutContract;
};

export type ColorStatePatch = {
  schema_version: number;
  enabled: boolean;
  applied: ColorAdjustment;
  segments: Record<string, ColorSegmentOverride | null>;
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
    const code = error.payload.code;
    if (code === "stale_creator_profile") {
      const version = typeof error.payload.profile_version === "number" ? `目前 version ${error.payload.profile_version}` : "目前 version 已更新";
      return `Creator Profile ${version}，請重新載入後再儲存，未套用這次舊內容。`;
    }
    if (code === "stale_story_settings") {
      const version = typeof error.payload.profile_version === "number" ? `目前 version ${error.payload.profile_version}` : "目前 version 已更新";
      return `Story Settings ${version}，請重新載入後再儲存，未套用這次舊內容。`;
    }
    if (code === "stale_project_revision") {
      const revision = typeof error.payload.project_revision === "number" ? `目前 project revision ${error.payload.project_revision}` : "目前 project revision 已更新";
      return `${revision}，請重新載入後再儲存，未套用這次舊內容。`;
    }
    if (typeof error.payload.project_revision === "number") {
      return `目前版本 ${error.payload.project_revision}，請重新載入後再儲存，未套用這次舊內容。`;
    }
    return "版本已更新，請重新載入後再儲存，未套用這次舊內容。";
  }
  return error instanceof Error ? error.message : "網路或服務錯誤";
}

let csrfToken: string | null = null;
let csrfRequest: Promise<string> | null = null;

async function getCsrfToken(): Promise<string> {
  if (csrfToken) return csrfToken;
  csrfRequest ||= fetch("/api/security", { headers: { accept: "application/json" } })
    .then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || typeof payload.csrf_token !== "string" || !payload.csrf_token) throw new Error("無法取得本機安全 token");
      csrfToken = payload.csrf_token;
      return payload.csrf_token;
    })
    .finally(() => { csrfRequest = null; });
  return csrfRequest;
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  let request = init;
  if (String(init?.method || "GET").toUpperCase() !== "GET" && url !== "/api/security") {
    const token = await getCsrfToken();
    const headers = new Headers(init?.headers);
    headers.set("x-video-vault-csrf", token);
    request = { ...init, headers };
  }
  const res = await fetch(url, request);
  const payload = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, payload && typeof payload === "object" ? payload : {});
  return payload as T;
}

export const api = {
  projects: () => json<Project[]>("/api/projects"),
  project: (id: number, signal?: AbortSignal) => json<ProjectDetail>(`/api/project?id=${id}`, { signal }),
  jobs: (projectId: number, signal?: AbortSignal, sinceRevision?: number) => json<Job[] | JobsSnapshot>(`/api/jobs?project_id=${projectId}&meta=1${sinceRevision === undefined ? "" : `&since_revision=${sinceRevision}`}`, { signal }).then((result) => Array.isArray(result) ? { jobs: result } : result),
  bgm: () => json<BgmTrack[]>("/api/bgm"),
  storage: (projectId: number) => json<StorageSummary>(`/api/project/storage?project_id=${projectId}`),
  storagePlan: (projectId: number) => json<{ ok: boolean; plan?: CleanupPlan; error?: string }>("/api/project/storage/plan", post({ project_id: projectId })),
  storageCleanup: (projectId: number, planId: string) => json<{ ok: boolean; results?: Array<{ artifact_id: string; status: string; code?: string }>; reclaimed_bytes?: number; error?: string }>("/api/project/storage/cleanup", post({ project_id: projectId, plan_id: planId })),
  storagePin: (projectId: number, artifactId: string, pinned: boolean) => json<{ ok: boolean; artifact?: StorageArtifact; error?: string }>("/api/project/storage/pin", post({ project_id: projectId, artifact_id: artifactId, pinned })),
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
  analyzeVideo: (projectId: number, videoId: number, baseRevision?: number, sampling?: SamplingOverride) =>
    json<{ ok: boolean; message?: string }>("/api/project/analyze-video", post({ project_id: projectId, video_id: videoId, base_revision: baseRevision, sampling })),
  cloudReviewPlan: (projectId: number, windowUuids?: string[]) =>
    json<{ ok: boolean; plan?: CloudReviewPlan; project_revision?: number; error?: string }>("/api/project/cloud-review/plan", post({ project_id: projectId, window_uuids: windowUuids })),
  cloudReview: (projectId: number, baseRevision?: number, windowUuids?: string[]) =>
    json<{ ok: boolean; review_status?: string; cloud_review?: CloudReviewAudit; project_revision?: number; local_result_preserved?: boolean; error?: string; code?: string }>("/api/project/cloud-review", post({ project_id: projectId, base_revision: baseRevision, window_uuids: windowUuids })),
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
  saveSegmentEvidence: (projectId: number, segmentId: string, patch: Record<string, unknown>, baseRevision?: number) =>
    json<{ ok: boolean; path?: string; error?: string; code?: string }>("/api/project/segment-evidence", post({ project_id: projectId, segment_id: segmentId, patch, base_revision: baseRevision })),
  storyboard: (projectId: number) => json<StoryboardState>(`/api/project/storyboard?project_id=${projectId}`),
  generateStoryboard: (projectId: number, force = false, baseRevision?: number) =>
    json<{ ok: boolean; storyboard?: StoryboardState; error?: string }>("/api/project/storyboard/generate", post({ project_id: projectId, force, base_revision: baseRevision })),
  updateStoryboard: (projectId: number, state: StoryboardState, baseRevision?: number) =>
    json<{ ok: boolean; storyboard?: StoryboardState; render_changed?: boolean; approval_invalidated?: boolean; error?: string }>("/api/project/storyboard", post({ project_id: projectId, state, base_revision: baseRevision })),
  storySettings: (projectId: number) => json<StoryDetail>(`/api/project/story?project_id=${projectId}`),
  creatorProfile: () => json<Record<string, unknown>>("/api/creator-profile"),
  saveCreatorProfile: (profile: Record<string, unknown>, expectedVersion?: number) =>
    json<{ ok: boolean; creator_profile?: Record<string, unknown>; profile_version?: number; error?: string; code?: string }>("/api/creator-profile", post({ profile, expected_version: expectedVersion })),
  saveStorySettings: (projectId: number, settings: Record<string, unknown>, baseRevision?: number, expectedVersion?: number) =>
    json<{ ok: boolean; settings?: StoryDetail["settings"]; profile_version?: number; project_revision?: number; error?: string; code?: string }>("/api/project/story/settings", post({ project_id: projectId, settings, base_revision: baseRevision, expected_version: expectedVersion ?? Number(settings.profile_version || 1) })),
  generateStory: (projectId: number, force = false, provider?: string, baseRevision?: number) =>
    json<{ ok: boolean; generation?: StoryGeneration; story?: StoryDetail; error?: string; code?: string }>("/api/project/story/generate", post({ project_id: projectId, force, provider, base_revision: baseRevision })),
  updateStoryReview: (projectId: number, storyGenerationUuid: string, review: Record<string, unknown>, baseRevision?: number) =>
    json<{ ok: boolean; generation?: StoryGeneration; error?: string; code?: string }>("/api/project/story/review", post({ project_id: projectId, story_generation_uuid: storyGenerationUuid, review, base_revision: baseRevision })),
  applyStory: (projectId: number, storyGenerationUuid: string, baseRevision?: number) =>
    json<{ ok: boolean; storyboard?: StoryboardState; render_changed?: boolean; approval_invalidated?: boolean; generation?: StoryGeneration; error?: string; code?: string }>("/api/project/story/apply", post({ project_id: projectId, story_generation_uuid: storyGenerationUuid, base_revision: baseRevision })),
  storyCalibration: (projectId: number) => json<StoryCalibration>(`/api/project/story/calibration?project_id=${projectId}`),
  recalculateStoryCalibration: (projectId: number, profileId?: string) => json<{ ok: boolean; calibration?: StoryCalibration; error?: string }>("/api/project/story/calibration", post({ project_id: projectId, profile_id: profileId, action: "recalculate" })),
  resetStoryCalibration: (projectId: number, profileId?: string) => json<{ ok: boolean; calibration?: StoryCalibration; error?: string }>("/api/project/story/calibration", post({ project_id: projectId, profile_id: profileId, action: "reset" })),
  storyboardThumbnail: (projectId: number, segmentId: string, ratio = 0.5, force = false) =>
    json<{ ok: boolean; file?: string; url?: string; cache_hit?: boolean; error?: string }>("/api/project/storyboard/thumbnail", post({ project_id: projectId, segment_id: segmentId, ratio, force })),
  storyboardPreview: (projectId: number, options: { mode: "segment" | "transition" | "range"; segmentId?: string; durationSeconds?: number; timelineStartSeconds?: number; storyboardState?: StoryboardState; force?: boolean }) =>
    json<{ ok: boolean; file?: string; url?: string; cache_hit?: boolean; duration_seconds?: number; timeline_start_seconds?: number; previews?: Array<{ kind: string; file: string; url?: string; duration_seconds: number; cache_hit?: boolean }>; error?: string }>("/api/project/storyboard/preview", post({ project_id: projectId, mode: options.mode, segment_id: options.segmentId || null, duration_seconds: options.durationSeconds ?? 8, timeline_start_seconds: options.timelineStartSeconds ?? 0, storyboard_state: options.storyboardState, force: options.force ?? false })),
  opencutExport: (projectId: number, renderClips = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/opencut-export", post({ project_id: projectId, render_clips: renderClips, max_segments: 20 })),
  hyperframesExport: (projectId: number, render = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/hyperframes-export", post({ project_id: projectId, render, max_segments: 20 })),
  opencutJob: (projectId: number, renderClips = false, baseRevision?: number) =>
    json<{ ok: boolean; message?: string; error?: string }>("/api/project/opencut-job", post({ project_id: projectId, render_clips: renderClips, max_segments: 20, base_revision: baseRevision, confirm_local_action: true })),
  hyperframesJob: (projectId: number, render = false, baseRevision?: number) =>
    json<{ ok: boolean; message?: string; error?: string }>("/api/project/hyperframes-job", post({ project_id: projectId, render, max_segments: 20, base_revision: baseRevision, confirm_local_action: true })),
  createRenderJob: (projectId: number, outputPath = "") =>
    json<{ ok: boolean; created: boolean; job?: Job; error?: string }>("/api/project/render-job", post({ project_id: projectId, output_path: outputPath })),
  cancelRenderJob: (jobId: string) =>
    json<{ ok: boolean; job?: Job; error?: string; reason?: string }>("/api/render-job/cancel", post({ job_id: jobId })),
  renderJobReport: (jobId: string) =>
    json<{ ok: boolean; report?: RenderReport; error?: string }>(`/api/render-job/report?id=${encodeURIComponent(jobId)}`),
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
