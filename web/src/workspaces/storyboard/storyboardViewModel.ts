import type {
  AudioSegmentSettings,
  ProjectDetail,
  Segment,
  StoryboardSegment,
  StoryboardState,
} from "../../api";

export type StoryboardSegmentView = {
  id: string;
  title: string;
  sourceName: string;
  groupId: string;
  groupTitle: string;
  groupOrder: number;
  order: number;
  included: boolean;
  locked: boolean;
  thumbnailUrl: string;
  thumbnailRatio: number;
  startSeconds: number;
  endSeconds: number;
  speed: number;
  durationSeconds: number;
  score: number;
  sceneRole: string;
  storyPosition: string;
  suggestedUse: string;
  notes: string;
  audioRole: AudioSegmentSettings["role"];
  audioLabel: string;
  audioCustomized: boolean;
  colorEnabled: boolean;
  colorCustomized: boolean;
  colorWarnings: string[];
};

export type StoryboardGroupView = {
  id: string;
  title: string;
  category: string;
  order: number;
  segments: StoryboardSegmentView[];
};

export type StoryboardViewModel = {
  exists: boolean;
  valid: boolean;
  errors: string[];
  warnings: string[];
  groups: StoryboardGroupView[];
  segments: StoryboardSegmentView[];
  summary: {
    totalSegments: number;
    includedSegments: number;
    excludedSegments: number;
    estimatedDurationSeconds: number;
  };
};

export type StoryboardSegmentEdit = Pick<
  StoryboardSegment,
  "group_id" | "order" | "included" | "locked" | "thumbnail_time_ratio" | "notes"
>;

const AUDIO_LABELS: Record<AudioSegmentSettings["role"], string> = {
  keep: "保留原音",
  lower: "降低原音",
  mute: "靜音",
  bgm_only: "只留 BGM",
};

export function buildStoryboardViewModel(detail: ProjectDetail): StoryboardViewModel {
  const state = detail.storyboard;
  const declaredGroups = [...(state.groups || [])].sort((left, right) => left.order - right.order);
  const groupMeta = new Map(declaredGroups.map((group) => [group.group_id, group]));
  const fallbackGroupIds: string[] = [];

  const rows = detail.segments.map((segment) => {
    const storyboard = state.segments?.[segment.segment_id];
    const groupId = storyboard?.group_id || segment.storyboard_group_id || segment.group || "ungrouped";
    if (!groupMeta.has(groupId) && !fallbackGroupIds.includes(groupId)) fallbackGroupIds.push(groupId);
    return toSegmentView(detail, segment, storyboard, groupId, groupMeta.get(groupId)?.title || readableGroupName(groupId), groupMeta.get(groupId)?.order ?? Number.MAX_SAFE_INTEGER);
  });

  const allGroups = [
    ...declaredGroups.map((group) => ({ id: group.group_id, title: group.title, category: group.category, order: group.order })),
    ...fallbackGroupIds.map((groupId, index) => ({ id: groupId, title: readableGroupName(groupId), category: "other", order: declaredGroups.length + index + 1 })),
  ];

  const groups = allGroups
    .map((group) => ({
      ...group,
      segments: rows
        .filter((segment) => segment.groupId === group.id)
        .sort((left, right) => left.order - right.order || left.startSeconds - right.startSeconds || left.id.localeCompare(right.id)),
    }))
    .filter((group) => group.segments.length > 0 || declaredGroups.some((declared) => declared.group_id === group.id))
    .sort((left, right) => left.order - right.order || left.title.localeCompare(right.title));

  const orderedSegments = groups.flatMap((group) => group.segments);
  const includedSegments = orderedSegments.filter((segment) => segment.included);

  return {
    exists: Boolean(state.exists || state.groups?.length || Object.keys(state.segments || {}).length),
    valid: state.validation?.valid !== false,
    errors: [...(state.validation?.errors || [])],
    warnings: [...(state.validation?.warnings || [])],
    groups,
    segments: orderedSegments,
    summary: {
      totalSegments: orderedSegments.length,
      includedSegments: includedSegments.length,
      excludedSegments: orderedSegments.length - includedSegments.length,
      estimatedDurationSeconds: includedSegments.reduce((total, segment) => total + segment.durationSeconds, 0),
    },
  };
}

