import { useEffect, useMemo, useState } from "react";
import { api, type CreativeBrief, type CreativeBriefFraming, type ProjectDetail } from "../../api";
import { formatApiError } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import "./creative-brief.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
  compact?: boolean;
  onApproved?: () => void;
};

type BriefOptions = NonNullable<CreativeBrief["options"]>;
type OutputOption = NonNullable<BriefOptions["output_contracts"]>[number];
type DirectionOption = NonNullable<BriefOptions["mismatch_directions"]>[number];

function strategyId(framing: CreativeBriefFraming | undefined, fallback = "auto_recommended") {
  return String(framing?.approved_strategy_id || framing?.approved_strategy || framing?.strategy_id || framing?.recommended_strategy_id || framing?.recommended_strategy || fallback);
}

function outputId(recommendation: CreativeBrief["recommendation"], options: OutputOption[]) {
  const candidate = recommendation?.output;
  return String(candidate?.output_contract_id || options.find((item) => item.orientation === candidate?.orientation)?.output_contract_id || options[0]?.output_contract_id || "");
}

function materializedOutput(options: OutputOption[], id: string, fallback?: NonNullable<CreativeBrief["recommendation"]>["output"]): OutputOption {
  return options.find((item) => item.output_contract_id === id) || fallback || options[0] || {};
}

function initialFraming(directions: DirectionOption[], approved: CreativeBrief["approved"], recommendation: CreativeBrief["recommendation"]) {
  return Object.fromEntries(directions.map((direction) => {
    const approvedItem = approved?.framing_intent?.[direction.direction_id as keyof NonNullable<CreativeBrief["approved"]>["framing_intent"]] as CreativeBriefFraming | undefined;
    const recommendationItem = recommendation?.framing_intent?.[direction.direction_id as keyof NonNullable<CreativeBrief["recommendation"]>["framing_intent"]] as CreativeBriefFraming | undefined;
    return [direction.direction_id, strategyId(approvedItem || recommendationItem)];
  }));
}

function framingPayload(directions: DirectionOption[], strategies: Record<string, string>, options: BriefOptions) {
  return Object.fromEntries(directions.map((direction) => {
    const selected = strategies[direction.direction_id] || "auto_recommended";
    const version = options.framing_strategies?.find((item) => item.strategy_id === selected)?.version || "";
    return [direction.direction_id, { approved_strategy_id: selected, approved_strategy_version: version }];
  }));
}

