type Props = { onAction: (action: string) => void; busy?: boolean; canRender: boolean; gateReason?: string };

export function RenderActions({ onAction, busy = false, canRender, gateReason }: Props) {
  const gated = !canRender;
  return <div className="actions">
    <button disabled={busy} onClick={() => onAction("project")}>產生 HyperFrames 專案</button>
    <button disabled={busy} onClick={() => onAction("rough")}>粗略排序預覽</button>
    <small>剪點可能不精準，僅供確認排序</small>
    <button disabled={busy || gated} onClick={() => onAction("accurate")}>精準預覽</button>
    <button className="primary" disabled={busy || gated} onClick={() => onAction("final")}>正式輸出</button>
    <button disabled={busy} onClick={() => onAction("opencut")}>OpenCut 素材包</button>
    <button disabled={busy || gated} onClick={() => onAction("opencut-render")}>OpenCut 調色片段</button>
    {gated && <div className="gate-reason" role="status"><b>正式輸出已鎖定</b><span>{gateReason ?? "請先完成專案核准。"}</span></div>}
    {busy && <div className="action-progress" role="status"><span className="spinner" /> 正在建立工作...</div>}
  </div>;
}
