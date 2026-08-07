import { useState } from "react";
import { api, type Clip, type PerceptionWindowResult } from "../../api";

type Props = {
  clip: Pick<Clip, "filename" | "perception_run">;
  projectId?: number;
  projectRevision?: number;
  setMessage?: (message: string) => void;
  onSaved?: () => Promise<unknown>;
};

function timecode(value: number | undefined): string {
  const seconds = Math.max(0, Number(value || 0));
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

function WindowEvidence({ item, projectId, projectRevision, setMessage, onSaved }: Props & { item: PerceptionWindowResult }) {
  const [action, setAction] = useState(item.action || "");
  const [shotRole, setShotRole] = useState(item.shot_role || "");
  const [duplicateGroup, setDuplicateGroup] = useState(item.duplicate_group || "");
  const [naturalAudio, setNaturalAudio] = useState(item.natural_audio_recommendation || "unknown");
  const [notes, setNotes] = useState(item.user_notes || "");
  const [include, setInclude] = useState(item.include !== false);
  const [locked, setLocked] = useState(Boolean(item.locked));
  const [saving, setSaving] = useState(false);
  const urls = item.evidence_urls || {};
  const validation = item.validation?.status || "unknown";
  async function saveEvidence() {
    if (!projectId || !item.segment_uuid) {
      setMessage?.("此證據尚未完成穩定片段對應，請先重新執行內容感知。 ");
      return;
    }
    setSaving(true);
    try {
      const result = await api.saveSegmentEvidence(projectId, item.segment_uuid, {
        action,
        shot_role: shotRole,
        duplicate_group: duplicateGroup,
        natural_audio_recommendation: naturalAudio,
        user_notes: notes,
        include,
        locked,
      }, projectRevision);
      if (!result.ok) {
        setMessage?.(`證據修正失敗：${result.error || "儲存未成功"}`);
        return;
      }
      await onSaved?.();
      setMessage?.("證據人工修正已儲存，專案需要重新核准。 ");
    } catch (error) {
      setMessage?.(`證據修正失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setSaving(false);
    }
  }
  return <details className="multiframe-window">
    <summary>
      <span>視窗 {item.ordinal || "-"}</span>
      <span>{timecode(item.start_seconds)} - {timecode(item.end_seconds)}</span>
      <span className={`evidence-status evidence-${validation}`}>{validation}</span>
      <span>{Math.round(Number(item.confidence || 0) * 100)}%</span>
    </summary>
    <div className="multiframe-window-body">
      {urls.contact_sheet && <img src={urls.contact_sheet} alt={`${item.summary || "多幀證據"} contact sheet`} loading="lazy" />}
      <div className="multiframe-facts">
        <b>{item.summary || "尚無描述"}</b>
        <span>動作：{item.action || "未判定"}</span>
        <span>鏡頭角色：{item.shot_role || "未判定"}</span>
        <span>技術品質：{Math.round(Number(item.technical_quality?.score || 0) * 100)}%</span>
        <span>自然音：{item.natural_audio_recommendation || "未判定"}</span>
        {item.duplicate_group && <span>重複群組：{item.duplicate_group}</span>}
        <span>證據影格：{(item.frame_timestamps || []).map(timecode).join("、") || "無"}</span>
        {!!item.validation?.needs_review_reasons?.length && <span className="muted">需注意：{item.validation.needs_review_reasons.join("、")}</span>}
      </div>
      <div className="multiframe-manual-editor" aria-label="人工修正感知證據">
        <label>動作 <input value={action} onChange={(event) => setAction(event.target.value)} /></label>
        <label>鏡頭角色 <input value={shotRole} onChange={(event) => setShotRole(event.target.value)} /></label>
        <label>重複群組 <input value={duplicateGroup} onChange={(event) => setDuplicateGroup(event.target.value)} /></label>
        <label>自然音 <input value={naturalAudio} onChange={(event) => setNaturalAudio(event.target.value)} /></label>
        <label><input type="checkbox" checked={include} onChange={(event) => setInclude(event.target.checked)} /> 納入片段</label>
        <label><input type="checkbox" checked={locked} onChange={(event) => setLocked(event.target.checked)} /> 鎖定人工判定</label>
        <label>備註 <textarea value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
        <button type="button" disabled={saving || !item.segment_uuid} onClick={() => void saveEvidence()}>{saving ? "儲存中…" : "儲存人工修正"}</button>
      </div>
      <div className="row">
        {urls.window && <a className="buttonlink" href={urls.window} target="_blank" rel="noreferrer">查看視窗 JSON</a>}
        {urls.normalized && <a className="buttonlink" href={urls.normalized} target="_blank" rel="noreferrer">查看標準結果</a>}
      </div>
    </div>
  </details>;
}

export function MultiFrameEvidencePanel({ clip, projectId, projectRevision, setMessage, onSaved }: Props) {
  const perception = clip.perception_run;
  const results = perception?.current_window_results || [];
  const contract = perception?.multi_frame_contract;
  if (!perception || (!results.length && !perception.current_window_validation)) return null;

  return <section className="multiframe-evidence" aria-label={`${clip.filename} 多幀感知證據`}>
    <div className="clip-summary-heading">
      <strong>多幀感知證據</strong>
      <span>{results.length ? `${results.length} 個視窗` : "尚無可用視窗"}</span>
    </div>
    <div className="muted">
      狀態：{perception.current_window_validation?.status || "unknown"}
      {contract?.provider ? ` · Provider：${String(contract.provider)}` : ""}
      {contract?.model ? ` · 模型：${String(contract.model)}` : ""}
    </div>
    {results.map((item) => <WindowEvidence key={item.window_uuid} item={item} projectId={projectId} projectRevision={projectRevision} setMessage={setMessage} onSaved={onSaved} clip={clip} />)}
    <div className="row">
      <a className="buttonlink" href="#workspace-storyboard">前往分鏡審核修改</a>
      <span className="muted">人工剪點、納入與備註會保留此處的證據對照。</span>
    </div>
  </section>;
}
