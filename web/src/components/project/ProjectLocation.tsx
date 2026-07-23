import { useState } from "react";
import { copyText } from "../../utils/clipboard";

export type ProjectLocationProps = {
  projectId: number;
  folder: string;
  setMessage: (value: string) => void;
};

export function ProjectLocation({ projectId, folder, setMessage }: ProjectLocationProps) {
  const [expanded, setExpanded] = useState(false);
  const hasFolder = Boolean(folder.trim());

  async function copyFolder() {
    const copied = await copyText(folder);
    setMessage(copied ? "專案資料夾路徑已複製。" : "無法自動複製路徑，請展開後手動複製。");
    if (!copied) setExpanded(true);
  }

  return <div className="project-location">
    <span>專案 #{projectId}</span>
    {hasFolder && <>
      <button type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? "隱藏資料夾" : "顯示資料夾"}</button>
      <button type="button" onClick={() => void copyFolder()}>複製路徑</button>
    </>}
    {expanded && hasFolder && <code title={folder}>{folder}</code>}
  </div>;
}
