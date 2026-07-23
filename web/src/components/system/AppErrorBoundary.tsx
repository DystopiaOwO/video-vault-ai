import { Component, type ErrorInfo, type ReactNode } from "react";
import { copyText } from "../../utils/clipboard";
import "./app-error-boundary.css";

type Props = {
  children: ReactNode;
  pathname?: string;
};

type State = {
  error: Error | null;
  copied: boolean;
};

export function diagnosticText(error: Error, pathname = window.location.pathname): string {
  return [
    "Video Vault AI UI error",
    `Path: ${pathname || "/"}`,
    `Message: ${error.message || "Unknown error"}`,
    error.name ? `Type: ${error.name}` : "",
  ].filter(Boolean).join("\n");
}

export class AppErrorBoundary extends Component<Props, State> {
  state: State = { error: null, copied: false };

  static getDerivedStateFromError(error: Error): State {
    return { error, copied: false };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Video Vault AI workspace crashed", error, info.componentStack);
  }

  reset = () => {
    this.setState({ error: null, copied: false });
  };

  reload = () => {
    window.location.reload();
  };

  copyDiagnostic = async () => {
    const { error } = this.state;
    if (!error) return;
    const copied = await copyText(diagnosticText(error, this.props.pathname));
    this.setState({ copied });
  };

  render() {
    const { error, copied } = this.state;
    if (!error) return this.props.children;

    return <main className="app-error-shell">
      <section className="app-error-panel" role="alert" aria-live="assertive">
        <span className="app-error-kicker">WORKSPACE RECOVERY</span>
        <h1>工作台發生錯誤</h1>
        <p>目前頁面沒有繼續執行，已保留原始素材與後端工作。先重新嘗試；仍失敗時再重新載入介面。</p>
        <div className="app-error-actions">
          <button type="button" className="good" onClick={this.reset}>重新嘗試</button>
          <button type="button" onClick={this.reload}>重新載入介面</button>
          <a className="nav" href="/">返回專案工作台</a>
        </div>
        <details className="app-error-details">
          <summary>技術資訊</summary>
          <dl>
            <div><dt>頁面</dt><dd><code>{this.props.pathname ?? window.location.pathname}</code></dd></div>
            <div><dt>錯誤</dt><dd><code>{error.message || "Unknown error"}</code></dd></div>
          </dl>
          <button type="button" onClick={() => void this.copyDiagnostic()}>{copied ? "已複製診斷資訊" : "複製診斷資訊"}</button>
        </details>
      </section>
    </main>;
  }
}
