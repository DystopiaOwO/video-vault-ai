import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, ReactNode } from "react";
import { api, type BgmTrack, type Job, type Project, type ProjectDetail } from "./api";
import { ClipSummaryEditor } from "./components/project/ClipSummaryEditor";
import { MultiFrameEvidencePanel } from "./components/project/MultiFrameEvidencePanel";
import { PerceptionSamplingControls } from "./components/project/PerceptionSamplingControls";
import { ProjectLocation } from "./components/project/ProjectLocation";
import { ProjectWorkflow, projectWorkflowSteps } from "./components/project/ProjectWorkflow";
import { RenderJobPanel } from "./components/render/RenderJobPanel";
import { type ProjectDataLoadOptions, ProjectDataLoader } from "./projectDataLoader";
import { ProjectNavigationIdentity, type ProjectOperationToken } from "./projectNavigationIdentity";
import { isCommittedEnter } from "./keyboard";
import {
  ProjectMutationCoordinator,
  type ProjectMutationControls,
  type ProjectMutation,
  type ProjectMutationToken,
  refreshFailureMessage,
  mutationLabel,
} from "./projectMutation";
import { AudioMixingWorkspace } from "./workspaces/audio/AudioMixingWorkspace";
import { ColorConsistencyWorkspace } from "./workspaces/color/ColorConsistencyWorkspace";
import { StoryboardWorkspaceController } from "./workspaces/storyboard/StoryboardWorkspaceController";
import { StorageWorkspace } from "./workspaces/storage/StorageWorkspace";
import { StoryUnderstandingWorkspace } from "./workspaces/story/StoryUnderstandingWorkspace";
import "./project-detail-polish.css";

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
  const notesRef = useRef("");
  const [message, setMessage] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const currentIdRef = useRef(0);
  const loadedRevisionRef = useRef<number | undefined>(undefined);
  const mountedRef = useRef(true);
  const loaderRef = useRef<ProjectDataLoader | null>(null);
  const navigationIdentityRef = useRef(new ProjectNavigationIdentity());
  const mutationCoordinatorRef = useRef(new ProjectMutationCoordinator());
  const [mutationBusy, setMutationBusy] = useState<ProjectMutationToken | null>(null);
  const creatingProjectRef = useRef(false);
  const normalizedProjectQuery = projectQuery.trim().toLocaleLowerCase();
  const filteredProjects = useMemo(() => projects.filter((project) => {
    if (!normalizedProjectQuery) return true;
    return [project.name, project.status, String(project.id)]
      .some((value) => String(value || "").toLocaleLowerCase().includes(normalizedProjectQuery));
  }), [normalizedProjectQuery, projects]);

  if (!loaderRef.current) {
    loaderRef.current = new ProjectDataLoader(
      {
        // Keep the existing App-level API call shape stable for legacy mocks;
        // ProjectDataLoader still owns generation, timeout, and cancellation.
        project: (projectId, signal) => api.project(projectId, signal),
        jobs: (projectId, signal) => api.jobs(projectId, signal),
      },
      (projectId) => projectId === currentIdRef.current,
      () => mountedRef.current,
      (project, nextJobs, projectRevision) => {
        setDetail(project);
        setJobs(nextJobs);
        loadedRevisionRef.current = projectRevision;
      },
      (error) => setMessage(`狀態更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`),
    );
  }

  useEffect(() => {
    void loadProjects();
    api.bgm().then(setBgmTracks).catch((error) => setMessage(`BGM 載入失敗：${error instanceof Error ? error.message : "未知錯誤"}`));
  }, []);

  async function loadProjects(options: { throwOnError?: boolean } = {}) {
    setProjectsLoading(true);
    try {
      const rows = await api.projects();
      setProjects(rows);
      setCurrentId((id) => id || rows[0]?.id || 0);
      return rows;
    } catch (error) {
      if (options.throwOnError) throw error;
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
    notesRef.current = notes;
  }, [notes]);

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

  async function refreshProjectAfterMutation(projectId: number, operation?: ProjectOperationToken): Promise<Job[]> {
    const result = await loaderRef.current?.loadResult(projectId, { forceFresh: true, throwOnError: true, timeoutMs: 15000 });
    if (!result || !result.ok) {
      throw result?.error || new Error("專案更新後重新載入失敗");
    }
    if (mountedRef.current && currentIdRef.current === projectId && (!operation || isCurrentProjectOperation(operation))) return result.jobs;
    return result.jobs;
  }

  useEffect(() => {
    if (!currentId) return;
    loadedRevisionRef.current = undefined;
    let cancelled = false;
    let timer: number | undefined;
    let wasActive = false;
    const poll = async () => {
      if (cancelled) return;
      const requestedProjectId = currentId;
      try {
        const rawSnapshot = await api.jobs(requestedProjectId, undefined, loadedRevisionRef.current);
        if (cancelled || currentIdRef.current !== requestedProjectId) return;
        const snapshot: import("./api").JobsSnapshot = Array.isArray(rawSnapshot)
          ? { jobs: rawSnapshot as Job[] }
          : rawSnapshot;
        const nextJobs = snapshot.jobs;
        const active = nextJobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status));
        setJobs(nextJobs);
        const revisionChanged = typeof snapshot.project_revision === "number"
          && typeof loadedRevisionRef.current === "number"
          && snapshot.project_revision > loadedRevisionRef.current;
        if ((wasActive && !active) || revisionChanged) {
          const result = await loaderRef.current?.loadResult(requestedProjectId, { forceFresh: true, timeoutMs: 15000 });
          if (!cancelled && result && !result.ok) {
            setMessage(`工作完成後更新失敗：${result.error.message}`);
          }
        }
        wasActive = active;
        if (!cancelled) timer = window.setTimeout(() => void poll(), document.hidden ? 30000 : active ? 1500 : 10000);
      } catch (error) {
        if (error instanceof Error && error.name === "AbortError") return;
        if (!cancelled && currentIdRef.current === requestedProjectId) {
          setMessage(`工作狀態更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
          timer = window.setTimeout(() => void poll(), document.hidden ? 30000 : 10000);
        }
      }
    };
    void (async () => {
      const result = await loaderRef.current?.loadResult(currentId, { forceFresh: true, timeoutMs: 15000 });
      if (!cancelled && result && !result.ok) setMessage(`專案載入失敗：${result.error.message}`);
      if (!cancelled) {
        if (result?.ok) {
          wasActive = result.jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status));
        }
        timer = window.setTimeout(() => void poll(), document.hidden ? 30000 : wasActive ? 1500 : 10000);
      }
    })();
    return () => {
      cancelled = true;
      loaderRef.current?.invalidate();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [currentId]);

  async function review(action: "approve" | "reject") {
    if (!detail) return;
    const requestedProjectId = detail.project.id;
    const submittedNotes = notes;
    const mutation = beginProjectMutation(requestedProjectId, action);
    if (!mutation) return;
    const operation = beginProjectOperation(requestedProjectId);
    setMessage("送出中...");
    try {
      const result = action === "approve"
        ? await (detail.project_revision === undefined ? api.approve(requestedProjectId, submittedNotes) : api.approve(requestedProjectId, submittedNotes, detail.project_revision))
        : await (detail.project_revision === undefined ? api.reject(requestedProjectId, submittedNotes) : api.reject(requestedProjectId, submittedNotes, detail.project_revision));
      if (result.ok === false) {
        if (isCurrentProjectOperation(operation)) {
          const error = result.error || "操作未成功";
          const guidance = action === "approve" && result.code === "storyboard_required"
            ? "請先到「分鏡審核」執行「建立分鏡」，完成後再核准。"
            : "";
          setMessage(`${action === "approve" ? "核准" : "退回"}失敗：${error}${guidance && !error.includes(guidance) ? ` ${guidance}` : ""}`);
        }
        return;
      }
      if (isCurrentProjectOperation(operation) && notesRef.current === submittedNotes) setNotes("");
      const successMessage = action === "approve" ? "專案已核准" : "專案已退回修改";
      try {
        await refreshProjectAfterMutation(requestedProjectId, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) setMessage(`審核操作失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      finishProjectMutation(mutation);
    }
  }

  async function revise() {
    if (!detail) return;
    const requestedProjectId = detail.project.id;
    const submittedNotes = notes;
    const mutation = beginProjectMutation(requestedProjectId, "revise");
    if (!mutation) return;
    const operation = beginProjectOperation(requestedProjectId);
    setMessage("正在依備註重建故事...");
    try {
      const result = await (detail.project_revision === undefined ? api.revise(requestedProjectId, submittedNotes) : api.revise(requestedProjectId, submittedNotes, detail.project_revision));
      if (result.ok === false) {
        if (isCurrentProjectOperation(operation)) setMessage("故事重建失敗：操作未成功");
        return;
      }
      if (isCurrentProjectOperation(operation) && notesRef.current === submittedNotes) setNotes("");
      const successMessage = "故事整理已依備註重建";
      try {
        await refreshProjectAfterMutation(requestedProjectId, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) setMessage(`故事重建失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      finishProjectMutation(mutation);
    }
  }

  async function createProject() {
    if (creatingProjectRef.current) return;
    const name = newProjectName.trim();
    if (!name) {
      setMessage("請先輸入專案名稱。");
      return;
    }
    if (projects.some((project) => project.name.trim().toLocaleLowerCase() === name.toLocaleLowerCase())) {
      setMessage("已有同名專案，請使用不同名稱。");
      return;
    }
    const operation = navigationIdentityRef.current.begin(currentIdRef.current);
    creatingProjectRef.current = true;
    setCreatingProject(true);
    setMessage("正在建立專案...");
    try {
      const result = await api.createProject(name);
      if (result.ok === false) {
        if (navigationIdentityRef.current.isCurrent(operation, currentIdRef.current)) setMessage("專案建立失敗：操作未成功");
        return;
      }
      try {
        await loadProjects({ throwOnError: true });
      } catch (error) {
        if (navigationIdentityRef.current.isCurrent(operation, currentIdRef.current)) {
          setMessage(refreshFailureMessage("專案已建立", error));
        }
        return;
      }
      if (!navigationIdentityRef.current.isCurrent(operation, currentIdRef.current)) return;
      setNewProjectName("");
      selectProject(result.id);
      setMessage("專案已建立，下一步請匯入素材。");
    } catch (error) {
      if (navigationIdentityRef.current.isCurrent(operation, currentIdRef.current)) {
        setMessage(`專案建立失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      creatingProjectRef.current = false;
      setCreatingProject(false);
    }
  }

  function selectProject(projectId: number) {
    if (projectId === currentId) return;
    currentIdRef.current = projectId;
    navigationIdentityRef.current.switchProject();
    mutationCoordinatorRef.current.switchProject();
    setMutationBusy(null);
    loaderRef.current?.invalidate();
    setCurrentId(projectId);
    setDetail(null);
    setJobs([]);
    setNotes("");
    setMessage("");
  }

  function beginProjectOperation(projectId: number): ProjectOperationToken {
    return navigationIdentityRef.current.begin(projectId);
  }

  function beginProjectMutation(projectId: number, mutation: ProjectMutation): ProjectMutationToken | null {
    if (currentIdRef.current !== projectId) return null;
    const token = mutationCoordinatorRef.current.begin(projectId, mutation);
    if (!token) {
      if (currentIdRef.current === projectId) {
        setMessage(`目前正在${mutationLabel(mutation)}，請完成後再執行其他操作。`);
      }
      return null;
    }
    setMutationBusy(token);
    return token;
  }

  function finishProjectMutation(token: ProjectMutationToken): void {
    mutationCoordinatorRef.current.finish(token);
    setMutationBusy(mutationCoordinatorRef.current.current());
  }

  const mutationControls: ProjectMutationControls = {
    beginProjectMutation,
    finishProjectMutation,
    isCurrentProject: (projectId) => currentIdRef.current === projectId,
    isProjectMutationBusy: (projectId) => mutationCoordinatorRef.current.isBusy(projectId),
    currentProjectMutation: (projectId) => {
      const current = mutationCoordinatorRef.current.current();
      return !current || projectId === undefined || current.projectId === projectId ? current : null;
    },
  };

  function isCurrentProjectOperation(operation: ProjectOperationToken): boolean {
    return navigationIdentityRef.current.isCurrent(operation, currentIdRef.current);
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
        : <ProjectView key={detail.project.id} detail={detail} jobs={jobs} bgmTracks={bgmTracks} notes={notes} setNotes={setNotes} setMessage={setMessage} refreshProject={refreshProject} refreshProjectAfterMutation={refreshProjectAfterMutation} review={review} revise={revise} beginProjectOperation={beginProjectOperation} isCurrentProjectOperation={isCurrentProjectOperation} mutationControls={mutationControls} mutationBusy={mutationBusy} />;

  return <main>
    <aside>
      <h1>Video Vault AI</h1>
      <nav className="sidebar-links" aria-label="主要導覽">
        <a className="nav" href="/bgm">BGM 資料庫</a>
        <a className="nav" href="/classic-bgm">舊版 BGM 上傳</a>
        <a className="nav" href="/classic">舊版工作台</a>
      </nav>
      <div className="new-project">
        <label htmlFor="new-project-name">建立專案</label>
        <input id="new-project-name" value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} onKeyDown={(event) => { if (isCommittedEnter(event)) { event.preventDefault(); void createProject(); } }} placeholder="例如：福岡旅行 2026" maxLength={80} />
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
        {!projectsLoading && filteredProjects.map((project) => <button key={project.id} className={project.id === currentId ? "project active" : "project"} aria-current={project.id === currentId ? "page" : undefined} onClick={() => selectProject(project.id)}>
          <b>{project.name}</b>
          <span>#{project.id} · {projectStatusLabel(project.status)} · {project.video_count ?? 0} 支素材</span>
        </button>)}
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
  </main>;
}

function BgmPage() {
  const [tracks, setTracks] = useState<BgmTrack[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api.bgm().then(setTracks).catch((reason) => setError(reason instanceof Error ? reason.message : "未知錯誤"));
  }, []);
  return <main>
    <aside>
      <h1>BGM 資料庫</h1>
      <a className="nav" href="/">專案工作台</a>
      <a className="nav" href="/classic-bgm">上傳 BGM</a>
    </aside>
    <section>
      <div className="hero"><div><h2>本地 BGM 總覽</h2><p>{tracks.length} 首可用音樂</p></div></div>
      {error && <div className="notice" role="alert">BGM 載入失敗：{error}</div>}
      <div className="grid">
        {tracks.map((track) => <Card key={track.id} title={track.title}>
          <p>{track.artist || "未知作者"} | {track.license_name || "未填授權"} | {track.mood || "未分類"}</p>
          {track.source_url && <p><a href={track.source_url} target="_blank" rel="noreferrer">來源</a></p>}
          <pre>{track.attribution_text || "尚未填寫 YouTube 署名文字。"}</pre>
        </Card>)}
        {!tracks.length && !error && <Card title="尚無 BGM"><p>請先到舊版 BGM 上傳頁登錄本地音樂。</p></Card>}
      </div>
    </section>
  </main>;
}

function ProjectView({ detail, jobs, bgmTracks, notes, setNotes, setMessage, refreshProject, refreshProjectAfterMutation, review, revise, beginProjectOperation, isCurrentProjectOperation, mutationControls, mutationBusy }: {
  detail: ProjectDetail;
  jobs: Job[];
  bgmTracks: BgmTrack[];
  notes: string;
  setNotes: (value: string) => void;
  setMessage: (value: string) => void;
  refreshProject: (projectId: number, options?: ProjectDataLoadOptions) => Promise<Job[]>;
  refreshProjectAfterMutation: (projectId: number, operation?: ProjectOperationToken) => Promise<Job[]>;
  review: (action: "approve" | "reject") => void;
  revise: () => void;
  beginProjectOperation: (projectId: number) => ProjectOperationToken;
  isCurrentProjectOperation: (operation: ProjectOperationToken) => boolean;
  mutationControls: ProjectMutationControls;
  mutationBusy: ProjectMutationToken | null;
}) {
  const [submitting, setSubmitting] = useState(false);
  const refreshCurrentProject = (options: ProjectDataLoadOptions = {}) => refreshProject(detail.project.id, options);
  const includedSegments = detail.storyboard?.segments
    ? Object.values(detail.storyboard.segments).filter((segment) => segment.included).length
    : detail.segments.filter((segment) => segment.include !== false).length;
  const workflowSteps = projectWorkflowSteps(detail, jobs);
  const outputDone = workflowSteps[workflowSteps.length - 1]?.done ?? false;
  const renderRunning = jobs.some((job) => Boolean(job.job_id) && ["queued", "running", "cancelling"].includes(job.status));
  const projectMutationBusy = Boolean(mutationBusy) || mutationControls.isProjectMutationBusy(detail.project.id);

  return <>
    <div className="hero">
      <div>
        <h2>{detail.project.name}</h2>
        <ProjectLocation projectId={detail.project.id} folder={detail.folder} setMessage={setMessage} />
      </div>
      <Status value={detail.project.status} />
    </div>
    <div className="project-metrics" aria-label="專案摘要">
      <div><span>素材</span><b>{detail.clips.length}</b></div>
      <div><span>感知片段</span><b>{detail.segments.length}</b></div>
      <div><span>納入成片</span><b>{includedSegments}</b></div>
      <div><span>正式輸出</span><b>{outputDone ? "已完成" : detail.can_render ? "可開始" : "待核准"}</b></div>
    </div>
    <WorkspaceNavigation />

    <div className="workspace-section" id="workspace-overview" tabIndex={-1}>
      <RenderJobPanel jobs={jobs} projectId={detail.project.id} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
      <ProjectWorkflow detail={detail} jobs={jobs} />
      <WorkflowSkeleton detail={detail} />
      <StorageWorkspace projectId={detail.project.id} setMessage={setMessage} />
    </div>

    <div className="workspace-section" id="workspace-storyboard" tabIndex={-1}>
      <StoryboardWorkspaceController detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
      <StoryUnderstandingWorkspace detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
    </div>

    <div className="workspace-section" id="workspace-review" tabIndex={-1}>
      <div className="grid">
        <Card title="審核">
          <p>Gate：{detail.can_render ? "可正式輸出" : detail.render_gate_reason}</p>
          <div className="review-note-heading">
            <label htmlFor="review-notes">審核與重建備註</label>
            {notes.trim() && <span role="status">有尚未送出的備註</span>}
          </div>
          <textarea id="review-notes" value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="記錄核准理由、退回項目或重建故事需求" />
          <div className="row">
            <button type="button" disabled={!notes} onClick={() => setNotes("")}>清除備註</button>
            <button type="button" className="good" disabled={projectMutationBusy} onClick={() => review("approve")}>核准專案</button>
            <button type="button" className="danger" disabled={projectMutationBusy} onClick={() => review("reject")}>退回修改</button>
            <button type="button" disabled={projectMutationBusy || !notes.trim()} onClick={revise}>依備註重建故事</button>
          </div>
        </Card>
        <Card title="輸出">
          <div className="output-actions">
            <button type="button" disabled={projectMutationBusy} onClick={() => void exportProject("hyperframes")}>產生初剪專案</button>
            <button type="button" disabled={projectMutationBusy || !detail.can_render} onClick={() => void exportProject("hyperframes-render")}>快速輸出 MP4</button>
            <button type="button" className="good" disabled={projectMutationBusy || submitting || !detail.can_render || renderRunning} onClick={() => void startFormalRender()}>{submitting ? "正在建立正式輸出…" : renderRunning ? "正式輸出進行中" : "正式輸出（Render Job）"}</button>
            <button type="button" disabled={projectMutationBusy} onClick={() => void exportProject("opencut")}>OpenCut 素材包</button>
            <button type="button" disabled={projectMutationBusy || !detail.can_render} onClick={() => void exportProject("opencut-render")}>OpenCut 調色片段</button>
          </div>
        </Card>
      </div>
    </div>

    <div className="workspace-section" id="workspace-media" tabIndex={-1}>
      <div className="grid">
        <Card title="素材">
          <div className="row">
            <input type="file" multiple accept="video/*" disabled={projectMutationBusy} onChange={uploadFiles} />
            <button type="button" disabled={projectMutationBusy} onClick={() => void analyze(true)}>全部重跑感知</button>
            <button type="button" disabled={projectMutationBusy || !detail.clips.length} onClick={() => void buildPlan()}>產生故事整理</button>
          </div>
          {detail.clips.map((clip) => <div className="item" key={`${detail.project.id}:${clip.clip_id}`}>
            <div className="row">
              <b>{clip.clip_id}</b>
            </div>
            {clip.filename}
            <span>{projectStatusLabel(clip.status)} · {clip.segment_count} 段 · {Math.round(clip.duration_seconds || 0)} 秒 · {clip.time_of_day || "未分類時段"}</span>
            <PerceptionSamplingControls clip={clip} disabled={projectMutationBusy} onAnalyze={(sampling) => analyzeOne(clip.video_id, sampling)} />
            <MultiFrameEvidencePanel
              clip={clip}
              projectId={detail.project.id}
              projectRevision={detail.project_revision}
              setMessage={setMessage}
              onSaved={async () => { await refreshCurrentProject({ forceFresh: true }); }}
            />
            <ClipSummaryEditor projectId={detail.project.id} projectRevision={detail.project_revision} clip={clip} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
          </div>)}
          {!detail.clips.length && <div className="inline-empty">尚無素材。先選擇多支影片匯入，再進行內容感知。</div>}
        </Card>
        <ColorConsistencyWorkspace detail={detail} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
      </div>
    </div>

    <div className="workspace-section" id="workspace-audio" tabIndex={-1}>
      <AudioMixingWorkspace detail={detail} bgmTracks={bgmTracks} setMessage={setMessage} refreshProject={refreshCurrentProject} mutationControls={mutationControls} />
    </div>

    <div className="workspace-section" id="workspace-script" tabIndex={-1}>
      <Card title="故事整理"><pre>{detail.script || "尚未產生故事整理。"}</pre></Card>
    </div>
  </>;

  async function exportProject(kind: "hyperframes" | "hyperframes-render" | "opencut" | "opencut-render") {
    const localAction = kind.startsWith("hyperframes")
      ? "這會在本機產生 HyperFrames 交接檔並開啟輸出資料夾，是否繼續？"
      : "這會在本機啟動 OpenCut 並開啟素材包資料夾，是否繼續？";
    const confirmed = window.confirm(localAction);
    if (confirmed === false) return;
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "export");
    if (!mutation) return;
    const operation = beginProjectOperation(detail.project.id);
    try {
      setMessage("工作已送出，請看 Render Job 狀態與進度。");
      const result = kind.startsWith("hyperframes")
        ? await api.hyperframesJob(detail.project.id, kind === "hyperframes-render", detail.project_revision)
        : await api.opencutJob(detail.project.id, kind === "opencut-render", detail.project_revision);
      if (!result.ok) {
        if (isCurrentProjectOperation(operation)) setMessage(`工作啟動失敗：${result.error || result.message || "工作未成功送出"}`);
        return;
      }
      const successMessage = result.message || "工作已開始";
      try {
        await refreshProjectAfterMutation(detail.project.id, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error, "工作狀態"));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`工作啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function startFormalRender() {
    if (!detail.can_render) {
      setMessage(`正式輸出被擋下：${detail.render_gate_reason}`);
      return;
    }
    const requestedProjectId = detail.project.id;
    const mutation = mutationControls.beginProjectMutation(requestedProjectId, "render");
    if (!mutation) return;
    const operation = beginProjectOperation(requestedProjectId);
    try {
      setSubmitting(true);
      setMessage("正在建立正式輸出…");
      const result = await api.createRenderJob(requestedProjectId);
      if (!result.ok) {
        if (!isCurrentProjectOperation(operation)) return;
        setMessage(`正式輸出失敗：${result.error || "建立 Render Job 未成功"}`);
        return;
      }
      const successMessage = result.created ? "正式輸出已排入佇列" : "正式輸出工作已在執行中";
      try {
        await refreshProjectAfterMutation(requestedProjectId, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error, "工作狀態"));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`正式輸出啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      setSubmitting(false);
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function uploadFiles(event: ChangeEvent<HTMLInputElement>) {
    const input = event.currentTarget;
    const files = Array.from(input.files || []);
    input.value = "";
    if (!files?.length) return;
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "upload");
    if (!mutation) return;
    const operation = beginProjectOperation(detail.project.id);
    setMessage("正在匯入素材...");
    try {
      const result = await api.uploadProject(detail.project.id, files, detail.project_revision);
      if (!result.ok) {
        if (isCurrentProjectOperation(operation)) setMessage(`素材匯入失敗：${result.error || "操作未成功"}`);
        return;
      }
      const successMessage = `已匯入 ${result.files?.length || 0} 支素材`;
      try {
        await refreshProjectAfterMutation(detail.project.id, operation);
        if (isCurrentProjectOperation(operation)) setMessage(`${successMessage}，下一步請跑內容感知。`);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`素材匯入失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function analyze(force: boolean) {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "analyze");
    if (!mutation) return;
    const operation = beginProjectOperation(detail.project.id);
    setMessage(force ? "已送出全部重跑感知。" : "已送出待感知素材。");
    try {
      const result = await api.analyzeJob(detail.project.id, force, detail.project_revision);
      if (!result.ok) {
        if (isCurrentProjectOperation(operation)) setMessage(`內容感知啟動失敗：${result.message || "操作未成功"}`);
        return;
      }
      const successMessage = result.message || "內容感知工作已開始";
      try {
        await refreshProjectAfterMutation(detail.project.id, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`內容感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function analyzeOne(videoId: number, sampling?: import("./api").SamplingOverride) {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "analyze");
    if (!mutation) return;
    const operation = beginProjectOperation(detail.project.id);
    setMessage("已送出單支素材感知，請看工作狀態百分比。");
    try {
      const result = await api.analyzeVideo(detail.project.id, videoId, detail.project_revision, sampling);
      if (!result.ok) {
        if (isCurrentProjectOperation(operation)) setMessage(`單支素材感知啟動失敗：${result.message || "操作未成功"}`);
        return;
      }
      const successMessage = result.message || "單支素材感知已開始";
      try {
        await refreshProjectAfterMutation(detail.project.id, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`單支素材感知啟動失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
    }
  }

  async function buildPlan() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "revise");
    if (!mutation) return;
    const operation = beginProjectOperation(detail.project.id);
    setMessage("正在產生故事整理...");
    try {
      const result = await (detail.project_revision === undefined ? api.buildPlan(detail.project.id) : api.buildPlan(detail.project.id, detail.project_revision));
      if (!result.ok) {
        if (isCurrentProjectOperation(operation)) setMessage("故事整理失敗：操作未成功");
        return;
      }
      const successMessage = "故事整理已更新，請審核片段。";
      try {
        await refreshProjectAfterMutation(detail.project.id, operation);
        if (isCurrentProjectOperation(operation)) setMessage(successMessage);
      } catch (error) {
        if (isCurrentProjectOperation(operation)) setMessage(refreshFailureMessage(successMessage, error));
      }
    } catch (error) {
      if (isCurrentProjectOperation(operation)) {
        setMessage(`故事整理失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      mutationControls.finishProjectMutation(mutation);
    }
  }
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
  return <details className="workflow-diagnostics">
    <summary>進階工作流診斷 · {detail.workflow.stages.length} 個階段</summary>
    <div className="workflow-grid">
      {detail.workflow.stages.map((stage) => <div className="workflow-card" key={stage.id}>
        <b>{stage.label}</b>
        <span className={["done", "completed", "succeeded", "success"].includes(stage.status) ? "pill ok" : "pill"}>{projectStatusLabel(stage.status)}</span>
        {stage.artifacts.length > 0 ? <details>
          <summary>{stage.artifacts.length} 個產物</summary>
          {stage.artifacts.map((artifact) => <code key={artifact} title={artifact}>{artifact}</code>)}
        </details> : <small>尚無產物</small>}
      </div>)}
      {!detail.workflow.stages.length && <div className="inline-empty">目前沒有進階工作流資料。</div>}
    </div>
  </details>;
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
    queued: "佇列中",
    running: "執行中",
    cancelling: "取消中",
    cancelled: "已取消",
    stopped: "已停止",
    done: "已完成",
    completed: "已完成",
    succeeded: "成功",
    success: "成功",
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
