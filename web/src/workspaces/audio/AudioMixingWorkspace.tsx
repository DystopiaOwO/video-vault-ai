import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AudioSegmentOverride,
  type AudioSegmentSettings,
  type AudioState,
  type BgmTrack,
  type Job,
  type ProjectDetail,
  type Segment,
} from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import { createProjectMutationControls, mutationLabel, ProjectMutationCoordinator, type ProjectMutationControls } from "../../projectMutation";
import "./audio-mixing-workspace.css";

export type AudioMixingWorkspaceProps = {
  detail: ProjectDetail;
  bgmTracks: BgmTrack[];
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
  mutationControls?: ProjectMutationControls;
};

type PreviewInfo = {
  cacheHit: boolean;
  duration: number;
  start: number;
  segmentId?: string;
};

const FALLBACK_AUDIO: AudioState = {
  schema_version: 1,
  enabled: true,
  bgm: {
    bgm_id: null,
    enabled: false,
    volume_db: -18,
    start_seconds: 0,
    loop: true,
    fade_in_seconds: 1.5,
    fade_out_seconds: 2,
  },
  original_audio: {
    default_role: "lower",
    default_volume_db: 0,
    lower_volume_db: -8,
    fade_in_seconds: .1,
    fade_out_seconds: .1,
  },
  normalization: {
    enabled: true,
    target_lufs: -14,
    true_peak_db: -1,
  },
  segments: {},
};

