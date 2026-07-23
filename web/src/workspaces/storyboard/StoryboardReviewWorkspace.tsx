import { useState } from "react";
import type { AudioSegmentSettings } from "../../api";
import {
  type StoryboardSegmentEdit,
  type StoryboardSegmentView,
  type StoryboardViewModel,
  validateSegmentTiming,
} from "./storyboardViewModel";
import "./storyboard-review.css";

export type SegmentTimingPatch = Partial<{
  startSeconds: number;
  endSeconds: number;
  speed: number;
}>;

export type StoryboardPreviewMode = "segment" | "transition" | "range";

export type StoryboardPreviewItem = {
  kind: string;
  url?: string;
  durationSeconds: number;
};

export type StoryboardReviewWorkspaceProps = {
  model: StoryboardViewModel;
  selectedId?: string;
  dirty?: boolean;
  saving?: boolean;
  regenerating?: boolean;
  previewing?: boolean;
  thumbnailing?: boolean;
  timingDrafts?: Record<string, { startSeconds: number; endSeconds: number; speed: number }>;
  previewItems?: StoryboardPreviewItem[];
  onSelect: (segmentId: string) => void;
  onStoryboardChange: (segmentId: string, patch: Partial<StoryboardSegmentEdit>) => void;
  onTimingChange: (segmentId: string, patch: SegmentTimingPatch) => void;
  onSaveTiming: (segmentId: string) => void;
  onSave: () => void;
  onRegenerate: () => void;
  onPreview: (segmentId: string, mode: StoryboardPreviewMode, force?: boolean) => void;
  onAudioRoleChange?: (segmentId: string, role: AudioSegmentSettings["role"] | "default") => void;
  onToggleColor?: (segmentId: string) => void;
  onResetColor?: (segmentId: string) => void;
  onGenerateThumbnail?: (segmentId: string, ratio: number, force?: boolean) => void;
  onMoveSegment?: (segmentId: string, delta: number) => void;
  onAddGroup?: (title: string) => void;
  onRenameGroup?: (groupId: string, title: string) => void;
  onMoveGroup?: (groupId: string, delta: number) => void;
  onDeleteGroup?: (groupId: string) => void;
};

