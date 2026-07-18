import type { RenderOutput } from "../../api";
export function RenderOutputsPanel({ outputs = [] }: { outputs?: RenderOutput[] }) { return outputs.length ? <div>{outputs.map((item) => <div className="output" key={item.path}><b>{item.label}</b><code>{item.path}</code></div>)}</div> : <p className="muted">尚未產生輸出檔案。</p>; }
