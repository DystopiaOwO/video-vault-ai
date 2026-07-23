import { useEffect, useRef, useState } from "react";
import { api, type Clip, type Job } from "../../api";
import type { ProjectDataLoadOptions } from "../../projectDataLoader";

export type ClipSummaryEditorProps = {
  projectId: number;
  clip: Pick<Clip, "video_id" | "filename" | "visual_summary">;
  setMessage: (value: string) => void;
  refreshProject: (options?: ProjectDataLoadOptions) => Promise<Job[]>;
};

export function ClipSummaryEditor({ projectId, clip, setMessage, refreshProject }: ClipSummaryEditorProps) {
  const incomingSummary = clip.visual_summary || "";
  const [text, setText] = useState(incomingSummary);
  const [baseline, setBaseline] = useState(incomingSummary);
  const [saving, setSaving] = useState(false);
  const identityRef = useRef(`${projectId}:${clip.video_id}`);
  const dirty = text !== baseline;
  const dirtyRef = useRef(dirty);
  const editorId = `clip-summary-${projectId}-${clip.video_id}`;

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    const identity = `${projectId}:${clip.video_id}`;
    const identityChanged = identityRef.current !== identity;
    if (identityChanged) identityRef.current = identity;
    if (!identityChanged && dirtyRef.current) return;

    setText(incomingSummary);
    setBaseline(incomingSummary);
    setSaving(false);
  }, [clip.video_id, incomingSummary, projectId]);

  useEffect(() => {
    if (!dirty) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [dirty]);

  async function save() {
    const summary = text.trim();
    setSaving(true);
    setMessage("正在儲存內容感知描述...");
    try {
      const result = await api.saveClipSummary(projectId, clip.video_id, summary);
      if (!result.ok) {
        setMessage("內容感知描述儲存失敗：找不到素材。");
        return;
      }
      setText(summary);
      setBaseline(summary);
      setMessage("內容感知描述已儲存，專案已回到待審。");
      await refreshProject({ forceFresh: true });
    } catch (error) {
      setMessage(`內容感知描述儲存失敗：${error instanceof Error ? error.message : "未知錯誤"}`);
    } finally {
      setSaving(false);
    }
  }

  return <div className="clip-summary-editor" data-unsaved-text-draft={dirty ? "true" : undefined}>
    <div className="clip-summary-heading">
      <label htmlFor={editorId}>內容感知描述</label>
      {dirty && <span role="status">有未儲存變更</span>}
    </div>
    <textarea
      id={editorId}
      aria-label={`${clip.filename} 內容感知描述`}
      value={text}
      onChange={(event) => setText(event.target.value)}
      placeholder="內容感知描述"
      disabled={saving}
    />
    <div className="clip-summary-actions">
      <small>{text.length} 字</small>
      <button type="button" disabled={!dirty || saving} onClick={() => setText(baseline)}>放棄變更</button>
      <button type="button" className="good" disabled={!dirty || saving} onClick={() => void save()}>{saving ? "儲存中…" : "儲存描述"}</button>
    </div>
  </div>;
}
