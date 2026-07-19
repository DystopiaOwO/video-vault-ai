import { useState } from "react";
import { api, Job } from "../../api";
import { ProjectDataLoadOptions } from "../../projectDataLoader";

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

const STATUS_LABELS: Record<string, string> = {
  queued: "排隊中",
  running: "執行中",
  cancelling: "停止中",
  cancelled: "已取消",
  succeeded: "已完成",
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
  final_qc: "Final QC",
  publishing: "發布正式檔案",
  done: "完成",
};

type Props = {
  jobs: Job[];
  projectId: number;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
};

export function RenderJobPanel({ jobs, projectId, setMessage, refreshProject }: Props) {
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  const activeJobs = jobs.filter((job) => ACTIVE_STATUSES.has(job.status));
  const hasFormalJob = jobs.some((job) => Boolean(job.job_id));

  async function cancel(job: Job) {
    const key = job.job_id || `${projectId}:${job.legacy_job_key || job.kind}`;
    setCancelling((current) => new Set(current).add(key));
    setMessage("正在停止指定工作...");
    try {
      const result = job.job_id
        ? await api.cancelRenderJob(job.job_id)
        : await api.cancelLegacyJob(projectId, job.legacy_job_key || job.kind);
      if (!result.ok) {
        const error = ("error" in result && result.error) || ("reason" in result && result.reason) || "停止要求未成功";
        await refreshProject({ forceFresh: true });
        setMessage(`停止失敗：${error}`);
        return;
      }
      await refreshProject({ forceFresh: true });
      const message = "message" in result ? result.message : ("reason" in result ? result.reason : undefined);
      setMessage(message || "停止要求已送出");
    } catch (error) {
      setMessage(`停止失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setCancelling((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  }

  return (
    <section className="render-job-panel" aria-live="polite">
      <div className="panel-heading">
        <div>
          <h3>Render Job</h3>
          <p className="muted">正式輸出會在背景執行，狀態每次更新都會自動同步。</p>
        </div>
        <span className={activeJobs.length ? "status-dot active" : "status-dot"}>
          {activeJobs.length ? `${activeJobs.length} 項執行中` : "目前沒有執行中工作"}
        </span>
      </div>
      {!jobs.length && <p className="empty-state">尚未建立 Render Job。</p>}
      <div className="job-list">
        {jobs.map((job, index) => (
          <article className={job.job_id ? "job-row formal" : "job-row"} key={job.job_id || `${job.kind}-${index}`}>
            <div className="job-primary">
              <div className="job-title-line">
                <strong>{job.job_id ? "正式輸出" : job.kind}</strong>
                <span className={`job-status ${statusClass(job.status)}`}>{STATUS_LABELS[job.status] || job.status}</span>
              </div>
              <span className="job-message">{job.message || "等待更新"}</span>
              {job.stage && <span className="job-stage">階段：{STAGE_LABELS[job.stage] || job.stage}</span>}
              {job.job_id && ACTIVE_STATUSES.has(job.status) && job.current_segment_id && (
                <span className="job-stage">
                  目前片段：{job.current_segment_id} {job.current_segment_index && job.segment_count ? `（${job.current_segment_index}/${job.segment_count}）` : ""}
                </span>
              )}
            </div>
            <div className="job-progress-block">
              <div className="job-progress-label"><span>進度</span><strong>{formatPercent(job.percent)}%</strong></div>
              <progress value={safePercent(job.percent)} max={100} />
            </div>
            <div className="job-meta">
              {job.job_id && job.status === "succeeded" && job.cache_hit !== undefined && <span>Final Cache：{job.cache_hit ? "命中" : "本次建立"}</span>}
              {job.output_path && <span className="job-path" title={job.output_path}>輸出：{job.output_path}</span>}
              {job.log_path && <span className="job-path" title={job.log_path}>記錄：{job.log_path}</span>}
              {job.error && <span className="job-error">錯誤：{job.error}</span>}
            </div>
            {ACTIVE_STATUSES.has(job.status) && (
              <button
                className="danger compact"
                onClick={() => cancel(job)}
                disabled={job.status === "cancelling" || cancelling.has(job.job_id || `${projectId}:${job.legacy_job_key || job.kind}`)}
              >
                {job.status === "cancelling" || cancelling.has(job.job_id || `${projectId}:${job.legacy_job_key || job.kind}`) ? "停止中..." : job.job_id ? "停止此 Render" : "停止此工作"}
              </button>
            )}
          </article>
        ))}
      </div>
      {hasFormalJob && <p className="job-footnote">正式輸出完成後，MP4 與 Render Report 會一起出現；發布邊界後的取消不會撤銷已完成輸出。</p>}
    </section>
  );
}

function safePercent(value: number | undefined) {
  return Math.max(0, Math.min(100, Number(value) || 0));
}

function formatPercent(value: number | undefined) {
  return safePercent(value).toFixed(1).replace(/\.0$/, "");
}

function statusClass(status: string) {
  if (status === "succeeded" || status === "done") return "success";
  if (status === "failed" || status === "interrupted") return "error";
  if (ACTIVE_STATUSES.has(status)) return "working";
  return "neutral";
}
