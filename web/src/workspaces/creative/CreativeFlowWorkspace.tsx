import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { ProjectDetail } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";
import type { ProjectMutationControls } from "../../projectMutation";
import { CreativeBriefCheckpoint } from "./CreativeBriefCheckpoint";
import { VisualStylePreviewWorkspace } from "./VisualStylePreviewWorkspace";
import { VisualStyleDraftProvider } from "./VisualStyleDraftController";
import { disclosureSections, resolveDisclosureAction, resolveDisclosureSummary } from "./disclosure";
import "./creative-flow.css";

type Props = {
  detail: ProjectDetail;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<unknown>;
  mutationControls: ProjectMutationControls;
};

type FlowStep = "direction" | "style" | "summary";
type AdvancedRendererProps = Omit<Props, "detail"> & { onPreviewReady?: () => void };

const ADVANCED_RENDERERS: Record<string, (props: AdvancedRendererProps & { detail: ProjectDetail }) => ReactNode> = {
  "creative_brief.framing": ({ detail, ...props }) => <CreativeBriefCheckpoint {...props} detail={detail} compact advancedSection="framing" />,
  "visual_style.grading": ({ detail, ...props }) => <VisualStylePreviewWorkspace {...props} detail={detail} compact advancedSection="grading" />,
  "visual_style.title_style": ({ detail, ...props }) => <VisualStylePreviewWorkspace {...props} detail={detail} compact advancedSection="title" />,
};

function initialStep(detail: ProjectDetail): FlowStep {
  if (detail.visual_style?.status === "approved" && detail.creative_brief?.status === "approved") return "summary";
  if (detail.creative_brief?.status === "approved") return "style";
  return "direction";
}

export function CreativeFlowWorkspace({ detail, setMessage, refreshProject, mutationControls }: Props) {
  const [step, setStep] = useState<FlowStep>(() => initialStep(detail));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [activeAdvancedSection, setActiveAdvancedSection] = useState<string | null>(null);
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

  function openSemanticEditor(sectionId: string, action: ReturnType<typeof resolveDisclosureAction>) {
    if (!action.available || !action.step) return;
    setStep(action.step);
    setAdvancedOpen(true);
    setActiveAdvancedSection(sectionId);
  }

  function renderAdvancedEditor(section: ReturnType<typeof disclosureSections>[number]) {
    const action = resolveDisclosureAction(section);
    const target = action.semantic_editor_target;
    const renderer = target ? ADVANCED_RENDERERS[target] : undefined;
    if (section.enabled === false || !renderer) return <span className="advanced-unavailable">{action.reason || "此區塊目前沒有可用的編輯入口"}</span>;
    return <>
      {renderer({ detail, setMessage, refreshProject, mutationControls, onPreviewReady: () => setStep("style") })}
      {action.available && <button type="button" className="semantic-editor-action" onClick={() => openSemanticEditor(section.section_id, action)}>{action.label}</button>}
    </>;
  }

  return <VisualStyleDraftProvider detail={detail}><section className="creative-flow" aria-label="創意設定流程">
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
      <div className="creative-summary-line" aria-label="已核准設定摘要">
        {disclosureSections(detail).filter((section) => section.include_in_final_summary).sort((left, right) => (left.summary_order || left.order) - (right.summary_order || right.order)).map((section) => {
          const summary = resolveDisclosureSummary(section, detail);
          return <span className={!summary.available ? "summary-unavailable" : ""} key={`${section.section_id}@${section.version}`}><small>{section.label}</small><strong>{summary.text}</strong></span>;
        })}
      </div>
      <div className="creative-summary-actions"><button type="button" onClick={() => setStep("direction")}>返回調整方向</button><button type="button" onClick={() => setStep("style")}>返回預覽風格</button><button type="button" className="primary" onClick={continueToStory}>確認設定，進入故事整理</button></div>
    </section>}

    <details className="creative-flow-advanced" open={advancedOpen}>
      <summary onClick={(event) => { event.preventDefault(); setAdvancedOpen((current) => { if (current) setActiveAdvancedSection(null); return !current; }); }}>詳細設定</summary>
      <div className="creative-flow-advanced-grid">
        {[...advancedSections, ...diagnosticSections].map((section) => {
          const summary = resolveDisclosureSummary(section, detail);
          const action = resolveDisclosureAction(section);
          return <details className="creative-flow-advanced-section" key={`${section.section_id}@${section.version}`} data-section-id={section.section_id} open={activeAdvancedSection === section.section_id}>
            <summary onClick={(event) => { event.preventDefault(); setActiveAdvancedSection((current) => current === section.section_id ? null : section.section_id); }}><span>{section.label}</span><small>{section.enabled === false ? "目前不可用" : summary.text}</small></summary>
            <div className="creative-flow-advanced-section-body"><p className={!summary.available ? "summary-unavailable" : ""}>{summary.text}</p>{activeAdvancedSection === section.section_id && renderAdvancedEditor(section)}</div>
          </details>;
        })}
      </div>
    </details>
  </section></VisualStyleDraftProvider>;
}
