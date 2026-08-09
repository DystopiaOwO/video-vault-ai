import { useEffect, useMemo, useState } from "react";
import { api, type DoctorReport } from "../api";
import "./system-status-page.css";

const labels: Record<string, string> = {
  "runtime": "Runtime",
  "runtime.media": "Runtime / Media",
  "frontend": "Frontend / HyperFrames",
  "storage": "SQLite / Library",
  "provider": "AI Provider",
  "configuration": "Configuration",
  "assets": "Assets",
};

const statusLabels: Record<string, string> = {
  pass: "通過",
  warning: "警告",
  blocked: "阻擋",
  skipped: "略過",
};

export function SystemStatusPage() {
  const [report, setReport] = useState<DoctorReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<"default" | "quick" | "full">("default");
  const [error, setError] = useState("");

  async function run(nextMode: "default" | "quick" | "full" = mode, checkId?: string) {
    setLoading(true);
    setError("");
    try {
      const result = await api.doctor(nextMode, checkId);
      if (checkId && report) {
        const checks = report.checks.map((check) => check.check_id === checkId ? result.checks[0] || check : check);
        const counts = (["pass", "warning", "blocked", "skipped"] as const).reduce((acc, status) => ({ ...acc, [status]: checks.filter((check) => check.status === status).length }), { pass: 0, warning: 0, blocked: 0, skipped: 0 });
        const status = counts.blocked ? "blocked" : counts.warning || counts.skipped ? "warning" : "pass";
        setReport({ ...report, generated_at: result.generated_at, status, ok: status !== "blocked", summary: counts, checks });
      } else {
        setReport(result);
      }
      setMode(nextMode);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "系統狀態載入失敗");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void run(); }, []);

  const grouped = useMemo(() => {
    const groups = new Map<string, DoctorReport["checks"]>();
    for (const check of report?.checks || []) groups.set(check.category, [...(groups.get(check.category) || []), check]);
    return [...groups.entries()];
  }, [report]);

  function exportReport() {
    if (!report) return;
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `video-vault-doctor-${report.mode}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return <main className="system-status-page">
    <aside>
      <h1>系統狀態</h1>
      <nav className="sidebar-links" aria-label="系統狀態導覽">
        <a className="nav" href="/">專案工作台</a>
        <a className="nav" href="/bgm">BGM 資料庫</a>
      </nav>
      <p className="muted">Doctor 預設唯讀，不會安裝套件、修改設定或下載模型。</p>
    </aside>
    <section>
      <div className="hero system-status-hero">
        <div>
          <h2>本機環境健檢</h2>
          <p>{report ? `最後檢查：${new Date(report.generated_at).toLocaleString()}` : "正在取得狀態…"}</p>
        </div>
        {report && <span className={`doctor-status doctor-${report.status}`}>{statusLabels[report.status] || report.status}</span>}
      </div>
      {error && <div className="notice" role="alert">系統狀態載入失敗：{error}</div>}
      <div className="system-status-actions" aria-label="執行健檢">
        <button type="button" disabled={loading} onClick={() => void run("quick")}>快速檢查</button>
        <button type="button" disabled={loading} onClick={() => void run("full")}>完整檢查</button>
        <button type="button" disabled={loading || !report} onClick={exportReport}>匯出去敏 JSON</button>
      </div>
      {report && <>
        <div className="doctor-summary" aria-label="健檢摘要">
          {(["pass", "warning", "blocked", "skipped"] as const).map((status) => <div key={status} className={`doctor-summary-${status}`}><span>{statusLabels[status]}</span><b>{report.summary[status] || 0}</b></div>)}
        </div>
        <p className="muted">模式：{report.mode} · schema：{report.schema_version} · 敏感資料已去除：{report.sensitive_data_redacted ? "是" : "否"}</p>
        {grouped.map(([category, checks]) => <details className="doctor-category" key={category} open>
          <summary><b>{labels[category] || category}</b><span>{checks.length} 項</span></summary>
          <div className="doctor-checks">
            {checks.map((check) => <article className={`doctor-check doctor-check-${check.status}`} key={check.check_id}>
              <div className="doctor-check-heading"><strong>{check.check_id}</strong><span>{statusLabels[check.status] || check.status}</span></div>
              <p>{check.summary}</p>
              {Object.keys(check.evidence || {}).length > 0 && <pre className="doctor-evidence">{JSON.stringify(check.evidence, null, 2)}</pre>}
              {check.remediation && <p className="muted">建議：{check.remediation}</p>}
              <div className="doctor-check-footer"><small>{check.duration_ms || 0} ms</small><button type="button" disabled={loading} onClick={() => void run(mode, check.check_id)}>重新檢查</button></div>
            </article>)}
          </div>
        </details>)}
      </>}
      {loading && <div role="status" className="inline-empty">正在執行健檢…</div>}
    </section>
  </main>;
}
