import type { Clip, PerceptionWindowResult } from "../../api";

type Props = {
  clip: Pick<Clip, "filename" | "perception_run">;
};

function timecode(value: number | undefined): string {
  const seconds = Math.max(0, Number(value || 0));
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
}

function WindowEvidence({ item }: { item: PerceptionWindowResult }) {
  const urls = item.evidence_urls || {};
  const validation = item.validation?.status || "unknown";
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
      <div className="row">
        {urls.window && <a className="buttonlink" href={urls.window} target="_blank" rel="noreferrer">查看視窗 JSON</a>}
        {urls.normalized && <a className="buttonlink" href={urls.normalized} target="_blank" rel="noreferrer">查看標準結果</a>}
      </div>
    </div>
  </details>;
}

export function MultiFrameEvidencePanel({ clip }: Props) {
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
    {results.map((item) => <WindowEvidence key={item.window_uuid} item={item} />)}
    <div className="row">
      <a className="buttonlink" href="#workspace-storyboard">前往分鏡審核修改</a>
      <span className="muted">人工剪點、納入與備註會保留此處的證據對照。</span>
    </div>
  </section>;
}
