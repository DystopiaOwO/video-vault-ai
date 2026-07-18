import type { Job } from "../../api";

export function RenderJobPanel({ jobs, onCancel }: { jobs: Job[]; onCancel: (job?: Job) => void }) {
  const running = jobs.filter((job) => job.status === "queued" || job.status === "running");
  return <div className="jobs">
    {running.length > 0 && <div className="running-banner"><span className="spinner" /><b>{running.length} 個工作執行中</b><span>可個別停止，不影響其他工作</span></div>}
    {jobs.map((job) => <article className="job" key={job.job_id ?? `${job.kind}-${job.status}`}>
      <div className="job-head"><b>{job.kind}</b><span className={`status status-${job.status}`}>{job.status}</span><strong>{Math.max(0, Math.min(100, job.percent))}%</strong></div>
      <progress value={job.percent} max={100} />
      <div className="job-grid"><span>階段 <b>{job.stage ?? "-"}</b></span><span>片段 <b>{job.current_segment || "-"}{job.total_segments ? ` / ${job.total_segments}` : ""}</b></span><span>Cache <b>{job.cache_hit ?? 0} hit / {job.cache_miss ?? 0} miss</b></span><span>Encoder <b>{job.encoder ?? "-"}</b></span></div>
      {job.output && <code>{job.output}</code>}
      {job.error && <p className="job-error">{job.error}</p>}
      <div className="job-footer"><span className="muted">{job.message}</span>{job.log_path && <button className="link-button" onClick={() => window.open(job.log_path, "_blank")}>查看 Log</button>}{(job.status === "queued" || job.status === "running") && <button className="danger" onClick={() => onCancel(job)}>停止</button>}</div>
    </article>)}
  </div>;
}
