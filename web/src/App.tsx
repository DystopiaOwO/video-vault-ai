import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { api, BgmTrack, ColorAdjustment, ColorSegmentState, ColorState, ColorStatePatch, Job, Project, ProjectDetail } from "./api";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { ProjectDataLoadOptions, ProjectDataLoader } from "./projectDataLoader";
import { AudioMixingWorkspace } from "./workspaces/audio/AudioMixingWorkspace";
import { StoryboardWorkspaceController } from "./workspaces/storyboard/StoryboardWorkspaceController";

export function App() {
  if (window.location.pathname === "/bgm") return <BgmPage />;
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectQuery, setProjectQuery] = useState("");
  const [currentId, setCurrentId] = useState<number>(0);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [bgmTracks, setBgmTracks] = useState<BgmTrack[]>([]);
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const currentIdRef = useRef(0);
  const mountedRef = useRef(true);
  const loaderRef = useRef<ProjectDataLoader | null>(null);
  const normalizedProjectQuery = projectQuery.trim().toLocaleLowerCase();
  const filteredProjects = useMemo(() => projects.filter((project) => {
    if (!normalizedProjectQuery) return true;
    return [project.name, project.status, String(project.id)]
      .some((value) => String(value || "").toLocaleLowerCase().includes(normalizedProjectQuery));
  }), [normalizedProjectQuery, projects]);

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
    void loadProjects();
    api.bgm().then(setBgmTracks).catch((error) => setMessage(`BGM 載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }, []);

  async function loadProjects() {
    setProjectsLoading(true);
    try {
      const rows = await api.projects();
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
      return rows;
    } catch (error) {
      setMessage(`專案載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      return [];
    } finally {
      setProjectsLoading(false);
    }
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
    try {
      action === "approve" ? await api.approve(detail.project.id, notes) : await api.reject(detail.project.id, notes);
      setNotes("");
      setMessage(action === "approve" ? "已核准專案" : "已退回修改");
      setDetail(await api.project(detail.project.id));
    } catch (error) {
      setMessage(`審核操作失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function revise() {
    if (!detail) return;
    setMessage("正在依備註重建故事...");
    try {
      await api.revise(detail.project.id, notes);
      setMessage("故事整理已依備註重建");
      setDetail(await api.project(detail.project.id));
    } catch (error) {
      setMessage(`故事重建失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name) {
      setMessage("請先輸入專案名稱。");
      return;
    }
    if (projects.some((project) => project.name.trim().toLocaleLowerCase() === name.toLocaleLowerCase())) {
      setMessage("已有同名專案，請使用不同名稱。");
      return;
    }
    setCreatingProject(true);
    setMessage("正在建立專案...");
    try {
      const result = await api.createProject(name);
      setNewProjectName("");
      await loadProjects();
      selectProject(result.id);
      setMessage("專案已建立，下一步請匯入素材。");
    } catch (error) {
      setMessage(`專案建立失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setCreatingProject(false);
    }
  }

  function selectProject(projectId: number) {
    if (projectId === currentId) return;
    loaderRef.current?.invalidate();
    setCurrentId(projectId);
    setDetail(null);
    setJobs([]);
    setNotes("");
    setMessage("");
  }

  async function refreshProject(projectId: number, options: ProjectDataLoadOptions = {}): Promise<Job[]> {
    return loadProjectData(projectId, options);
  }

  const projectContent = projectsLoading
    ? <WorkspaceLoading title="正在載入專案" detail="取得專案清單與工作狀態…" />
    : projects.length === 0
      ? <WorkspaceEmpty title="尚未建立專案" detail="從左側輸入名稱建立第一個專案，再匯入多支影片素材。" />
      : !detail || detail.project.id !== currentId
        ? <WorkspaceLoading title="正在開啟專案" detail="載入素材、分鏡、調色、音訊與輸出狀態…" />
        : <ProjectView key={detail.project.id} detail={detail} jobs={jobs} bgmTracks={bgmTracks} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} review={review} revise={revise} />;

  return (
    <main>
      <aside>
        <h1>Video Vault AI</h1>
        <nav className="sidebar-links" aria-label="主要導覽">
          <a className="nav" href="/bgm">BGM 資料庫</a>
          <a className="nav" href="/classic-bgm">舊版 BGM 上傳</a>
          <a className="nav" href="/classic">舊版工作台</a>
        </nav>
        <div className="new-project">
          <label htmlFor="new-project-name">建立專案</label>
          <input id="new-project-name" value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createProject(); }} placeholder="例如：福岡旅行 2026" maxLength={80} />
          <button disabled={creatingProject || !newProjectName.trim()} onClick={() => void createProject()}>{creatingProject ? "建立中…" : "新增專案"}</button>
        </div>
        <div className="sidebar-section-heading">
          <h2>專案</h2>
          <span>{filteredProjects.length} / {projects.length}</span>
        </div>
        <label className="project-search">
          <span>搜尋專案</span>
          <input type="search" aria-label="搜尋專案" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} placeholder="名稱、狀態或編號" />
        </label>
        <div className="project-list" aria-label="專案清單">
          {projectsLoading && <div className="sidebar-empty">載入專案中…</div>}
          {!projectsLoading && filteredProjects.map((project) => (
            <button key={project.id} className={project.id === currentId ? "project active" : "project"} aria-current={project.id === currentId ? "page" : undefined} onClick={() => selectProject(project.id)}>
              <b>{project.name}</b>
              <span>#{project.id} · {projectStatusLabel(project.status)} · {project.video_count ?? 0} 支素材</span>
            </button>
          ))}
          {!projectsLoading && projects.length > 0 && filteredProjects.length === 0 && <div className="sidebar-empty">
            <b>找不到符合的專案</b>
            <button type="button" onClick={() => setProjectQuery("")}>清除搜尋</button>
          </div>}
          {!projectsLoading && projects.length === 0 && <div className="sidebar-empty">建立第一個專案後會顯示在這裡。</div>}
        </div>
      </aside>
      <section>
        {message && <div className="notice" role="status"><span>{message}</span><button type="button" aria-label="關閉通知" onClick={() => setMessage("")}>×</button></div>}
        {projectContent}
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
  const includedSegments = detail.storyboard?.segments
    ? Object.values(detail.storyboard.segments).filter((segment) => segment.included).length
    : detail.segments.filter((segment) => segment.include !== false).length;
  return (
    <>
      <div className="hero">
        <div>
          <h2>{detail.project.name}</h2>
          <p>{detail.folder || `專案 #${detail.project.id}`}</p>
        </div>
        <Status value={detail.project.status} />
      </div>
      <div className="project-metrics" aria-label="專案摘要">
        <div><span>素材</span><b>{detail.clips.length}</b></div>
        <div><span>感知片段</span><b>{detail.segments.length}</b></div>
        <div><span>納入成片</span><b>{includedSegments}</b></div>
        <div><span>正式輸出</span><b>{detail.can_render ? "已解鎖" : "待核准"}</b></div>
      </div>
      <WorkspaceNavigation />

      <div className="workspace-section" id="workspace-overview" tabIndex={-1}>
        <RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshCurrentProject} />
        <Workflow detail={detail} />
      </div>

      <div className="workspace-section" id="workspace-storyboard" tabIndex={-1}>
        <StoryboardWorkspaceController detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      </div>

      <div className="workspace-section" id="workspace-review" tabIndex={-1}>
        <div className="grid">
          <Card title="審核">
            <p>Gate：{detail.can_render ? "可正式輸出" : detail.render_gate_reason}</p>
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="記錄核准理由、退回項目或重建故事需求" />
            <div className="row">
              <button className="good" onClick={() => review("approve")}>核准專案</button>
              <button className="danger" onClick={() => review("reject")}>退回修改</button>
              <button disabled={!notes.trim()} onClick={revise}>依備註重建故事</button>
            </div>
          </Card>
          <Card title="輸出">
            <div className="output-actions">
              <button onClick={() => exportProject("hyperframes")}>產生初剪專案</button>
              <button disabled={!detail.can_render} onClick={() => exportProject("hyperframes-render")}>快速輸出 MP4</button>
              <button className="good" disabled={submitting || !detail.can_render || jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status))} onClick={startFormalRender}>{submitting ? "正在建立正式輸出…" : "正式輸出（Render Job）"}</button>
              <button onClick={() => exportProject("opencut")}>OpenCut 素材包</button>
              <button disabled={!detail.can_render} onClick={() => exportProject("opencut-render")}>OpenCut 調色片段</button>
            </div>
          </Card>
        </div>
      </div>

      <div className="workspace-section" id="workspace-media" tabIndex={-1}>
        <WorkflowSkeleton detail={detail} />
        <div className="grid">
          <Card title="素材">
            <div className="row">
              <input type="file" multiple accept="video/*" onChange={uploadFiles} />
              <button onClick={() => analyze(true)}>全部重跑感知</button>
              <button disabled={!detail.clips.length} onClick={buildPlan}>產生故事整理</button>
            </div>
            {detail.clips.map((clip) => (
              <div className="item" key={clip.clip_id}>
                <div className="row">
                  <b>{clip.clip_id}</b>
                  <button onClick={() => analyzeOne(clip.video_id)}>重跑感知</button>
                </div>
                {clip.filename}
                <span>{projectStatusLabel(clip.status)} · {clip.segment_count} 段 · {Math.round(clip.duration_seconds || 0)} 秒 · {clip.time_of_day || "未分類時段"}</span>
                <ClipSummary projectId={detail.project.id} clip={clip} setMessage={setMessage} refreshProject={refreshCurrentProject} />
              </div>
            ))}
            {!detail.clips.length && <div className="inline-empty">尚無素材。先選擇多支影片匯入，再進行內容感知。</div>}
          </Card>
          <ColorConsistencyPanel detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} />
        </div>
      </div>

      <div className="workspace-section" id="workspace-audio" tabIndex={-1}>
        <AudioMixingWorkspace detail={detail} bgmTracks={bgmTracks} setMessage={setMessage} refreshProject={refreshCurrentProject} />
      </div>

      <div className="workspace-section" id="workspace-script" tabIndex={-1}>
        <Card title="故事整理">
          <pre>{detail.script || "尚未產生故事整理。"}</pre>
        </Card>
      </div>
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
    try {
      const result = await api.uploadProject(detail.project.id, files);
      event.target.value = "";
      setMessage(result.ok ? `已匯入 ${result.files?.length || 0} 支素材，下一步請跑內容感知。` : result.error || "匯入失敗");
      await refreshCurrentProject({ forceFresh: true });
    } catch (error) {
      event.target.value = "";
      setMessage(`素材匯入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function analyze(force: boolean) {
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    try {
      const result = await api.analyzeJob(detail.project.id, force);
      setMessage(result.message || "內容感知工作已開始");
      await refreshCurrentProject();
    } catch (error) {
      setMessage(`內容感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function analyzeOne(videoId: number) {
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    try {
      const result = await api.analyzeVideo(detail.project.id, videoId);
      setMessage(result.message || "單支素材感知已開始");
      await refreshCurrentProject();
    } catch (error) {
      setMessage(`單支素材感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }

  async function buildPlan() {
    setMessage("正在產生故事整理...");
    try {
      await api.buildPlan(detail.project.id);
      setMessage("故事整理已更新，請審核片段。");
      await refreshCurrentProject({ forceFresh: true });
    } catch (error) {
      setMessage(`故事整理失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    }
  }
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

function WorkspaceNavigation() {
  const items = [
    ["workspace-overview", "總覽"],
    ["workspace-storyboard", "分鏡"],
    ["workspace-review", "審核與輸出"],
    ["workspace-media", "素材與調色"],
    ["workspace-audio", "音訊"],
    ["workspace-script", "故事整理"],
  ] as const;
  return <nav className="workspace-nav" aria-label="專案工作區導覽">
    {items.map(([id, label]) => <a key={id} href={`#${id}`}>{label}</a>)}
  </nav>;
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

function WorkspaceLoading({ title, detail }: { title: string; detail: string }) {
  return <div className="workspace-state" role="status">
    <span className="workspace-spinner" aria-hidden="true" />
    <div><h2>{title}</h2><p>{detail}</p></div>
  </div>;
}

function WorkspaceEmpty({ title, detail }: { title: string; detail: string }) {
  return <div className="workspace-state empty">
    <span className="workspace-empty-icon" aria-hidden="true">＋</span>
    <div><h2>{title}</h2><p>{detail}</p></div>
  </div>;
}

function projectStatusLabel(value: string): string {
  const labels: Record<string, string> = {
    approved: "已核准",
    needs_review: "待審核",
    rejected: "已退回",
    processing: "處理中",
    perceived: "已感知",
    pending: "等待中",
    ready: "已就緒",
    failed: "失敗",
  };
  return labels[value] || value || "未知狀態";
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return <div className="card"><h3>{title}</h3>{children}</div>;
}

function Status({ value }: { value: string }) {
  return <span className={value === "approved" ? "pill ok" : "pill"}>{projectStatusLabel(value)}</span>;
}
