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

export function CreativeBriefCheckpoint({ detail, setMessage, refreshProject, mutationControls }: Props) {
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
      <div className="creative-brief-recommendation-heading"><span className="creative-brief-label">AI 建議</span><strong>{String(recommendation.output?.aspect_ratio || "尚未解析")}</strong></div>
      <span>{recommendation.reason || "尚無建議理由"}</span>
    </div>
    <div className="creative-brief-controls">
      <fieldset className="creative-brief-output-selector"><legend>最終影片方向</legend>
        <div className="creative-brief-option-grid">
          {outputOptions.map((option) => <label className={`creative-brief-option ${option.orientation}`} key={option.output_contract_id}>
            <input type="radio" name={`brief-output-${detail.project.id}`} checked={outputContractId === option.output_contract_id} disabled={Boolean(busy)} onChange={() => setOutputContractId(String(option.output_contract_id))} />
            <span className="creative-brief-option-mark" aria-hidden="true" />
            <span className="creative-brief-option-copy"><strong>{option.label || option.aspect_ratio}</strong><small>{option.width} × {option.height}</small></span>
          </label>)}
        </div>
      </fieldset>
      <div className="creative-brief-output"><span className="creative-brief-label">目前選擇</span><strong>{selectedOutput.width} × {selectedOutput.height}</strong><span className="creative-brief-output-aspect">{selectedOutput.aspect_ratio || "未知比例"}</span><code>{selectedOutput.render_profile_id || "unknown-profile"}</code></div>
      {directions.map((direction) => <label className="creative-brief-direction" key={direction.direction_id}><span className="creative-brief-direction-title">{direction.label}</span><span className="creative-brief-direction-hint">建議先裁切／重新構圖，不適合時再考慮背景處理</span><select value={strategies[direction.direction_id] || ""} disabled={Boolean(busy)} onChange={(event) => setStrategies((current) => ({ ...current, [direction.direction_id]: event.target.value }))}>{strategiesFor(direction.direction_id).map((strategy) => <option key={strategy.strategy_id} value={strategy.strategy_id}>{strategy.label}</option>)}</select></label>)}
    </div>
    <div className="creative-brief-actions">
      <button type="button" className="primary" disabled={Boolean(busy) || !recommendedOutputId} onClick={() => { const nextStrategies = initialFraming(directions, {}, recommendation); setOutputContractId(recommendedOutputId); setStrategies(nextStrategies); void save("recommendation", { outputContractId: recommendedOutputId, strategies: nextStrategies }); }}>{busy === "save" ? "儲存中…" : "採用 AI 建議並核准"}</button>
      <button type="button" disabled={Boolean(busy) || !outputContractId} onClick={() => void save("human_override")}>儲存並核准手動覆寫</button>
    </div>
    {approvedState && <p className="creative-brief-approved" role="status">已核准值會由 Render / VID-27 讀取；visual-only 變更不會讓既有 StoryInput 無謂 stale。</p>}
  </section>;
}