export function CreativeBriefCheckpoint({ detail, setMessage, refreshProject, mutationControls, compact = false, onApproved }: Props) {
  const brief = detail.creative_brief || {};
  const recommendation = brief.recommendation || {};
  const approved = brief.approved || {};
  const options = brief.options || {};
  const outputOptions = (options.output_contracts || []).filter((item) => item.enabled_for_round1_ui !== false);
  const directions = options.mismatch_directions || [];
  const recommendedOutputId = outputId(recommendation, outputOptions);
  const [outputContractId, setOutputContractId] = useState(String(approved.output?.output_contract_id || recommendedOutputId));
  const [strategies, setStrategies] = useState<Record<string, string>>(() => initialFraming(directions, approved, recommendation));
  const [busy, setBusy] = useState("");
  const selectedOutput = useMemo(() => materializedOutput(outputOptions, outputContractId, approved.output || recommendation.output), [approved.output, outputContractId, outputOptions, recommendation.output]);
  const counts = recommendation.source_orientation_summary || brief.source_geometry?.orientation_counts || {};
  const approvedState = brief.status === "approved";

  useEffect(() => {
    setOutputContractId(String(approved.output?.output_contract_id || recommendedOutputId));
    setStrategies(initialFraming(directions, approved, recommendation));
  }, [brief.brief_version, brief.status, options.registry_hash]);

  async function refreshRecommendation() {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    setBusy("recommend");
    try {
      const result = await api.recommendCreativeBrief(detail.project.id, detail.project_revision);
      if (!result.ok || !result.creative_brief) throw new Error(result.error || "素材方向分析失敗");
      await refreshProject({ forceFresh: true });
      setMessage("已重新整理素材 display geometry 與 Creative Brief 建議；尚未核准。");
    } catch (error) {
      setMessage(`Creative Brief 建議失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  async function save(approvalSource: "recommendation" | "human_override", values?: { outputContractId: string; strategies: Record<string, string> }) {
    const mutation = mutationControls.beginProjectMutation(detail.project.id, "story");
    if (!mutation) return;
    const nextOutputId = values?.outputContractId || outputContractId;
    const nextOutput = materializedOutput(outputOptions, nextOutputId, recommendation.output);
    if (!nextOutput.output_contract_id) {
      setMessage("Creative Brief registry 尚未提供可核准的 output contract。");
      mutationControls.finishProjectMutation(mutation);
      return;
    }
    setBusy("save");
    try {
      const nextStrategies = values?.strategies || strategies;
      const result = await api.saveCreativeBrief(detail.project.id, {
        output: nextOutput,
        framing_intent: framingPayload(directions, nextStrategies, options),
      }, approvalSource, detail.project_revision);
      if (!result.ok || !result.creative_brief) throw new Error(result.error || "Creative Brief 儲存失敗");
      await refreshProject({ forceFresh: true });
      setMessage(approvalSource === "recommendation" ? "已採用 AI 建議並核准 Creative Brief。" : "已儲存手動覆寫並核准 Creative Brief。");
      onApproved?.();
    } catch (error) {
      setMessage(`Creative Brief 儲存失敗：${formatApiError(error)}`);
    } finally {
      mutationControls.finishProjectMutation(mutation);
      setBusy("");
    }
  }

  function strategiesFor(directionId: string) {
    return (options.framing_strategies || []).filter((item) => item.supported_direction_ids.includes(directionId));
  }

  function framingSummary() {
    const selected = directions.map((direction) => {
      const strategy = strategies[direction.direction_id] || "auto_recommended";
      return strategiesFor(direction.direction_id).find((item) => item.strategy_id === strategy)?.label || "自動建議";
    }).filter(Boolean);
    return selected.length ? selected.join("；") : "依素材方向自動處理畫面";
  }

  if (compact) {
    return <section className="creative-brief creative-brief-simple" aria-label="Creative Brief checkpoint">
      <div className="creative-brief-simple-heading">
        <div><span className="step-kicker">步驟 1／3</span><h3>先決定影片方向</h3><p>AI 先根據素材比例提出建議，你可以直接採用或改選另一個方向。</p></div>
        <span className={approvedState ? "brief-status approved" : "brief-status"}>{approvedState ? "已核准" : "待人工確認"}</span>
      </div>
      <div className="creative-brief-recommendation creative-brief-recommendation-simple">
        <span className="recommendation-label">AI 建議</span>
        <strong>{String(recommendation.output?.aspect_ratio || "尚未解析")} · {String(recommendation.output?.width || "—")}×{String(recommendation.output?.height || "—")}</strong>
        <span>{recommendation.reason || "尚無建議理由"}</span>
        <small>素材：直向 {counts.portrait ?? 0} · 橫向 {counts.landscape ?? 0} · 方形／未知 {(counts.square ?? 0) + (counts.unknown ?? 0)}</small>
      </div>
      <div className="creative-brief-simple-options" aria-label="影片方向選擇">
        {outputOptions.map((option) => <label key={option.output_contract_id} className={outputContractId === option.output_contract_id ? "selected" : ""}>
          <input type="radio" name={`brief-simple-output-${detail.project.id}`} checked={outputContractId === option.output_contract_id} disabled={Boolean(busy)} onChange={() => setOutputContractId(String(option.output_contract_id))} />
          <span className="orientation-icon" aria-hidden="true"><span /></span>
          <span><strong>{option.label || option.aspect_ratio}</strong><small>{option.width}×{option.height}</small></span>
          {String(option.output_contract_id) === recommendedOutputId && <em>AI 推薦</em>}
        </label>)}
      </div>
      <p className="creative-brief-simple-framing"><strong>畫面處理：</strong>{framingSummary()}</p>
      <details className="creative-brief-advanced">
        <summary>詳細設定</summary>
        <div className="creative-brief-advanced-body">
          <button type="button" disabled={Boolean(busy)} onClick={() => void refreshRecommendation()}>{busy === "recommend" ? "分析中…" : "重新分析素材方向"}</button>
          <div className="creative-brief-controls">
            <div className="creative-brief-output"><span>目前選擇</span><strong>{selectedOutput.width}×{selectedOutput.height}</strong><code>{selectedOutput.render_profile_id || "unknown-profile"}</code></div>
            {directions.map((direction) => <label key={direction.direction_id}>{direction.label}<select value={strategies[direction.direction_id] || ""} disabled={Boolean(busy)} onChange={(event) => setStrategies((current) => ({ ...current, [direction.direction_id]: event.target.value }))}>{strategiesFor(direction.direction_id).map((strategy) => <option key={strategy.strategy_id} value={strategy.strategy_id}>{strategy.label}</option>)}</select></label>)}
          </div>
        </div>
      </details>
      <div className="creative-brief-simple-actions">
        <button type="button" className="primary" disabled={Boolean(busy) || !recommendedOutputId} onClick={() => { const nextStrategies = initialFraming(directions, {}, recommendation); setOutputContractId(recommendedOutputId); setStrategies(nextStrategies); void save("recommendation", { outputContractId: recommendedOutputId, strategies: nextStrategies }); }}>{busy === "save" ? "儲存中…" : "採用推薦方向"}</button>
        <button type="button" disabled={Boolean(busy) || !outputContractId || outputContractId === recommendedOutputId} onClick={() => void save("human_override")}>{busy === "save" ? "儲存中…" : "使用此方向並繼續"}</button>
      </div>
      {approvedState && <p className="creative-brief-approved" role="status">方向已核准，可以進入下一步視覺風格。</p>}
    </section>;
  }

  return <section className="creative-brief card" aria-label="Creative Brief checkpoint">
    <div className="creative-brief-heading">
      <div><span className="eyebrow">CREATIVE BRIEF CHECKPOINT</span><h3>先確認最終影片方向</h3><p>這是 Story generation 前的 project-level visual contract；實際 smart crop、背景處理與預覽屬於 VID-27。</p></div>
      <span className={approvedState ? "brief-status approved" : "brief-status"}>{approvedState ? "已核准" : "待人工確認"}</span>
    </div>
    <div className="creative-brief-summary" aria-label="素材方向摘要">
      <span>直向素材：{counts.portrait ?? 0}</span><span>橫向素材：{counts.landscape ?? 0}</span><span>方形／未知：{(counts.square ?? 0) + (counts.unknown ?? 0)}</span>
      <button type="button" disabled={Boolean(busy)} onClick={() => void refreshRecommendation()}>{busy === "recommend" ? "分析中…" : "重新分析素材方向"}</button>
    </div>
    <div className="creative-brief-recommendation">
      <strong>AI 建議：{String(recommendation.output?.aspect_ratio || "尚未解析")}</strong>
      <span>{recommendation.reason || "尚無建議理由"}</span>
    </div>
    <div className="creative-brief-controls">
      <fieldset><legend>最終影片方向</legend>
        {outputOptions.map((option) => <label key={option.output_contract_id}><input type="radio" name={`brief-output-${detail.project.id}`} checked={outputContractId === option.output_contract_id} disabled={Boolean(busy)} onChange={() => setOutputContractId(String(option.output_contract_id))} />{option.label || option.aspect_ratio}（{option.width}×{option.height}）</label>)}
      </fieldset>
      <div className="creative-brief-output"><span>目前選擇</span><strong>{selectedOutput.width}×{selectedOutput.height}</strong><code>{selectedOutput.render_profile_id || "unknown-profile"}</code></div>
      {directions.map((direction) => <label key={direction.direction_id}>{direction.label}<select value={strategies[direction.direction_id] || ""} disabled={Boolean(busy)} onChange={(event) => setStrategies((current) => ({ ...current, [direction.direction_id]: event.target.value }))}>{strategiesFor(direction.direction_id).map((strategy) => <option key={strategy.strategy_id} value={strategy.strategy_id}>{strategy.label}</option>)}</select></label>)}
    </div>
    <div className="creative-brief-actions">
      <button type="button" className="primary" disabled={Boolean(busy) || !recommendedOutputId} onClick={() => { const nextStrategies = initialFraming(directions, {}, recommendation); setOutputContractId(recommendedOutputId); setStrategies(nextStrategies); void save("recommendation", { outputContractId: recommendedOutputId, strategies: nextStrategies }); }}>{busy === "save" ? "儲存中…" : "採用 AI 建議並核准"}</button>
      <button type="button" disabled={Boolean(busy) || !outputContractId} onClick={() => void save("human_override")}>儲存並核准手動覆寫</button>
    </div>
    {approvedState && <p className="creative-brief-approved" role="status">已核准值會由 Render / VID-27 讀取；visual-only 變更不會讓既有 StoryInput 無謂 stale。</p>}
  </section>;
}