export function updateStoryboardSegment(
  state: StoryboardState,
  segmentId: string,
  patch: Partial<StoryboardSegmentEdit>,
): StoryboardState {
  const current = state.segments?.[segmentId];
  if (!current) return state;
  return {
    ...state,
    groups: state.groups.map((group) => ({ ...group })),
    segments: {
      ...state.segments,
      [segmentId]: {
        ...current,
        ...patch,
      },
    },
  };
}

export function validateSegmentTiming(
  startSeconds: number,
  endSeconds: number,
  speed: number,
  sourceDurationSeconds?: number,
): string[] {
  const errors: string[] = [];
  if (![startSeconds, endSeconds, speed].every(Number.isFinite)) errors.push("片段時間與速度必須是有限數值");
  if (startSeconds < 0) errors.push("片段起點不可小於 0 秒");
  if (endSeconds <= startSeconds) errors.push("片段終點必須大於起點");
  if (speed < 0.25 || speed > 4) errors.push("片段速度必須介於 0.25 到 4 倍");
  if (sourceDurationSeconds !== undefined && sourceDurationSeconds > 0 && endSeconds > sourceDurationSeconds + 0.001) {
    errors.push("片段終點超過來源影片長度");
  }
  return errors;
}

function toSegmentView(
  detail: ProjectDetail,
  segment: Segment,
  storyboard: StoryboardSegment | undefined,
  groupId: string,
  groupTitle: string,
  groupOrder: number,
): StoryboardSegmentView {
  const audioOverride = detail.audio.segments?.[segment.segment_id];
  const audioRole = normalizeAudioRole(
    audioOverride?.role || storyboard?.effective_audio_role || detail.audio.original_audio.default_role || segment.audio_role,
  );
  const colorOverride = detail.color.segments?.[segment.segment_id];
  const explicitlyEnabled = colorOverride?.enabled === true;
  const colorEnabled = (detail.color.enabled || explicitlyEnabled)
    && colorOverride?.enabled !== false
    && colorOverride?.excluded !== true;
  const startSeconds = finiteOr(segment.start_seconds, 0);
  const endSeconds = finiteOr(segment.end_seconds, startSeconds);
  const speed = Math.min(4, Math.max(0.25, finiteOr(segment.speed, 1)));

  return {
    id: segment.segment_id,
    title: segment.title || segment.segment_id,
    sourceName: segment.source_filename || segment.clip_id || "未知素材",
    groupId,
    groupTitle,
    groupOrder,
    order: storyboard?.order ?? segment.storyboard_order ?? segment.manual_order ?? Number.MAX_SAFE_INTEGER,
    included: storyboard?.included ?? segment.include ?? true,
    locked: storyboard?.locked ?? segment.storyboard_locked ?? false,
    thumbnailUrl: storyboard?.thumbnail_url || "",
    thumbnailRatio: storyboard?.thumbnail_time_ratio ?? segment.thumbnail_time_ratio ?? 0.5,
    startSeconds,
    endSeconds,
    speed,
    durationSeconds: Math.max(0, endSeconds - startSeconds) / speed,
    score: finiteOr(segment.score, 0),
    sceneRole: segment.scene_role || "未分類",
    storyPosition: segment.story_position || "未指定",
    suggestedUse: segment.suggested_use || "未指定",
    notes: storyboard?.notes ?? segment.storyboard_notes ?? segment.user_notes ?? "",
    audioRole,
    audioLabel: AUDIO_LABELS[audioRole],
    audioCustomized: Boolean(audioOverride && typeof audioOverride === "object"),
    colorEnabled,
    colorCustomized: Boolean(colorOverride),
    colorWarnings: [...(colorOverride?.warnings || [])],
  };
}

function normalizeAudioRole(value: string | undefined): AudioSegmentSettings["role"] {
  if (value === "keep" || value === "keep_original") return "keep";
  if (value === "mute") return "mute";
  if (value === "bgm_only") return "bgm_only";
  return "lower";
}

function readableGroupName(value: string): string {
  if (!value || value === "ungrouped") return "未分組";
  return value.replaceAll("_", " ");
}

function finiteOr(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) ? Number(value) : fallback;
}
