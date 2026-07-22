import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { ChangeEvent, ReactNode } from "react";
import { api, AudioSegmentOverride, AudioSegmentSettings, AudioState, BgmTrack, ColorAdjustment, ColorSegmentState, ColorState, ColorStatePatch, Job, Project, ProjectDetail, Segment } from "./api";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { ProjectDataLoadOptions, ProjectDataLoader } from "./projectDataLoader";
import "./styles.css";

export function App() {
  if (window.location.pathname === "/bgm") return <BgmPage />;
  const [projects, setProjects] = useState<Project[]>([]);
  const [currentId, setCurrentId] = useState<number>(0);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [bgmTracks, setBgmTracks] = useState<BgmTrack[]>([]);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const currentIdRef = useRef(0);
  const mountedRef = useRef(true);
  const loaderRef = useRef<ProjectDataLoader | null>(null);
  if (!loaderRef.current) {
    loaderRef.current = new ProjectDataLoader(
      { project: api.project, jobs: api.jobs },
      (projectId) => projectId === currentIdRef.current,
      () => mountedRef.current,
      (project, nextJobs) => {
        setDetail(project);
        setJobs(nextJobs);
      },
      (error) => setMessage(`狀態更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`),
    );
  }

  useEffect(() => {
    loadProjects();
    api.bgm().then(setBgmTracks).catch((error) => setMessage(`BGM 載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }, []);

  function loadProjects() {
    return api.projects().then((rows) => {
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
    }).catch((error) => setMessage(`專案載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }

  useEffect(() => {
    currentIdRef.current = currentId;
  }, [currentId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      loaderRef.current?.invalidate();
    };
  }, []);

  function loadProjectData(requestedProjectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loaderRef.current?.load(requestedProjectId, options) || Promise.resolve([]);
  }

  useEffect(() => {
    if (!currentId) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      if (cancelled) return;
      await loadProjectData(currentId);
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1500);
    };
    void poll();
    return () => {
      cancelled = true;
      loaderRef.current?.invalidate();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentId]);

  async function review(action: "approve" | "reject") {
    if (!detail) return;
    setMessage("送出中...");
    action === "approve" ? await api.approve(detail.project.id, notes) : await api.reject(detail.project.id, notes);
    setNotes("");
    setMessage(action === "approve" ? "已核准專案" : "已退回修改");
    setDetail(await api.project(detail.project.id));
  }

  async function revise() {
    if (!detail) return;
    setMessage("正在依備註重建故事...");
    await api.revise(detail.project.id, notes);
    setMessage("故事整理已依備註重建");
    setDetail(await api.project(detail.project.id));
  }

  async function createProject() {
    setMessage("正在建立專案...");
    const result = await api.createProject(newProjectName);
    setNewProjectName("");
    await loadProjects();
    setCurrentId(result.id);
    setMessage("專案已建立，下一步請匯入素材。");
  }

  async function refreshProject(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loadProjectData(projectId, options);
  }

  return (
    <main>
      <aside>
        <h1>video-vault-ai</h1>
        <a className="nav" href="/bgm">BGM 資料庫</a>
        <a className="nav" href="/classic-bgm">舊版 BGM 上傳</a>
        <a className="nav" href="/classic">舊版工作台</a>
        <div className="new-project">
          <input value={newProjectName} onChange={(e) => setNewProjectName(e.target.value)} placeholder="新專案名稱" />
          <button onClick={createProject}>新增專案</button>
        </div>
        <h2>專案</h2>
        {projects.map((p) => (
          <button key={p.id} className={p.id === currentId ? "project active" : "project"} onClick={() => setCurrentId(p.id)}>
            <b>{p.name}</b>
            <span>#{p.id} | {p.status} | {p.video_count ?? 0} clips</span>
          </button>
        ))}
      </aside>
      <section>
        {message && <div className="notice">{message}</div>}
        {!detail ? <div className="card">尚未選擇專案</div> : <ProjectView key={detail.project.id} detail={detail} jobs={jobs} bgmTracks={bgmTracks} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} review={review} revise={revise} />}
      </section>
    </main>
  );
}

