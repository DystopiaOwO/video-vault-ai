import { useEffect, useMemo, useState } from "react";
import type { ProjectDetail } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import { CreativeBriefCheckpoint } from "./CreativeBriefCheckpoint";
import { VisualStylePreviewWorkspace } from "./VisualStylePreviewWorkspace";
import { disclosureSections } from "./disclosure";
import "./creative-flow.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

type FlowStep = "direction" | "style" | "summary";

const ADVANCED_STEP_TARGETS: Record<string, FlowStep> = {
  framing: "direction",
  grading: "style",
  title: "style",
};

function initialStep(detail: ProjectDetail): FlowStep {
  if (detail.visual_style?.status === "approved" && detail.creative_brief?.status === "approved") return "summary";
  if (detail.creative_brief?.status === "approved") return "style";
  return "direction";
}

function humanStrategy(value: unknown) {
  const strategy = String(value || "");
  return {
    crop_reframe: "裁切填滿",
    background_treatment: "模糊背景補齊",
    preserve_full_frame: "保留完整畫面",
    auto_recommended: "依素材自動處理",
  }[strategy] || "依素材自動處理";
}

function summaryFor(sectionId: string, detail: ProjectDetail): string {
  const brief = detail.creative_brief || {};
  const output = brief.approved?.output || brief.recommendation?.output || {};
  const style = detail.visual_style?.approved || detail.visual_style?.recommendation || {};
  const framing = brief.approved?.framing_intent || brief.recommendation?.framing_intent || {};
  if (sectionId === "framing") {
    const strategies = Object.values(framing).map((item) => humanStrategy(item?.approved_strategy_id || item?.approved_strategy || item?.recommended_strategy_id || item?.recommended_strategy));
    return strategies.length ? strategies.join("；") : "依素材自動處理畫面";
  }
  if (sectionId === "grading") return String((style.grading as Record<string, unknown> | undefined)?.look_id || "跟隨所選風格");
  if (sectionId === "title") return `${String(style.title_style_id || "預設字卡")} · ${String(style.anchor || "安全區內")}`;
  if (sectionId === "captions") return "字幕樣式可於字幕功能中調整";
  if (sectionId === "technical") return `方向 ${String(output.aspect_ratio || "—")} · 語意 contract 已保留`;
  return "尚未設定";
}

function styleLabel(detail: ProjectDetail) {
  const style = detail.visual_style?.approved || detail.visual_style?.recommendation || {};
  const id = String(style.visual_style_id || "diary_natural");
  return ({ diary_natural: "Diary Natural", clean_minimal: "Clean Minimal", cinematic: "Cinematic" } as Record<string, string>)[id] || id;
}

export function CreativeFlowWorkspace({ detail, setMessage, refreshProject, mutationControls }: Props) {
  const [step, setStep] = useState<FlowStep>(() => initialStep(detail));
  const briefApproved = detail.creative_brief?.status === "approved";
  const styleApproved = detail.visual_style?.status === "approved";
  const advancedSections = useMemo(() => disclosureSections(detail, "advanced"), [detail.editor_disclosure]);
  const diagnosticSections = useMemo(() => disclosureSections(detail, "diagnostic"), [detail.editor_disclosure]);

  useEffect(() => {
    if (styleApproved && briefApproved) setStep((current) => current === "direction" || current === "style" ? "summary" : current);
    else if (briefApproved) setStep((current) => current === "direction" ? "style" : current);
  }, [briefApproved, styleApproved]);

  function continueToStory() {
    document.querySelector('[aria-label="專案故事理解"]')?.scrollIntoView({ behavior: "smooth", block: "start" });
    setMessage("Creative Brief 與 Visual Style 已核准；接下來可進入故事整理。正式 approval gate 仍由既有流程控制。");
  }

  return <section className="creative-flow" aria-label="創意設定流程">
    <header className="creative-flow-header">
      <div><span className="eyebrow">創意流程</span><h2>先看結果，再調細節</h2><p>AI 先提出建議，確認真實預覽後再進入故事整理。</p></div>
      <div className="creative-flow-progress" aria-label="創意流程進度">
        {(["direction", "style", "summary"] as FlowStep[]).map((item, index) => <button type="button" key={item} className={step === item ? "active" : ""} disabled={item === "style" && !briefApproved || item === "summary" && !styleApproved} onClick={() => setStep(item)}><span>{index + 1}</span>{item === "direction" ? "方向" : item === "style" ? "風格" : "確認"}</button>)}
      </div>
    </header>

    {step === "direction" && <CreativeBriefCheckpoint detail={detail} setMessage={setMessage} refreshProject={refreshProject} mutationControls={mutationControls} compact onApproved={() => setStep("style")} />}
    {step === "style" && <VisualStylePreviewWorkspace detail={detail} setMessage={setMessage} refreshProject={refreshProject} mutationControls={mutationControls} compact onApproved={() => setStep("summary")} />}
    {step === "summary" && <section className="creative-summary" aria-label="創意設定確認">
      <div className="creative-summary-heading"><div><span className="step-kicker">步驟 3／3</span><h3>確認目前的創意設定</h3><p>這裡只顯示已核准的語意值；展開詳細設定不會另外建立一份設定。</p></div><span className="brief-status approved">已準備</span></div>
      <div className="creative-summary-line"><strong>{String((detail.creative_brief?.approved?.output || detail.creative_brief?.recommendation?.output)?.aspect_ratio || "—")}</strong><span>·</span><strong>{styleLabel(detail)}</strong><span>·</span><span>{summaryFor("framing", detail)}</span><span>·</span><span>疊在影片上</span></div>
      <div className="creative-summary-actions"><button type="button" onClick={() => setStep("direction")}>返回調整方向</button><button type="button" onClick={() => setStep("style")}>返回預覽風格</button><button type="button" className="primary" onClick={continueToStory}>確認設定，進入故事整理</button></div>
    </section>}

    <details className="creative-flow-advanced">
      <summary>詳細設定與更多資訊</summary>
      <div className="creative-flow-advanced-grid">
        {advancedSections.map((section) => <article key={`${section.section_id}@${section.version}`} data-section-id={section.section_id}><div><h4>{section.label}</h4><p>{summaryFor(section.section_id, detail)}</p></div>{ADVANCED_STEP_TARGETS[section.section_id] ? <button type="button" onClick={() => setStep(ADVANCED_STEP_TARGETS[section.section_id])}>微調</button> : <span>摘要</span>}</article>)}
      </div>
      {diagnosticSections.length > 0 && <details className="creative-flow-diagnostic"><summary>技術資訊</summary>{diagnosticSections.map((section) => <p key={section.section_id}>{section.label}：{summaryFor(section.section_id, detail)} · {detail.editor_disclosure?.registry_version || ""}</p>)}</details>}
    </details>
  </section>;
}
