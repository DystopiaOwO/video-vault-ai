import { useMemo, useRef, useState } from "react";
import { api, type Job } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import { createProjectMutationControls, mutationLabel, ProjectMutationCoordinator, type ProjectMutationControls } from "../../projectMutation";
import { copyText } from "../../utils/clipboard";
import "./render-job-panel.css";

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);
const SUCCESS_STATUSES = new Set(["succeeded", "success", "completed", "done"]);
const FAILURE_STATUSES = new Set(["failed", "interrupted"]);
const DEFAULT_VISIBLE_JOBS = 6;

const STATUS_LABELS: Record<string, string> = {
  queued: "排隊中",
  running: "執行中",
  cancelling: "停止中",
  cancelled: "已取消",
  succeeded: "已完成",
  success: "已完成",
  completed: "已完成",
  done: "已完成",
  failed: "失敗",
  interrupted: "已中斷",
  stopped: "已停止",
};

const STAGE_LABELS: Record<string, string> = {
  queued: "等待工作",
  validating: "驗證輸出條件",
  segments: "輸出片段",
  assembling: "組合時間軸",
  final_qc: "最終品質檢查",
  publishing: "發布正式檔案",
  done: "完成",
};

export type JobFilter = "all" | "active" | "completed" | "failed";

type Props = {
  jobs: Job[];
  projectId: number;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
  mutationControls?: ProjectMutationControls;
};