function BgmPage() {
  const [tracks, setTracks] = useState<BgmTrack[]>([]);
  useEffect(() => {
    api.bgm().then(setTracks);
  }, []);
  return (
    <main>
      <aside>
        <h1>BGM 資料庫</h1>
        <a className="nav" href="/">專案工作台</a>
        <a className="nav" href="/classic-bgm">上傳 BGM</a>
      </aside>
      <section>
        <div className="hero"><div><h2>本地 BGM 總覽</h2><p>{tracks.length} 首可用音樂</p></div></div>
        <div className="grid">
          {tracks.map((track) => (
            <Card key={track.id} title={track.title}>
              <p>{track.artist || "未知作者"} | {track.license_name || "未填授權"} | {track.mood || "未分類"}</p>
              {track.source_url && <p><a href={track.source_url} target="_blank">來源</a></p>}
              <pre>{track.attribution_text || "尚未填寫 YouTube 署名文字。"}</pre>
            </Card>
          ))}
          {!tracks.length && <Card title="尚無 BGM"><p>請先到舊版 BGM 上傳頁登錄本地音樂。</p></Card>}
        </div>
      </section>
    </main>
  );
}

function ProjectView({ detail, jobs, bgmTracks, notes, setNotes, setMessage, refreshProject, review, revise }: {
  detail: ProjectDetail;
  jobs: Job[];
  bgmTracks: BgmTrack[];
  notes: string;
  setNotes: (value: string) => void;
  setMessage: (value: string) => void;
  refreshProject: (projectId: number, options?: ProjectDataLoadOptions) => Promise<Job[]>;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const refreshCurrentProject = (options: ProjectDataLoadOptions = {}) => refreshProject(detail.project.id, options);
  return (
    <>
      <div className="hero">
        <div>
          <h2>{detail.project.name}</h2>
          <p>{detail.folder}</p>
        </div>
        <Status value={detail.project.status} />
      </div>
      <RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      <Workflow detail={detail} />
      <div className="grid">
        <Card title="審核">
          <p>Gate：{detail.can_render ? "可正式輸出" : detail.render_gate_reason}</p>
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="審核備註" />
          <div className="row">
            <button className="good" onClick={() => review("approve")}>核准專案</button>
            <button className="danger" onClick={() => review("reject")}>退回修改</button>
            <button onClick={revise}>依備註重建故事</button>
          </div>
        </Card>
        <Card title="輸出">
          <button onClick={() => exportProject("hyperframes")}>產生初剪專案</button>
          <button disabled={!detail.can_render} onClick={() => exportProject("hyperframes-render")}>快速輸出 MP4</button>
          <button className="good" disabled={submitting || !detail.can_render || jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status))} onClick={startFormalRender}>{submitting ? "正在建立正式輸出…" : "正式輸出（Render Job）"}</button>
          <button onClick={() => exportProject("opencut")}>OpenCut 素材包</button>
          <button disabled={!detail.can_render} onClick={() => exportProject("opencut-render")}>OpenCut 調色片段</button>
        </Card>
      </div>
      <WorkflowSkeleton detail={detail} />
      <div className="grid">
        <Card title="素材">
          <div className="row">
            <input type="file" multiple accept="video/*" onChange={uploadFiles} />
            <button onClick={() => analyze(true)}>全部重跑感知</button>
            <button onClick={buildPlan}>產生故事整理</button>
          </div>
          {detail.clips.map((c) => (
            <div className="item" key={c.clip_id}>
              <div className="row">
                <b>{c.clip_id}</b>
                <button onClick={() => analyzeOne(c.video_id)}>重跑感知</button>
              </div>
              {c.filename}
              <span>{c.status} | {c.segment_count} 段 | {Math.round(c.duration_seconds || 0)}s | {c.time_of_day}</span>
              <ClipSummary projectId={detail.project.id} clip={c} setMessage={setMessage} refreshProject={refreshCurrentProject} />
            </div>
          ))}
        </Card>
        <ColorConsistencyPanel detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      </div>
      <AudioMixingPanel detail={detail} bgmTracks={bgmTracks} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      <SegmentTable detail={detail} />
      <Card title="故事整理">
        <pre>{detail.script || "尚未產生故事整理。"}</pre>
      </Card>
    </>
  );

  async function exportProject(kind: "hyperframes" | "hyperframes-render" | "opencut" | "opencut-render") {
    try {
      setMessage("工作已送出，請看 Render Job 狀態與進度。");
      const result = kind.startsWith("hyperframes")
        ? await api.hyperframesJob(detail.project.id, kind === "hyperframes-render")
        : await api.opencutJob(detail.project.id, kind === "opencut-render");
      await refreshCurrentProject({ forceFresh: true });
      if (!result.ok) {
        setMessage(`工作啟動失敗：${result.error || result.message || "工作未成功送出"}`);
        return;
      }
      setMessage(result.message || "工作已開始");
    } catch (error) {
      setMessage(`工作啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function startFormalRender() {
    if (!detail.can_render) {
      setMessage(`正式輸出被擋下：${detail.render_gate_reason}`);
      return;
    }
    const requestedProjectId = detail.project.id;
    try {
      setSubmitting(true);
      setMessage("正在建立正式輸出…");
      const result = await api.createRenderJob(requestedProjectId);
      if (!result.ok) {
        setMessage(`正式輸出失敗：${result.error || "建立 Render Job 未成功"}`);
        return;
      }
      await refreshProject(requestedProjectId, { forceFresh: true });
      setMessage(result.created ? "正式輸出已排入佇列" : "正式輸出工作已在執行中");
    } catch (error) {
      setMessage(`正式輸出啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setSubmitting(false);
    }
  }

  async function uploadFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = event.target.files;
    if (!files?.length) return;
    setMessage("正在匯入素材...");
    const result = await api.uploadProject(detail.project.id, files);
    event.target.value = "";
    setMessage(result.ok ? `已匯入 ${result.files?.length || 0} 支素材，下一步請跑內容感知。` : result.error || "匯入失敗");
    await refreshCurrentProject();
  }

  async function analyze(force: boolean) {
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    const result = await api.analyzeJob(detail.project.id, force);
    setMessage(result.message || "內容感知工作已開始");
    await refreshCurrentProject();
  }

  async function analyzeOne(videoId: number) {
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    const result = await api.analyzeVideo(detail.project.id, videoId);
    setMessage(result.message || "單支素材感知已開始");
    await refreshCurrentProject();
  }

  async function buildPlan() {
    setMessage("正在產生故事整理...");
    await api.buildPlan(detail.project.id);
    setMessage("故事整理已更新，請審核片段。");
    await refreshCurrentProject();
  }

}

