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
};

export type Job = {
  kind: string;
  status: string;
  message: string;
  percent: number;
};

export type ProjectDetail = {
  project: Project;
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

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  projects: () => json<Project[]>("/api/projects"),
  project: (id: number) => json<ProjectDetail>(`/api/project?id=${id}`),
  jobs: (projectId: number) => json<Job[]>(`/api/jobs?project_id=${projectId}`),
  bgm: () => json<BgmTrack[]>("/api/bgm"),
  createProject: (name: string) =>
    json<{ ok: boolean; id: number }>("/api/projects", post({ name, video_ids: [], category: "unknown", content_type: "diary_montage", platform: "YouTube" })),
  uploadProject: (projectId: number, files: FileList) => {
    const body = new FormData();
    body.append("project_id", String(projectId));
    Array.from(files).forEach((file) => body.append("file", file));
    return json<{ ok: boolean; files?: string[]; error?: string }>("/api/project/upload", { method: "POST", body });
  },
  analyzeJob: (projectId: number, force = false) =>
    json<{ ok: boolean; message?: string }>("/api/project/analyze-job", post({ project_id: projectId, force })),
  analyzeVideo: (projectId: number, videoId: number) =>
    json<{ ok: boolean; message?: string }>("/api/project/analyze-video", post({ project_id: projectId, video_id: videoId })),
  saveClipSummary: (projectId: number, videoId: number, summary: string) =>
    json<{ ok: boolean }>("/api/project/clip-summary", post({ project_id: projectId, video_id: videoId, summary })),
  colorPreview: (projectId: number, mode: string) =>
    json<{ ok: boolean; message?: string }>("/api/project/color-job", post({ project_id: projectId, mode })),
  buildPlan: (projectId: number) =>
    json<{ ok: boolean }>("/api/project/build-plan", post({ project_id: projectId })),
  assignBgm: (projectId: number, bgmId: number) =>
    json<{ ok: boolean }>("/api/project/bgm", post({ project_id: projectId, bgm_id: bgmId })),
  approve: (projectId: number, notes: string) =>
    json<{ ok: boolean }>("/api/project/approve", post({ project_id: projectId, notes })),
  reject: (projectId: number, notes: string) =>
    json<{ ok: boolean }>("/api/project/reject", post({ project_id: projectId, notes })),
  revise: (projectId: number, notes: string) =>
    json<{ ok: boolean }>("/api/project/revise", post({ project_id: projectId, notes })),
  saveSegments: (projectId: number, segments: Segment[]) =>
    json<{ ok: boolean }>("/api/project/segments", post({ project_id: projectId, segments })),
  opencutExport: (projectId: number, renderClips = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/opencut-export", post({ project_id: projectId, render_clips: renderClips, max_segments: 20 })),
  hyperframesExport: (projectId: number, render = false) =>
    json<{ ok: boolean; folder?: string; output?: string; error?: string }>("/api/project/hyperframes-export", post({ project_id: projectId, render, max_segments: 20 })),
  opencutJob: (projectId: number, renderClips = false) =>
    json<{ ok: boolean; message?: string }>("/api/project/opencut-job", post({ project_id: projectId, render_clips: renderClips, max_segments: 20 })),
  hyperframesJob: (projectId: number, render = false) =>
    json<{ ok: boolean; message?: string }>("/api/project/hyperframes-job", post({ project_id: projectId, render, max_segments: 20 })),
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