export function StoryboardReviewWorkspace({
  model,
  selectedId,
  dirty = false,
  saving = false,
  regenerating = false,
  previewing = false,
  thumbnailing = false,
  timingDrafts = {},
  previewItems = [],
  onSelect,
  onStoryboardChange,
  onTimingChange,
  onSaveTiming,
  onSave,
  onRegenerate,
  onPreview,
  onAudioRoleChange,
  onToggleColor,
  onResetColor,
  onGenerateThumbnail,
  onMoveSegment,
  onAddGroup,
  onRenameGroup,
  onMoveGroup,
  onDeleteGroup,
}: StoryboardReviewWorkspaceProps) {
  const [newGroupTitle, setNewGroupTitle] = useState("");
  const selected = model.segments.find((segment) => segment.id === selectedId) || model.segments[0];

  function addGroup() {
    const title = newGroupTitle.trim();
    if (!title || !onAddGroup) return;
    onAddGroup(title);
    setNewGroupTitle("");
  }

  if (!model.exists || model.segments.length === 0) {
    return <section className="review-empty" aria-label="分鏡審核">
      <span>STORYBOARD REVIEW</span>
      <h2>尚未建立分鏡</h2>
      <p>先依內容感知結果建立分鏡，再進行片段排序、剪點與人工審核。</p>
      <button type="button" className="review-primary" disabled={regenerating} onClick={onRegenerate}>
        {regenerating ? "建立中…" : "建立分鏡"}
      </button>
    </section>;
  }

  return <section className="review-workspace" aria-label="分鏡審核工作區">
    <div className="review-browser">
      <header className="review-toolbar">
        <div>
          <span>STORYBOARD REVIEW</span>
          <h2>分鏡片段</h2>
          <p>依敘事分組檢視片段；完整設定集中在右側 Inspector。</p>
        </div>
        <div className="review-toolbar-actions">
          {dirty && <strong className="review-dirty">有未儲存變更</strong>}
          <button type="button" disabled={regenerating || dirty} onClick={onRegenerate}>{regenerating ? "重建中…" : "重新產生"}</button>
          <button type="button" className="review-primary" disabled={saving || !dirty} onClick={onSave}>{saving ? "儲存中…" : "儲存分鏡"}</button>
        </div>
      </header>

      <div className="review-summary" aria-label="分鏡摘要">
        <span>共 {model.summary.totalSegments} 段</span>
        <span>納入 {model.summary.includedSegments} 段</span>
        <span>排除 {model.summary.excludedSegments} 段</span>
        <span>預估 {formatDuration(model.summary.estimatedDurationSeconds)}</span>
      </div>

      {!model.valid && <div className="review-validation error"><b>分鏡尚未通過檢查</b>{model.errors.map((error) => <span key={error}>{error}</span>)}</div>}
      {model.warnings.length > 0 && <div className="review-validation warning">{model.warnings.map((warning) => <span key={warning}>{warning}</span>)}</div>}

      {onAddGroup && <div className="review-add-group">
        <input aria-label="新增分組名稱" value={newGroupTitle} onChange={(event) => setNewGroupTitle(event.target.value)} placeholder="新增分組名稱" onKeyDown={(event) => { if (event.key === "Enter") addGroup(); }} />
        <button type="button" disabled={!newGroupTitle.trim()} onClick={addGroup}>新增分組</button>
      </div>}

      <div className="review-groups">
        {model.groups.map((group, groupIndex) => <section className="review-group" key={group.id}>
          <header>
            <div className="review-group-copy">
              {onRenameGroup
                ? <input aria-label={`${group.title} 分組名稱`} value={group.title} onChange={(event) => onRenameGroup(group.id, event.target.value)} />
                : <b>{group.title}</b>}
              <small>{group.segments.length} 個片段 · {formatDuration(group.segments.filter((segment) => segment.included).reduce((total, segment) => total + segment.durationSeconds, 0))}</small>
            </div>
            <div className="review-group-actions">
              <span>{group.category}</span>
              {onMoveGroup && <button type="button" aria-label={`${group.title} 分組上移`} disabled={groupIndex === 0} onClick={() => onMoveGroup(group.id, -1)}>↑</button>}
              {onMoveGroup && <button type="button" aria-label={`${group.title} 分組下移`} disabled={groupIndex === model.groups.length - 1} onClick={() => onMoveGroup(group.id, 1)}>↓</button>}
              {onDeleteGroup && group.segments.length === 0 && <button type="button" onClick={() => onDeleteGroup(group.id)}>刪除</button>}
            </div>
          </header>
          <div className="review-segment-list">
            {group.segments.map((segment, index) => <SegmentRow
              key={segment.id}
              segment={segment}
              index={index}
              selected={selected?.id === segment.id}
              onSelect={onSelect}
            />)}
          </div>
        </section>)}
      </div>
    </div>

    {selected && <SegmentInspector
      segment={selected}
      groups={model.groups.map((group) => ({ id: group.id, title: group.title }))}
      timingDraft={timingDrafts[selected.id]}
      previewing={previewing}
      thumbnailing={thumbnailing}
      previewItems={previewItems}
      onStoryboardChange={onStoryboardChange}
      onTimingChange={onTimingChange}
      onSaveTiming={onSaveTiming}
      onPreview={onPreview}
      onAudioRoleChange={onAudioRoleChange}
      onToggleColor={onToggleColor}
      onResetColor={onResetColor}
      onGenerateThumbnail={onGenerateThumbnail}
      onMoveSegment={onMoveSegment}
    />}
  </section>;
}

function SegmentRow({ segment, index, selected, onSelect }: {
  segment: StoryboardSegmentView;
  index: number;
  selected: boolean;
  onSelect: (segmentId: string) => void;
}) {
  return <button
    type="button"
    className={`review-segment${selected ? " selected" : ""}${segment.included ? "" : " excluded"}`}
    aria-pressed={selected}
    onClick={() => onSelect(segment.id)}
  >
    <span className="review-order" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
    <span className="review-thumbnail">
      {segment.thumbnailUrl ? <img src={segment.thumbnailUrl} alt="" /> : <i>{String(index + 1).padStart(2, "0")}</i>}
    </span>
    <span className="review-segment-copy">
      <b>{segment.title}</b>
      <small title={segment.sourceName}>{segment.sourceName}</small>
      <span>{segment.startSeconds.toFixed(1)}–{segment.endSeconds.toFixed(1)} 秒 · 成片 {segment.durationSeconds.toFixed(1)} 秒 · AI {segment.score.toFixed(2)}</span>
    </span>
    <span className="review-tags">
      <i>{segment.sceneRole}</i>
      <i className={segment.included ? "success" : "muted"}>{segment.included ? "已納入" : "已排除"}</i>
      {segment.locked && <i>已鎖定</i>}
    </span>
  </button>;
}