function AudioMixingPanel({ detail, bgmTracks, setMessage, refreshProject }: { detail: ProjectDetail; bgmTracks: BgmTrack[]; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const fallback: AudioState = {
    schema_version: 1,
    enabled: true,
    bgm: { bgm_id: null, enabled: false, volume_db: -18, start_seconds: 0, loop: true, fade_in_seconds: 1.5, fade_out_seconds: 2 },
    original_audio: { default_role: "lower", default_volume_db: 0, lower_volume_db: -8, fade_in_seconds: .1, fade_out_seconds: .1 },
    normalization: { enabled: true, target_lufs: -14, true_peak_db: -1 },
    segments: {}
  };
  const [state, setState] = useState<AudioState>(detail.audio || fallback);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewInfo, setPreviewInfo] = useState<{ cacheHit: boolean; duration: number; start: number; segmentId?: string } | null>(null);
  const [busy, setBusy] = useState("");
  useEffect(() => setState(detail.audio || fallback), [detail.project.id, detail.audio]);
  const patchState = (patch: Partial<AudioState>) => setState((current) => ({ ...current, ...patch }));
  const updateSegment = (segmentId: string, patch: AudioSegmentOverride) => setState((current) => ({ ...current, segments: { ...current.segments, [segmentId]: { ...(current.segments[segmentId] || {}), ...patch } } }));
  const resetSegment = (segmentId: string) => setState((current) => ({ ...current, segments: { ...current.segments, [segmentId]: null } }));
  const effectiveSegment = (segment: Segment): AudioSegmentSettings => {
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
  };
  async function save() {
    setBusy("save");
    const result = await api.audioSettings(detail.project.id, { enabled: state.enabled, bgm: state.bgm, original_audio: state.original_audio, normalization: state.normalization, segments: state.segments });
    setBusy("");
    if (!result.ok) { setMessage(`音訊設定儲存失敗：${result.error || "未知錯誤"}`); return; }
    if (result.state) setState(result.state);
    setMessage("音訊設定已儲存，專案已回到待審。");
    await refreshProject();
  }
  async function preview(segmentId = "", force = false) {
    setBusy("preview");
    const result = await api.audioPreview(detail.project.id, {
      segmentId,
      durationSeconds: 12,
      patch: { enabled: state.enabled, bgm: state.bgm, original_audio: state.original_audio, normalization: state.normalization, segments: state.segments },
      force,
    });
    setBusy("");
    if (!result.ok) { setMessage(`音訊預覽失敗：${result.error || "未知錯誤"}`); return; }
    setPreviewUrl(result.url || "");
    setPreviewInfo({ cacheHit: Boolean(result.cache_hit), duration: Number(result.duration_seconds || 0), start: Number(result.timeline_start_seconds || 0), segmentId: segmentId || undefined });
    setMessage(force ? "音訊預覽已強制重新產生。保存後仍需重新核准才能正式輸出。" : result.cache_hit ? "音訊預覽已從快取載入。" : "音訊預覽完成。保存後仍需重新核准才能正式輸出。");
  }
  return <div className="grid">
    <Card title="音訊混音與 BGM">
      <label className="toggle"><input type="checkbox" checked={state.enabled} onChange={(e) => patchState({ enabled: e.target.checked })} /> 啟用專案音訊設定</label>
      <div className="audio-grid">
        <label>BGM<select value={state.bgm.bgm_id ?? ""} onChange={(e) => patchState({ bgm: { ...state.bgm, bgm_id: e.target.value ? Number(e.target.value) : null, enabled: Boolean(e.target.value) } })}><option value="">不使用</option>{bgmTracks.map((track) => <option key={track.id} value={track.id}>{track.title}</option>)}</select></label>
        <label>音量 dB<input type="number" min={-60} max={12} step={1} value={state.bgm.volume_db} onChange={(e) => patchState({ bgm: { ...state.bgm, volume_db: Number(e.target.value) } })} /></label>
        <label>起始秒數<input type="number" min={0} step={0.1} value={state.bgm.start_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, start_seconds: Number(e.target.value) } })} /></label>
        <label>淡入秒數<input type="number" min={0} step={0.1} value={state.bgm.fade_in_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, fade_in_seconds: Number(e.target.value) } })} /></label>
        <label>淡出秒數<input type="number" min={0} step={0.1} value={state.bgm.fade_out_seconds} onChange={(e) => patchState({ bgm: { ...state.bgm, fade_out_seconds: Number(e.target.value) } })} /></label>
        <label className="toggle"><input type="checkbox" checked={state.bgm.loop} onChange={(e) => patchState({ bgm: { ...state.bgm, loop: e.target.checked } })} /> BGM 循環</label>
      </div>
      <div className="audio-grid">
        <label>原音預設角色<select value={state.original_audio.default_role} onChange={(e) => patchState({ original_audio: { ...state.original_audio, default_role: e.target.value as AudioSegmentSettings["role"] } })}><option value="keep">保留原音</option><option value="lower">降低原音</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select></label>
        <label>降低原音 dB<input type="number" min={-60} max={12} step={1} value={state.original_audio.lower_volume_db} onChange={(e) => patchState({ original_audio: { ...state.original_audio, lower_volume_db: Number(e.target.value) } })} /></label>
        <label className="toggle"><input type="checkbox" checked={state.normalization.enabled} onChange={(e) => patchState({ normalization: { ...state.normalization, enabled: e.target.checked } })} /> 音量正規化</label>
        <label>目標 LUFS<input type="number" min={-40} max={0} step={1} value={state.normalization.target_lufs} onChange={(e) => patchState({ normalization: { ...state.normalization, target_lufs: Number(e.target.value) } })} /></label>
        <label>True Peak dB<input type="number" min={-20} max={0} step={0.1} value={state.normalization.true_peak_db} onChange={(e) => patchState({ normalization: { ...state.normalization, true_peak_db: Number(e.target.value) } })} /></label>
      </div>
      <div className="row"><button className="good" disabled={busy === "save"} onClick={save}>{busy === "save" ? "儲存中…" : "儲存音訊設定"}</button><button disabled={Boolean(busy)} onClick={() => preview()}>{busy === "preview" ? "預覽產生中…" : "產生 12 秒預覽"}</button><button disabled={Boolean(busy)} onClick={() => preview("", true)}>強制重新產生</button></div>
      {previewInfo && <p className="muted">預覽範圍 {previewInfo.start.toFixed(1)}s，長度 {previewInfo.duration.toFixed(1)}s，{previewInfo.cacheHit ? "命中快取" : "新產生"}</p>}
      {previewUrl && <video controls width="100%" src={previewUrl} />}
      {state.bgm.track && <p className="muted">目前 BGM：{state.bgm.track.title}｜作者：{state.bgm.track.artist || "未知"}｜{state.bgm.track.duration_seconds ? `${state.bgm.track.duration_seconds}s` : "長度未知"}｜授權：{state.bgm.track.license_name || "未填"}｜{state.bgm.track.attribution_text || "未填署名"}</p>}
      {state.enabled && (!state.bgm.enabled || !state.bgm.bgm_id) && (state.original_audio.default_role === "bgm_only" || Object.values(state.segments).some((item) => item?.role === "bgm_only")) && <div className="notice">有片段設定為只留 BGM，但目前沒有有效 BGM；儲存後的 Manifest／預覽會阻擋，請先選擇可讀取的音樂。</div>}
    </Card>
    <Card title="片段原音角色">
      {detail.segments.map((segment) => { const item = effectiveSegment(segment); const customized = Boolean(state.segments[segment.segment_id]); return <div className="audio-segment" key={segment.segment_id}><b>{segment.title || segment.segment_id}</b><span className="muted">{customized ? "自訂設定" : "使用專案預設"}</span><select value={item.role} onChange={(e) => updateSegment(segment.segment_id, { role: e.target.value as AudioSegmentSettings["role"] })}><option value="keep">保留</option><option value="lower">降低</option><option value="mute">靜音</option><option value="bgm_only">只留 BGM</option></select><input aria-label="片段音量" type="number" min={-60} max={12} step={1} value={item.volume_db} onChange={(e) => updateSegment(segment.segment_id, { volume_db: Number(e.target.value) })} /><input aria-label="淡入秒數" type="number" min={0} step={.1} value={item.fade_in_seconds} onChange={(e) => updateSegment(segment.segment_id, { fade_in_seconds: Number(e.target.value) })} /><input aria-label="淡出秒數" type="number" min={0} step={.1} value={item.fade_out_seconds} onChange={(e) => updateSegment(segment.segment_id, { fade_out_seconds: Number(e.target.value) })} /><label className="toggle"><input type="checkbox" checked={item.locked} onChange={(e) => updateSegment(segment.segment_id, { locked: e.target.checked })} />鎖定</label><button onClick={() => resetSegment(segment.segment_id)}>套用專案預設</button><button disabled={Boolean(busy)} onClick={() => preview(segment.segment_id)}>預覽</button><button disabled={Boolean(busy)} onClick={() => preview(segment.segment_id, true)}>強制重跑</button><span>dB｜淡入｜淡出</span></div>; })}
      {!detail.segments.length && <p>尚未有可調整的片段。</p>}
    </Card>
  </div>;
}