export function AudioMixingWorkspace({ detail, bgmTracks, setMessage, refreshProject, mutationControls }: AudioMixingWorkspaceProps) {
  const [state, setState] = useState<AudioState>(() => cloneAudioState(detail.audio || FALLBACK_AUDIO));
  const [baseline, setBaseline] = useState<AudioState>(() => cloneAudioState(detail.audio || FALLBACK_AUDIO));
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewInfo, setPreviewInfo] = useState<PreviewInfo | null>(null);
  const [busy, setBusy] = useState<"" | "save" | "preview">("");
  const [segmentQuery, setSegmentQuery] = useState("");
  const projectIdRef = useRef(detail.project.id);
  const dirty = useMemo(() => audioSignature(state) !== audioSignature(baseline), [baseline, state]);
  const dirtyRef = useRef(dirty);
  const fallbackControlsRef = useRef<ProjectMutationControls | null>(null);
  if (!fallbackControlsRef.current) fallbackControlsRef.current = createProjectMutationControls(new ProjectMutationCoordinator());
  const controls = mutationControls || fallbackControlsRef.current;

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  useEffect(() => {
    const incoming = cloneAudioState(detail.audio || FALLBACK_AUDIO);
    const projectChanged = projectIdRef.current !== detail.project.id;
    if (projectChanged) {
      projectIdRef.current = detail.project.id;
      setState(incoming);
      setBaseline(incoming);
      clearPreview();
      setSegmentQuery("");
      setBusy("");
      return;
    }
    if (!dirtyRef.current) {
      setState(incoming);
      setBaseline(incoming);
    }
  }, [detail.audio, detail.project.id]);

  const filteredSegments = useMemo(() => {
    const query = segmentQuery.trim().toLocaleLowerCase();
    if (!query) return detail.segments;
    return detail.segments.filter((segment) => [segment.title, segment.segment_id, segment.clip_id, segment.scene_role]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query)));
  }, [detail.segments, segmentQuery]);

  function clearPreview() {
    setPreviewUrl("");
    setPreviewInfo(null);
  }

  function applyState(updater: (current: AudioState) => AudioState) {
    setState((current) => updater(current));
    clearPreview();
  }

  function patchState(patch: Partial<AudioState>) {
    applyState((current) => ({ ...current, ...patch }));
  }

  function updateSegment(segmentId: string, patch: AudioSegmentOverride) {
    applyState((current) => ({
      ...current,
      segments: {
        ...current.segments,
        [segmentId]: {
          ...(current.segments[segmentId] || {}),
          ...patch,
        },
      },
    }));
  }

  function resetSegment(segmentId: string) {
    applyState((current) => ({
      ...current,
      segments: {
        ...current.segments,
        [segmentId]: null,
      },
    }));
  }

  function resetAll() {
    setState(cloneAudioState(baseline));
    clearPreview();
    setMessage("已放棄尚未儲存的音訊設定。");
  }

  async function save() {
    if (!dirty || busy) return;
    const mutation = controls.beginProjectMutation(detail.project.id, "audio");
    if (!mutation) {
      setMessage(`目前正在${mutationLabel("audio")}，請完成後再執行其他操作。`);
      return;
    }
    setBusy("save");
    setMessage("正在儲存音訊設定…");
    try {
      const result = await api.audioSettings(detail.project.id, {
        enabled: state.enabled,
        bgm: state.bgm,
        original_audio: state.original_audio,
        normalization: state.normalization,
        segments: state.segments,
      });
      if (!result.ok) {
        setMessage(`音訊設定儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      const saved = cloneAudioState(result.state || state);
      setState(saved);
      setBaseline(saved);
      clearPreview();
      setMessage("音訊設定已儲存，專案已回到待審。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`音訊設定儲存失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
      controls.finishProjectMutation(mutation);
    }
  }

  async function preview(segmentId = "", force = false) {
    if (busy) return;
    setBusy("preview");
    setMessage(force ? "正在忽略快取並重新產生音訊預覽…" : "正在產生音訊預覽…");
    try {
      const result = await api.audioPreview(detail.project.id, {
        segmentId,
        durationSeconds: 12,
        patch: {
          enabled: state.enabled,
          bgm: state.bgm,
          original_audio: state.original_audio,
          normalization: state.normalization,
          segments: state.segments,
        },
        force,
      });
      if (!result.ok) {
        setMessage(`音訊預覽失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviewUrl(result.url || "");
      setPreviewInfo({
        cacheHit: Boolean(result.cache_hit),
        duration: Number(result.duration_seconds || 0),
        start: Number(result.timeline_start_seconds || 0),
        segmentId: segmentId || undefined,
      });
      setMessage(force
        ? "音訊預覽已忽略快取重新產生。"
        : result.cache_hit
          ? "音訊預覽已從快取載入。"
          : "音訊預覽完成。");
    } catch (error) {
      setMessage(`音訊預覽失敗：${errorMessage(error)}`);
    } finally {
      setBusy("");
    }
  }

  const invalidBgmOnly = state.enabled
    && (!state.bgm.enabled || !state.bgm.bgm_id)
    && (state.original_audio.default_role === "bgm_only" || Object.values(state.segments).some((item) => item?.role === "bgm_only"));

  return <div className="audio-workspace">
    <section className="audio-project-panel">
      <header className="audio-workspace-header">
        <div>
          <span>AUDIO MIXING</span>
          <h3>音訊混音與 BGM</h3>
          <p>先調整草稿並產生短預覽，確認後再儲存到專案。</p>
        </div>
        <div className="audio-header-actions">
          {dirty && <strong>有未儲存變更</strong>}
          <button type="button" disabled={Boolean(busy) || !dirty} onClick={resetAll}>放棄變更</button>
          <button type="button" className="good" disabled={Boolean(busy) || !dirty || invalidBgmOnly} onClick={() => void save()}>{busy === "save" ? "儲存中…" : "儲存音訊設定"}</button>
        </div>
      </header>

      <label className="audio-toggle"><input type="checkbox" disabled={Boolean(busy)} checked={state.enabled} onChange={(event) => patchState({ enabled: event.target.checked })} /> 啟用專案音訊設定</label>

      <div className="audio-settings-grid">
        <section>
          <h4>BGM</h4>
          <div className="audio-form-grid">
            <label className="wide">音樂<select disabled={Boolean(busy)} value={state.bgm.bgm_id ?? ""} onChange={(event) => patchState({ bgm: { ...state.bgm, bgm_id: event.target.value ? Number(event.target.value) : null, enabled: Boolean(event.target.value) } })}><option value="">不使用</option>{bgmTracks.map((track) => <option key={track.id} value={track.id}>{track.title}</option>)}</select></label>
            <label>音量 dB<input disabled={Boolean(busy)} type="number" min={-60} max={12} step={1} value={state.bgm.volume_db} onChange={(event) => patchState({ bgm: { ...state.bgm, volume_db: Number(event.target.value) } })} /></label>
            <label>起始秒數<input disabled={Boolean(busy)} type="number" min={0} step={0.1} value={state.bgm.start_seconds} onChange={(event) => patchState({ bgm: { ...state.bgm, start_seconds: Number(event.target.value) } })} /></label>
            <label>淡入秒數<input disabled={Boolean(busy)} type="number" min={0} step={0.1} value={state.bgm.fade_in_seconds} onChange={(event) => patchState({ bgm: { ...state.bgm, fade_in_seconds: Number(event.target.value) } })} /></label>
            <label>淡出秒數<input disabled={Boolean(busy)} type="number" min={0} step={0.1} value={state.bgm.fade_out_seconds} onChange={(event) => patchState({ bgm: { ...state.bgm, fade_out_seconds: Number(event.target.value) } })} /></label>
            <label className="wide audio-toggle"><input type="checkbox" disabled={Boolean(busy)} checked={state.bgm.loop} onChange={(event) => patchState({ bgm: { ...state.bgm, loop: event.target.checked } })} /> BGM 循環</label>
          </div>
        </section>

        <section>
          <h4>原音與正規化</h4>
          <div className="audio-form-grid">
            <label className="wide">原音預設角色<select disabled={Boolean(busy)} value={state.original_audio.default_role} onChange={(event) => patchState({ original_audio: { ...state.original_audio, default_role: event.target.value as AudioSegmentSettings["role"] } })}><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
            <label>降低原音 dB<input disabled={Boolean(busy)} type="number" min={-60} max={12} step={1} value={state.original_audio.lower_volume_db} onChange={(event) => patchState({ original_audio: { ...state.original_audio, lower_volume_db: Number(event.target.value) } })} /></label>
            <label>目標 LUFS<input disabled={Boolean(busy) || !state.normalization.enabled} type="number" min={-40} max={0} step={1} value={state.normalization.target_lufs} onChange={(event) => patchState({ normalization: { ...state.normalization, target_lufs: Number(event.target.value) } })} /></label>
            <label>True Peak dB<input disabled={Boolean(busy) || !state.normalization.enabled} type="number" min={-20} max={0} step={0.1} value={state.normalization.true_peak_db} onChange={(event) => patchState({ normalization: { ...state.normalization, true_peak_db: Number(event.target.value) } })} /></label>
            <label className="wide audio-toggle"><input type="checkbox" disabled={Boolean(busy)} checked={state.normalization.enabled} onChange={(event) => patchState({ normalization: { ...state.normalization, enabled: event.target.checked } })} /> 音量正規化</label>
          </div>
        </section>
      </div>

      {invalidBgmOnly && <div className="audio-warning">有片段設定為只留 BGM，但目前沒有有效 BGM。請先選擇音樂，否則無法儲存。</div>}

      <div className="audio-preview-toolbar">
        <button type="button" disabled={Boolean(busy)} onClick={() => void preview()}>{busy === "preview" ? "預覽產生中…" : "產生 12 秒預覽"}</button>
        <button type="button" disabled={Boolean(busy)} onClick={() => void preview("", true)}>忽略快取重跑</button>
        {previewInfo && <span>範圍 {previewInfo.start.toFixed(1)} 秒 · 長度 {previewInfo.duration.toFixed(1)} 秒 · {previewInfo.cacheHit ? "快取" : "新產生"}</span>}
      </div>
      {previewUrl && <video controls preload="metadata" src={previewUrl} />}
      {state.bgm.track && <p className="audio-track-meta">目前 BGM：{state.bgm.track.title} · 作者 {state.bgm.track.artist || "未知"} · 授權 {state.bgm.track.license_name || "未填"}</p>}
    </section>

    <section className="audio-segment-panel">
      <header className="audio-segment-heading">
        <div><h3>片段原音角色</h3><p>只有需要例外處理的片段才建立覆寫。</p></div>
        <label><span>搜尋片段</span><input type="search" aria-label="搜尋音訊片段" value={segmentQuery} onChange={(event) => setSegmentQuery(event.target.value)} placeholder="標題、片段或素材編號" /></label>
      </header>
      <div className="audio-segment-list">
        {filteredSegments.map((segment) => <AudioSegmentRow
          key={segment.segment_id}
          segment={segment}
          settings={effectiveSegment(state, segment)}
          customized={Boolean(state.segments[segment.segment_id])}
          busy={Boolean(busy)}
          onChange={(patch) => updateSegment(segment.segment_id, patch)}
          onReset={() => resetSegment(segment.segment_id)}
          onPreview={(force) => void preview(segment.segment_id, force)}
        />)}
        {filteredSegments.length === 0 && <div className="audio-empty">找不到符合條件的片段。</div>}
      </div>
    </section>
  </div>;
}

function AudioSegmentRow({ segment, settings, customized, busy, onChange, onReset, onPreview }: {
  segment: Segment;
  settings: AudioSegmentSettings;
  customized: boolean;
  busy: boolean;
  onChange: (patch: AudioSegmentOverride) => void;
  onReset: () => void;
  onPreview: (force: boolean) => void;
}) {
  return <div className="audio-segment-row">
    <div className="audio-segment-title">
      <b>{segment.title || segment.segment_id}</b>
      <span>{segment.clip_id} · {customized ? "片段自訂" : "專案預設"}</span>
    </div>
    <label>角色<select disabled={busy} value={settings.role} onChange={(event) => onChange({ role: event.target.value as AudioSegmentSettings["role"] })}><option value="keep">保留</option><option value="lower">降低</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
    <label>音量 dB<input aria-label={`${segment.title || segment.segment_id} 音量`} disabled={busy || settings.role === "mute" || settings.role === "bgm_only"} type="number" min={-60} max={12} step={1} value={settings.volume_db} onChange={(event) => onChange({ volume_db: Number(event.target.value) })} /></label>
    <label>淡入<input disabled={busy} type="number" min={0} step={.1} value={settings.fade_in_seconds} onChange={(event) => onChange({ fade_in_seconds: Number(event.target.value) })} /></label>
    <label>淡出<input disabled={busy} type="number" min={0} step={.1} value={settings.fade_out_seconds} onChange={(event) => onChange({ fade_out_seconds: Number(event.target.value) })} /></label>
    <label className="audio-toggle"><input type="checkbox" disabled={busy} checked={settings.locked} onChange={(event) => onChange({ locked: event.target.checked })} /> 鎖定</label>
    <div className="audio-row-actions">
      <button type="button" disabled={busy || !customized} onClick={onReset}>恢復預設</button>
      <button type="button" disabled={busy} onClick={() => onPreview(false)}>預覽</button>
      <button type="button" disabled={busy} onClick={() => onPreview(true)}>重跑</button>
    </div>
  </div>;
}

function effectiveSegment(state: AudioState, segment: Segment): AudioSegmentSettings {
  const configured = state.segments[segment.segment_id];
  const override = configured && typeof configured === "object" ? configured : {};
  const role = override.role || state.original_audio.default_role || (segment.audio_role === "keep_original" ? "keep" : segment.audio_role === "mute" ? "mute" : "lower");
  const defaultVolume = role === "lower" ? state.original_audio.lower_volume_db : state.original_audio.default_volume_db;
  return {
    role,
    volume_db: override.volume_db ?? (role === "mute" || role === "bgm_only" ? 0 : defaultVolume),
    fade_in_seconds: override.fade_in_seconds ?? (state.original_audio.fade_in_seconds ?? .1),
    fade_out_seconds: override.fade_out_seconds ?? (state.original_audio.fade_out_seconds ?? .1),
    locked: Boolean(override.locked),
  };
}

function cloneAudioState(state: AudioState): AudioState {
  return JSON.parse(JSON.stringify(state)) as AudioState;
}

function audioSignature(state: AudioState): string {
  return JSON.stringify({
    enabled: state.enabled,
    bgm: state.bgm,
    original_audio: state.original_audio,
    normalization: state.normalization,
    segments: state.segments,
  });
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "網路或服務錯誤";
}