function updatedTimestamp(job: Job): number {
  const timestamp = job.updated_at ? Date.parse(job.updated_at) : 0;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function sortRenderJobs(jobs: Job[]): Job[] {
  return [...jobs].sort((left, right) => {
    const activeDifference = Number(ACTIVE_STATUSES.has(right.status)) - Number(ACTIVE_STATUSES.has(left.status));
    if (activeDifference) return activeDifference;
    const timestampDifference = updatedTimestamp(right) - updatedTimestamp(left);
    if (timestampDifference) return timestampDifference;
    const formalDifference = Number(Boolean(right.job_id)) - Number(Boolean(left.job_id));
    if (formalDifference) return formalDifference;
    return String(right.job_id || right.legacy_job_key || right.kind).localeCompare(String(left.job_id || left.legacy_job_key || left.kind));
  });
}

export function filterRenderJobs(jobs: Job[], filter: JobFilter): Job[] {
  if (filter === "active") return jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  if (filter === "completed") return jobs.filter((job) => SUCCESS_STATUSES.has(job.status));
  if (filter === "failed") return jobs.filter((job) => FAILURE_STATUSES.has(job.status));
  return jobs;
}

export function RenderJobPanel({ jobs, projectId, setMessage, refreshProject, mutationControls }: Props) {
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<JobFilter>("all");
  const [expanded, setExpanded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const pendingCancelsRef = useRef<Job[]>([]);
  const fallbackControlsRef = useRef<ProjectMutationControls | null>(null);
  if (!fallbackControlsRef.current) fallbackControlsRef.current = createProjectMutationControls(new ProjectMutationCoordinator());
  const controls = mutationControls || fallbackControlsRef.current;
  const sortedJobs = useMemo(() => sortRenderJobs(jobs), [jobs]);
  const filteredJobs = useMemo(() => filterRenderJobs(sortedJobs, filter), [filter, sortedJobs]);
  const displayedJobs = expanded ? filteredJobs : filteredJobs.slice(0, DEFAULT_VISIBLE_JOBS);
  const activeJobs = jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  const hasFormalJob = jobs.some((job) => Boolean(job.job_id));
  const hiddenJobCount = Math.max(0, filteredJobs.length - displayedJobs.length);
  const projectMutationBusy = controls.isProjectMutationBusy(projectId);
  const renderCancelBlocked = projectMutationBusy
    && controls.currentProjectMutation(projectId)?.mutation !== "render-cancel";

  function setProjectMessage(message: string) {
    if (controls.isCurrentProject(projectId)) setMessage(message);
  }

  async function cancel(job: Job) {
    const key = jobKey(job, projectId);
    const mutation = controls.beginProjectMutation(projectId, "render-cancel");
    if (!mutation) {
      const current = controls.currentProjectMutation(projectId);
      if (current?.mutation === "render-cancel" && !cancelling.has(key)) {
        setCancelling((value) => new Set(value).add(key));
        pendingCancelsRef.current.push(job);
        setProjectMessage("停止要求已排入目前工作之後。 ");
        return;
      }
      if (controls.isCurrentProject(projectId)) {
        setProjectMessage(`目前正在${mutationLabel("render-cancel")}，請完成後再執行其他操作。`);
      }
      return;
    }
    setCancelling((current) => new Set(current).add(key));
    setProjectMessage("正在停止指定工作...");
    try {
      const result = job.job_id
        ? await api.cancelRenderJob(job.job_id)
        : await api.cancelLegacyJob(projectId, job.legacy_job_key || job.kind);
      if (!result.ok) {
        const error = ("error" in result && result.error) || ("reason" in result && result.reason) || "停止要求未成功";
        try {
          await refreshProject({ forceFresh: true, throwOnError: true });
          setProjectMessage(`停止失敗：${error}`);
        } catch (refreshError) {
          if (controls.isCurrentProject(projectId)) {
            setProjectMessage(`停止失敗：${error}；但畫面更新失敗：${refreshError instanceof Error ? refreshError.message : "未知錯誤"}`);
          }
        }
        return;
      }
      const successMessage = ("message" in result ? result.message : ("reason" in result ? result.reason : undefined)) || "停止要求已送出";
      try {
        await refreshProject({ forceFresh: true, throwOnError: true });
        setProjectMessage(successMessage);
      } catch (refreshError) {
        if (controls.isCurrentProject(projectId)) {
          setProjectMessage(`${successMessage}，但畫面更新失敗：${refreshError instanceof Error ? refreshError.message : "未知錯誤"}`);
        }
      }
    } catch (error) {
      if (controls.isCurrentProject(projectId)) {
        setProjectMessage(`停止失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      setCancelling((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      controls.finishProjectMutation(mutation);
      const next = pendingCancelsRef.current.shift();
      if (next && controls.isCurrentProject(projectId)) void cancel(next);
    }
  }

  async function refresh() {
    setRefreshing(true);
    try {
      await refreshProject({ forceFresh: true });
      setProjectMessage("工作狀態已更新。");
    } catch (error) {
      if (controls.isCurrentProject(projectId)) {
        setProjectMessage(`工作狀態更新失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
      }
    } finally {
      setRefreshing(false);
    }
  }

  async function copyPath(path: string, label: string) {
    const copied = await copyText(path);
    setProjectMessage(copied ? `${label}路徑已複製。` : `無法複製${label}路徑，請展開後手動複製。`);
  }

  return <section className="render-job-panel" aria-live="polite">
    <div className="panel-heading render-job-heading">
      <div>
        <h3>Render Job</h3>
        <p className="muted">正式輸出會在背景執行；執行中工作固定排在最前面。</p>
      </div>
      <div className="render-job-heading-actions">
        <span className={activeJobs.length ? "status-dot active" : "status-dot"}>
          {activeJobs.length ? `${activeJobs.length} 項執行中` : "目前沒有執行中工作"}
        </span>
        <label>
          <span>顯示</span>
          <select aria-label="篩選 Render Job" value={filter} onChange={(event) => { setFilter(event.target.value as JobFilter); setExpanded(false); }}>
            <option value="all">全部（{jobs.length}）</option>
            <option value="active">執行中（{activeJobs.length}）</option>
            <option value="completed">已完成（{jobs.filter((job) => SUCCESS_STATUSES.has(job.status)).length}）</option>
            <option value="failed">失敗（{jobs.filter((job) => FAILURE_STATUSES.has(job.status)).length}）</option>
          </select>
        </label>
        <button type="button" disabled={refreshing} onClick={() => void refresh()}>{refreshing ? "更新中…" : "立即更新"}</button>
      </div>
    </div>

    {!jobs.length && <p className="empty-state">尚未建立 Render Job。</p>}
    {jobs.length > 0 && !filteredJobs.length && <p className="empty-state">這個篩選條件目前沒有工作。</p>}

    <div className="job-list">
      {displayedJobs.map((job, index) => {
        const key = jobKey(job, projectId);
        const cancellingJob = job.status === "cancelling" || cancelling.has(key);
        return <article className={job.job_id ? "job-row formal" : "job-row"} key={key || `${job.kind}-${index}`}>
          <div className="job-primary">
            <div className="job-title-line">
              <strong>{job.job_id ? "正式輸出" : job.kind}</strong>
              <span className={`job-status ${statusClass(job.status)}`}>{STATUS_LABELS[job.status] || job.status}</span>
            </div>
            <span className="job-message">{job.message || "等待更新"}</span>
            {job.stage && <span className="job-stage">階段：{STAGE_LABELS[job.stage] || job.stage}</span>}
            {job.job_id && ACTIVE_STATUSES.has(job.status) && job.current_segment_id && <span className="job-stage">
              目前片段：{job.current_segment_id}{segmentPosition(job)}
            </span>}
            {job.updated_at && <span className="job-updated">更新：{formatUpdatedAt(job.updated_at)}</span>}
          </div>

          <div className="job-progress-block">
            <div className="job-progress-label"><span>進度</span><strong>{formatPercent(job.percent)}%</strong></div>
            <progress aria-label={`${job.job_id ? "正式輸出" : job.kind}進度`} value={safePercent(job.percent)} max={100} />
          </div>

          <div className="job-meta">
            {job.job_id && SUCCESS_STATUSES.has(job.status) && job.cache_hit !== undefined && <span>Final Cache：{job.cache_hit ? "命中" : "本次建立"}</span>}
            {job.error && <span className="job-error">錯誤：{job.error}</span>}
            {(job.output_path || job.log_path) && <details className="job-files">
              <summary>{job.output_path ? "輸出與記錄" : "工作記錄"}</summary>
              {job.output_path && <JobPath label="輸出檔案" path={job.output_path} onCopy={() => void copyPath(job.output_path!, "輸出檔案")} />}
              {job.log_path && <JobPath label="工作記錄" path={job.log_path} onCopy={() => void copyPath(job.log_path!, "工作記錄")} />}
            </details>}
          </div>

          {ACTIVE_STATUSES.has(job.status) && <button
            type="button"
            className="danger compact"
            onClick={() => void cancel(job)}
            disabled={cancellingJob || renderCancelBlocked}
          >{cancellingJob ? "停止中..." : job.job_id ? "停止此 Render" : "停止此工作"}</button>}
        </article>;
      })}
    </div>

    {filteredJobs.length > DEFAULT_VISIBLE_JOBS && <button type="button" className="render-job-expand" onClick={() => setExpanded((value) => !value)}>
      {expanded ? "收合歷史工作" : `顯示另外 ${hiddenJobCount} 項工作`}
    </button>}
    {hasFormalJob && <p className="job-footnote">正式輸出完成後，MP4 與 Render Report 會一起出現；發布邊界後的取消不會撤銷已完成輸出。</p>}
  </section>;
}

function JobPath({ label, path, onCopy }: { label: string; path: string; onCopy: () => void }) {
  return <div className="job-file-row">
    <span>{label}</span>
    <code title={path}>{fileName(path)}</code>
    <button type="button" aria-label={`複製${label}路徑`} onClick={onCopy}>複製</button>
  </div>;
}

function jobKey(job: Job, projectId: number): string {
  return job.job_id || `${projectId}:${job.legacy_job_key || job.kind}`;
}

function segmentPosition(job: Job): string {
  if (job.current_segment_index === undefined || job.current_segment_index === null || !job.segment_count) return "";
  return `（${job.current_segment_index}/${job.segment_count}）`;
}

function fileName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function formatUpdatedAt(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return value;
  return new Intl.DateTimeFormat("zh-TW", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(timestamp);
}

function safePercent(value: number | undefined) {
  return Math.max(0, Math.min(100, Number(value) || 0));
}

function formatPercent(value: number | undefined) {
  return safePercent(value).toFixed(1).replace(/\.0$/, "");
}

function statusClass(status: string) {
  if (SUCCESS_STATUSES.has(status)) return "success";
  if (FAILURE_STATUSES.has(status)) return "error";
  if (ACTIVE_STATUSES.has(status)) return "working";
  return "neutral";
}