function ClipSummary({ projectId, clip, setMessage, refreshProject }: { projectId: number; clip: { video_id: number; visual_summary: string }; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const [text, setText] = useState(clip.visual_summary || "");
  useEffect(() => setText(clip.visual_summary || ""), [clip.visual_summary]);
  async function save() {
    setMessage("正在儲存內容感知描述...");
    const result = await api.saveClipSummary(projectId, clip.video_id, text);
    setMessage(result.ok ? "內容感知描述已儲存，專案已回到待審。" : "儲存失敗：找不到素材");
    await refreshProject();
  }
  return <div className="stack"><textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="內容感知描述" /><button onClick={save}>儲存描述</button></div>;
}

function ColorConsistencyPanel({ detail, setMessage, refreshProject }: { detail: ProjectDetail; setMessage: (value: string) => void; refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]> }) {
  const [state, setState] = useState<ColorState>(() => detail.color || emptyColorState());
  const [previews, setPreviews] = useState<Array<{ video_id: number; segment_id: string; before_url: string; after_url: string; cache_hit: boolean }>>([]);
  const [busy, setBusy] = useState("");
  useEffect(() => setState(detail.color || emptyColorState()), [detail.project.id, detail.color]);

  const selectedReferenceId = typeof state.reference === "object" && "id" in state.reference ? state.reference.id : "";
  const suggested = state.suggested;
  const applied = state.applied;

  async function analyze(force = false) {
    setBusy("analyze");
    setMessage(force ? "正在重跑色彩分析..." : "正在分析核心畫面色彩...");
    try {
      const result = await api.colorAnalyze(detail.project.id, force);
      if (!result.ok || !result.state) {
        setMessage(`色彩分析失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩分析完成，請確認建議值後再儲存實際套用值。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩分析失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function save() {
    setBusy("save");
    try {
      const result = await api.colorSettings(detail.project.id, toColorStatePatch(state));
      if (!result.ok || !result.state) {
        setMessage(`色彩設定儲存失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩設定已儲存，專案已回到待審。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩設定儲存失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function changeReference(referenceId: string) {
    if (!referenceId) return;
    setBusy("reference");
    try {
      const result = await api.colorReference(detail.project.id, referenceId);
      if (!result.ok || !result.state) {
        setMessage(`色彩基準更新失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setState(result.state);
      setMessage("色彩基準已更新，請重新確認建議值。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`色彩基準更新失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  async function preview(force = false) {
    setBusy("preview");
    setMessage(force ? "正在強制重新產生 Before / After 調色預覽..." : "正在產生 Before / After 調色預覽...");
    try {
      const result = await api.colorPreviewDirect(detail.project.id, force);
      if (!result.ok) {
        setPreviews([]);
        setMessage(`調色預覽失敗：${result.error || "未知錯誤"}`);
        return;
      }
      setPreviews(result.previews || []);
      setMessage("Before / After 調色預覽已完成。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setPreviews([]);
      setMessage(`調色預覽失敗：${error instanceof Error ? error.message : "網路或服務錯誤"}`);
    } finally {
      setBusy("");
    }
  }

  function updateApplied(field: keyof ColorAdjustment, value: string | number) {
    setState({ ...state, applied: { ...state.applied, [field]: field === "mode" || field === "lut_path" || field === "lut_kind" ? String(value) : Number(value) } });
  }

  function updateSegment(segmentId: string, patch: { enabled?: boolean; locked?: boolean; excluded?: boolean }) {
    const current = state.segments[segmentId] || { enabled: true, locked: false, excluded: false };
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, ...patch } } });
  }

  function segmentState(segmentId: string): ColorSegmentState {
    return state.segments[segmentId] || { enabled: true, locked: false, excluded: false, suggested: state.suggested, applied: state.applied, confidence: 0, warnings: [] };
  }

  function applySegmentSuggestion(segmentId: string) {
    const current = segmentState(segmentId);
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: { ...(current.suggested || state.suggested) } } } });
  }

  function resetSegment(segmentId: string) {
    const current = segmentState(segmentId);
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: { ...state.applied } } } });
  }

  function updateSegmentApplied(segmentId: string, field: keyof ColorAdjustment, value: string | number) {
    const current = segmentState(segmentId);
    const appliedValues = { ...(current.applied || state.applied), [field]: Number(value) };
    setState({ ...state, segments: { ...state.segments, [segmentId]: { ...current, applied: appliedValues } } });
  }

  return (
    <Card title="色彩一致性與調色預覽">
      <div className="color-toolbar row">
        <button onClick={() => analyze(false)} disabled={Boolean(busy)}>分析核心畫面</button>
        <button onClick={() => analyze(true)} disabled={Boolean(busy)}>重跑色彩分析</button>
        <button onClick={() => void preview(false)} disabled={Boolean(busy)}>產生 Before / After 預覽</button>
        <button onClick={() => void preview(true)} disabled={Boolean(busy)}>強制重新產生</button>
        <button className="good" onClick={save} disabled={busy !== ""}>儲存實際套用值</button>
        {busy && <span className="muted">{busy === "preview" ? "正在輸出預覽..." : "處理中..."}</span>}
      </div>
      <label className="toggle"><input type="checkbox" checked={state.enabled} onChange={(e) => setState({ ...state, enabled: e.target.checked })} /> 啟用專案色彩一致性</label>
      <div className="color-grid">
        <div>
          <label>Reference Clip / Frame</label>
          <select value={selectedReferenceId} onChange={(e) => void changeReference(e.target.value)} disabled={!state.references.length || Boolean(busy)}>
            <option value="">尚未選擇基準畫面</option>
            {state.references.map((reference) => <option key={reference.id} value={reference.id}>{reference.type === "segment" ? "片段" : "畫格"}｜{reference.label || "未命名"}｜{reference.score.toFixed(2)}</option>)}
          </select>
          <p className="muted">{state.analysis.basis_text || "先執行色彩分析，系統會從內容感知結果挑選核心畫面。"}</p>
          {typeof state.reference === "object" && "frame_url" in state.reference && state.reference.frame_url ? <img className="reference-thumbnail" src={state.reference.frame_url} alt="色彩基準畫面" /> : null}
          {state.analysis.luma && <p className="muted">平均亮度 {Number(state.analysis.luma.average || 0).toFixed(1)}｜高光比例 {(Number(state.analysis.luma.highlight_ratio || 0) * 100).toFixed(1)}%｜信心 {state.analysis.confidence || "未分析"}</p>}
          {state.analysis.warnings?.length ? <p className="job-error">{state.analysis.warnings.join("｜")}</p> : null}
        </div>
        <div>
          <label>技術 LUT</label>
          <select value={applied.mode} onChange={(e) => updateApplied("mode", e.target.value)}>
            <option value="dji_dlog_m">DJI D-Log M</option>
            <option value="dji_dlog">DJI D-Log</option>
            <option value="dji_lut">自訂 DJI LUT</option>
            <option value="safe_restore">保守修正</option>
            <option value="manual">手動調整</option>
            <option value="none">不套用</option>
          </select>
          <input value={applied.lut_path} onChange={(e) => updateApplied("lut_path", e.target.value)} placeholder=".cube LUT 路徑" />
        </div>
      </div>
      <div className="adjustment-grid">
        {(["exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma"] as const).map((field) => (
          <label key={field}>{adjustmentLabel(field)}<input type="number" step="0.01" value={applied[field]} onChange={(e) => updateApplied(field, e.target.value)} /></label>
        ))}
      </div>
      <div className="suggested-box">
        <b>系統建議值（唯讀）</b>
        <span>曝光 {suggested.exposure}｜色溫 {suggested.temperature}｜色調 {suggested.tint}｜對比 {suggested.contrast}｜飽和 {suggested.saturation}｜Gamma {suggested.gamma}</span>
      </div>
      {detail.segments.length > 0 && <div className="color-segments"><b>片段覆寫</b>{detail.segments.map((segment) => { const item = segmentState(segment.segment_id); const appliedDisabled = item.excluded || !item.enabled; return <div className="color-segment-row" key={segment.segment_id}><span><b>{segment.clip_id}</b>｜{segment.title}<small>信心 {Number(item.confidence || 0).toFixed(2)}{item.warnings?.length ? `｜${item.warnings.join("、")}` : ""}{appliedDisabled ? "｜目前不套用片段色彩" : ""}</small></span><label><input type="checkbox" checked={item.enabled} onChange={(e) => updateSegment(segment.segment_id, { enabled: e.target.checked })} /> 啟用</label><label><input type="checkbox" checked={item.locked} onChange={(e) => updateSegment(segment.segment_id, { locked: e.target.checked })} /> Lock</label><label><input type="checkbox" checked={item.excluded} onChange={(e) => updateSegment(segment.segment_id, { excluded: e.target.checked })} /> Exclude</label><button disabled={appliedDisabled} onClick={() => applySegmentSuggestion(segment.segment_id)}>套用建議</button><button disabled={appliedDisabled} onClick={() => resetSegment(segment.segment_id)}>重設為專案</button><details><summary>片段色彩值</summary><div className="adjustment-grid">{(["exposure", "temperature", "tint", "contrast", "highlights", "shadows", "saturation", "gamma"] as const).map((field) => <label key={field}>{adjustmentLabel(field)}<input disabled={appliedDisabled} type="number" step="0.01" value={item.applied?.[field] ?? state.applied[field]} onChange={(e) => updateSegmentApplied(segment.segment_id, field, e.target.value)} /></label>)}</div></details></div>; })}</div>}
      {previews.length > 0 && <div className="preview-grid"><b>最近一次預覽</b>{previews.map((previewItem) => <div className="preview-item" key={`${previewItem.video_id}-${previewItem.segment_id}`}><span>素材 #{previewItem.video_id}｜{previewItem.segment_id} {previewItem.cache_hit ? "（快取）" : "（重新產生）"}</span><div className="preview-videos"><div><small>Before</small><video controls preload="metadata" src={previewItem.before_url} /></div><div><small>After</small><video controls preload="metadata" src={previewItem.after_url} /></div></div></div>)}</div>}
    </Card>
  );
}

function emptyColorState(): ColorState {
  return {
    schema_version: 2,
    enabled: true,
    reference: {},
    references: [],
    analysis: {},
    suggested: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
    applied: { mode: "none", lut_path: "", lut_kind: "", exposure: 0, temperature: 0, tint: 0, contrast: 1, saturation: 1, gamma: 1, highlights: 0, shadows: 0 },
    segments: {},
  };
}

function toColorStatePatch(state: ColorState): ColorStatePatch {
  return {
    schema_version: state.schema_version,
    enabled: state.enabled,
    applied: { ...state.applied },
    segments: Object.fromEntries(Object.entries(state.segments).map(([segmentId, segment]) => [segmentId, {
      enabled: segment.enabled,
      locked: segment.locked,
      excluded: segment.excluded,
      ...(segment.applied ? { applied: { ...segment.applied } } : {})
    }]))
  };
}

function adjustmentLabel(field: keyof ColorAdjustment) {
  return ({ exposure: "曝光", temperature: "白平衡色溫", tint: "白平衡色調", contrast: "對比", highlights: "高光", shadows: "陰影", saturation: "飽和度", gamma: "Gamma" } as Record<string, string>)[field] || field;
}

function Workflow({ detail }: { detail: ProjectDetail }) {
  const steps = [
    ["新增專案", true],
    ["匯入素材", detail.clips.length > 0],
    ["內容感知", detail.clips.some((clip) => clip.segment_count > 0 || clip.status === "perceived")],
    ["故事整理", detail.segments.length > 0 || Boolean(detail.script)],
    ["核准", detail.can_render],
    ["輸出", false]
  ] as const;
  return <div className="workflow">{steps.map(([label, done]) => <span key={label} className={done ? "step done" : "step"}>{label}</span>)}</div>;
}

function WorkflowSkeleton({ detail }: { detail: ProjectDetail }) {
  return (
    <Card title="OpenMontage-style 工作流骨架">
      <div className="workflow-grid">
        {detail.workflow.stages.map((stage) => (
          <div className="workflow-card" key={stage.id}>
            <b>{stage.label}</b>
            <span className={stage.status === "done" ? "pill ok" : "pill"}>{stage.status}</span>
            <small>{stage.artifacts.join("\n")}</small>
          </div>
        ))}
      </div>
    </Card>
  );
}

function SegmentTable({ detail }: { detail: ProjectDetail }) {
  const [rows, setRows] = useState<Segment[]>(detail.segments || []);
  const [saved, setSaved] = useState("");
  useEffect(() => setRows(detail.segments || []), [detail.project.id, detail.segments]);
  if (!detail.segments?.length) return <Card title="片段審核"><p>尚未有推薦片段。</p></Card>;
  async function save() {
    await api.saveSegments(detail.project.id, rows.map((row, index) => ({ ...row, manual_order: index + 1 })));
    setSaved("已儲存，專案已回到待審");
  }
  return (
    <Card title="片段審核">
      <div className="row"><button onClick={save}>儲存片段審核</button>{saved && <span className="muted">{saved}</span>}</div>
      <div className="table">
        <div className="thead">
          <span>使用</span><span>順序</span><span>片段</span><span>時間</span><span>場景</span><span>用途</span><span>分數</span><span>備註</span>
        </div>
        {rows.map((s, index) => (
          <div className="trow" key={s.segment_id}>
            <span><input type="checkbox" checked={s.include} onChange={(e) => update(index, { include: e.target.checked })} /> {s.include ? "保留" : "不用"}</span>
            <span className="order-edit">
              <button disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
              <button disabled={index === rows.length - 1} onClick={() => move(index, 1)}>↓</button>
              {index + 1}
            </span>
            <span><b>{s.clip_id}</b> {segmentTitle(s.title)}</span>
            <span className="time-edit">
              <TimeBox label="開始" seconds={s.start_seconds} onChange={(value) => update(index, { start_seconds: value })} />
              <b>~</b>
              <TimeBox label="結束" seconds={s.end_seconds} onChange={(value) => update(index, { end_seconds: value })} />
            </span>
            <span>{s.scene_role}</span>
            <span>{useLabel(s.suggested_use)}</span>
            <span>{Number(s.score || 0).toFixed(2)}</span>
            <span><input value={s.user_notes || ""} onChange={(e) => update(index, { user_notes: e.target.value })} /></span>
          </div>
        ))}
      </div>
    </Card>
  );

  function update(index: number, patch: Partial<Segment>) {
    setRows(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
    setSaved("");
  }

  function move(index: number, delta: number) {
    const next = [...rows];
    const target = index + delta;
    [next[index], next[target]] = [next[target], next[index]];
    setRows(next);
    setSaved("");
  }
}

function TimeBox({ label, seconds, onChange }: { label: string; seconds: number; onChange: (value: number) => void }) {
  const value = Math.max(0, Math.round(seconds || 0));
  const minutes = Math.floor(value / 60);
  const secs = value % 60;
  return (
    <input
      aria-label={label}
      title={label}
      value={`${minutes}:${String(secs).padStart(2, "0")}`}
      onChange={(e) => onChange(parseTime(e.target.value))}
    />
  );
}

function time(seconds: number) {
  const value = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(value / 3600);
  const m = Math.floor((value % 3600) / 60);
  const s = String(value % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function parseTime(value: string) {
  const zh = value.trim().match(/^(\d+)\s*分\s*(\d+)\s*秒?$/);
  if (zh) return Number(zh[1]) * 60 + Number(zh[2]);
  const parts = value.trim().split(":").map(Number);
  if (parts.some(Number.isNaN)) return 0;
  if (parts.length === 3) return Math.max(0, parts[0] * 3600 + parts[1] * 60 + parts[2]);
  if (parts.length === 2) return Math.max(0, parts[0] * 60 + parts[1]);
  return Math.max(0, Number(value) || 0);
}

function segmentTitle(value: string) {
  return value
    .replace("Shorts candidate", "短影音候選片段")
    .replace("B-roll candidate", "補畫面候選片段")
    .replace("Product closeup candidate", "產品特寫候選片段");
}

function useLabel(value: string) {
  return ({ Shorts: "短影音", "B-roll": "補畫面", "Product closeup": "產品特寫" } as Record<string, string>)[value] || value;
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card"><h3>{title}</h3>{children}</div>;
}

function Status({ value }: { value: string }) {
  return <span className={value === "approved" ? "pill ok" : "pill"}>{value}</span>;
}

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<StrictMode><App /></StrictMode>);