function SegmentInspector({
  segment,
  groups,
  timingDraft,
  previewing,
  thumbnailing,
  previewItems,
  onStoryboardChange,
  onTimingChange,
  onSaveTiming,
  onPreview,
  onAudioRoleChange,
  onToggleColor,
  onResetColor,
  onGenerateThumbnail,
  onMoveSegment,
}: {
  segment: StoryboardSegmentView;
  groups: Array<{ id: string; title: string }>;
  timingDraft?: { startSeconds: number; endSeconds: number; speed: number };
  previewing: boolean;
  thumbnailing: boolean;
  previewItems: StoryboardPreviewItem[];
  onStoryboardChange: (segmentId: string, patch: Partial<StoryboardSegmentEdit>) => void;
  onTimingChange: (segmentId: string, patch: SegmentTimingPatch) => void;
  onSaveTiming: (segmentId: string) => void;
  onPreview: (segmentId: string, mode: StoryboardPreviewMode, force?: boolean) => void;
  onAudioRoleChange?: (segmentId: string, role: AudioSegmentSettings["role"] | "default") => void;
  onToggleColor?: (segmentId: string) => void;
  onResetColor?: (segmentId: string) => void;
  onGenerateThumbnail?: (segmentId: string, ratio: number, force?: boolean) => void;
  onMoveSegment?: (segmentId: string, delta: number) => void;
}) {
  const [ignoreCache, setIgnoreCache] = useState(false);
  const timing = timingDraft || {
    startSeconds: segment.startSeconds,
    endSeconds: segment.endSeconds,
    speed: segment.speed,
  };
  const timingErrors = validateSegmentTiming(timing.startSeconds, timing.endSeconds, timing.speed);
  const outputDuration = Math.max(0, timing.endSeconds - timing.startSeconds) / Math.max(0.25, timing.speed);

  return <aside className="review-inspector" aria-label="片段設定">
    <header><span>SEGMENT INSPECTOR</span><h2>片段設定</h2></header>
    <div className="review-inspector-preview">
      {segment.thumbnailUrl ? <img src={segment.thumbnailUrl} alt={`${segment.title} 代表畫格`} /> : <span>尚未產生代表畫格</span>}
      <div><b>{segment.title}</b><small title={segment.sourceName}>{segment.sourceName}</small></div>
    </div>

    <div className="review-toggle-grid">
      <label><input type="checkbox" checked={segment.included} onChange={(event) => onStoryboardChange(segment.id, { included: event.target.checked })} />納入成片</label>
      <label><input type="checkbox" checked={segment.locked} onChange={(event) => onStoryboardChange(segment.id, { locked: event.target.checked })} />鎖定片段</label>
    </div>

    {onMoveSegment && <div className="review-order-actions">
      <button type="button" onClick={() => onMoveSegment(segment.id, -1)}>片段上移</button>
      <button type="button" onClick={() => onMoveSegment(segment.id, 1)}>片段下移</button>
    </div>}

    <div className="review-form-grid">
      <label className="wide">分組<select aria-label="片段分組" value={segment.groupId} onChange={(event) => onStoryboardChange(segment.id, { group_id: event.target.value })}>{groups.map((group) => <option key={group.id} value={group.id}>{group.title}</option>)}</select></label>
      <label>起點（秒）<input aria-label="片段起點" type="number" min={0} step={0.001} value={timing.startSeconds} onChange={(event) => onTimingChange(segment.id, { startSeconds: Number(event.target.value) })} /></label>
      <label>終點（秒）<input aria-label="片段終點" type="number" min={0} step={0.001} value={timing.endSeconds} onChange={(event) => onTimingChange(segment.id, { endSeconds: Number(event.target.value) })} /></label>
      <label>速度<select aria-label="片段速度" value={timing.speed} onChange={(event) => onTimingChange(segment.id, { speed: Number(event.target.value) })}><option value={0.5}>0.50×</option><option value={0.75}>0.75×</option><option value={1}>1.00×</option><option value={1.15}>1.15×</option><option value={1.25}>1.25×</option><option value={1.5}>1.50×</option><option value={2}>2.00×</option></select></label>
      <label>成片長度<input aria-label="成片長度" readOnly value={`${outputDuration.toFixed(1)} 秒`} /></label>
      <label className="wide">代表畫格<select aria-label="代表畫格位置" value={segment.thumbnailRatio} onChange={(event) => onStoryboardChange(segment.id, { thumbnail_time_ratio: Number(event.target.value) })}><option value={0.25}>片段 25%</option><option value={0.5}>片段 50%</option><option value={0.75}>片段 75%</option></select></label>
      {onGenerateThumbnail && <button type="button" className="review-wide-button" disabled={thumbnailing} onClick={() => onGenerateThumbnail(segment.id, segment.thumbnailRatio, ignoreCache)}>{thumbnailing ? "產生中…" : "產生代表畫格"}</button>}
      <label className="wide"><input aria-label="忽略快取並強制重跑" type="checkbox" checked={ignoreCache} onChange={(event) => setIgnoreCache(event.target.checked)} />忽略快取並強制重跑</label>
      <label className="wide">原音角色<select aria-label="原音角色" value={segment.audioCustomized ? segment.audioRole : "default"} disabled={!onAudioRoleChange} onChange={(event) => onAudioRoleChange?.(segment.id, event.target.value as AudioSegmentSettings["role"] | "default")}><option value="default">使用專案預設（{segment.audioLabel}）</option><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
      <label className="wide">分鏡備註<textarea aria-label="分鏡備註" value={segment.notes} onChange={(event) => onStoryboardChange(segment.id, { notes: event.target.value })} /></label>
    </div>

    <div className="review-effective-state">
      <div><span>調色</span><b>{segment.colorEnabled ? "啟用" : "停用"}{segment.colorCustomized ? " · 片段自訂" : " · 專案預設"}</b></div>
      <div className="review-effective-actions">
        <button type="button" disabled={!onToggleColor} onClick={() => onToggleColor?.(segment.id)}>{segment.colorEnabled ? "停用此片段" : "啟用此片段"}</button>
        {segment.colorCustomized && onResetColor && <button type="button" onClick={() => onResetColor(segment.id)}>恢復專案預設</button>}
      </div>
      {segment.colorWarnings.map((warning) => <small key={warning}>{warning}</small>)}
    </div>

    {timingErrors.length > 0 && <div className="review-timing-errors" role="alert">{timingErrors.map((error) => <span key={error}>{error}</span>)}</div>}
    <button type="button" className="review-save-timing" disabled={timingErrors.length > 0} onClick={() => onSaveTiming(segment.id)}>儲存剪點</button>

    <div className="review-preview-actions">
      <button type="button" disabled={previewing} onClick={() => onPreview(segment.id, "transition", ignoreCache)}>預覽前後銜接</button>
      <button type="button" disabled={previewing} onClick={() => onPreview(segment.id, "range", ignoreCache)}>從此片段預覽 8 秒</button>
      <button type="button" className="review-primary" disabled={previewing} onClick={() => onPreview(segment.id, "segment", ignoreCache)}>{previewing ? "產生中…" : "產生 5 秒預覽"}</button>
    </div>

    {previewItems.length > 0 && <div className="review-preview-results" aria-label="分鏡預覽結果">
      {previewItems.map((item, index) => <div key={`${item.kind}-${index}`}>
        <b>{previewLabel(item.kind)} · {item.durationSeconds.toFixed(1)} 秒</b>
        {item.url ? <video controls src={item.url} /> : <span>預覽檔案未提供網址</span>}
      </div>)}
    </div>}
  </aside>;
}

function previewLabel(kind: string): string {
  if (kind === "incoming") return "前段銜接";
  if (kind === "outgoing") return "後段銜接";
  if (kind === "range") return "分鏡範圍";
  return "片段預覽";
}

function formatDuration(value: number): string {
  const seconds = Math.max(0, Math.round(value));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
