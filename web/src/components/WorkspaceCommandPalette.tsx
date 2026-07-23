import { useEffect, useMemo, useRef, useState } from "react";
import { focusProjectSearch, navigateToWorkspace } from "../workspaceNavigationEnhancements";
import "./workspace-command-palette.css";

export type CommandAction = {
  id: string;
  label: string;
  detail: string;
  keywords: string;
  run: () => void;
};

export function buildWorkspaceCommands(close: () => void): CommandAction[] {
  const action = (id: string, label: string, detail: string, keywords: string, run: () => void): CommandAction => ({
    id,
    label,
    detail,
    keywords,
    run: () => {
      close();
      window.requestAnimationFrame(run);
    },
  });

  return [
    action("project-search", "搜尋專案", "將游標移到左側專案搜尋", "project search 專案 查找", () => focusProjectSearch()),
    action("overview", "前往總覽", "Render Job、進度與工作流", "overview jobs status 總覽 進度", () => navigateToWorkspace(0)),
    action("storyboard", "前往分鏡", "片段排序、剪點與 Inspector", "storyboard timing inspector 分鏡 剪點", () => navigateToWorkspace(1)),
    action("review", "前往審核與輸出", "核准、退回與正式輸出", "review render export 審核 輸出", () => navigateToWorkspace(2)),
    action("media", "前往素材與調色", "素材感知、描述與色彩一致性", "media clip color 素材 調色 感知", () => navigateToWorkspace(3)),
    action("audio", "前往音訊", "BGM、原音與片段例外", "audio bgm sound 音訊 混音", () => navigateToWorkspace(4)),
    action("script", "前往故事整理", "檢視專案敘事與腳本", "story script 故事 腳本", () => navigateToWorkspace(5)),
    action("bgm", "開啟 BGM 資料庫", "瀏覽本地音樂與授權資訊", "music library bgm 音樂 資料庫", () => window.location.assign("/bgm")),
  ];
}

export function WorkspaceCommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const commands = useMemo(() => buildWorkspaceCommands(() => setOpen(false)), []);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return commands;
    return commands.filter((command) => `${command.label} ${command.detail} ${command.keywords}`.toLocaleLowerCase().includes(normalized));
  }, [commands, query]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        setOpen((current) => !current);
        return;
      }
      if (!open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => filtered.length ? (index + 1) % filtered.length : 0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => filtered.length ? (index - 1 + filtered.length) % filtered.length : 0);
      } else if (event.key === "Enter" && filtered[activeIndex]) {
        event.preventDefault();
        filtered[activeIndex].run();
      } else if (event.key === "Tab") {
        const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('input, button:not([disabled]), [tabindex]:not([tabindex="-1"])') || []);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeIndex, filtered, open]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    setQuery("");
    setActiveIndex(0);
    window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => {
      document.body.style.overflow = previousOverflow;
      previousFocusRef.current?.focus();
    };
  }, [open]);

  useEffect(() => setActiveIndex(0), [query]);

  return <>
    <button
      className="command-palette-trigger"
      type="button"
      onClick={() => setOpen(true)}
      aria-label="開啟命令面板"
      aria-keyshortcuts="Control+K Meta+K"
      title="快速導覽（Ctrl / ⌘ + K）"
    >
      <span>快速導覽</span><kbd>Ctrl K</kbd>
    </button>
    {open && <div className="command-palette-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) setOpen(false);
    }}>
      <section ref={dialogRef} className="command-palette" role="dialog" aria-modal="true" aria-label="工作區命令面板">
        <header>
          <span aria-hidden="true">⌕</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜尋工作區或功能…"
            aria-label="搜尋命令"
            role="combobox"
            aria-controls="workspace-command-results"
            aria-expanded="true"
            aria-autocomplete="list"
          />
          <kbd>Esc</kbd>
        </header>
        <div id="workspace-command-results" className="command-palette-results" role="listbox" aria-label="命令結果">
          {filtered.map((command, index) => <button
            key={command.id}
            type="button"
            role="option"
            aria-selected={index === activeIndex}
            className={index === activeIndex ? "active" : ""}
            onMouseEnter={() => setActiveIndex(index)}
            onClick={command.run}
          >
            <span><b>{command.label}</b><small>{command.detail}</small></span>
            <span aria-hidden="true">↵</span>
          </button>)}
          {!filtered.length && <div className="command-palette-empty">找不到符合的功能</div>}
        </div>
        <footer><span>↑↓ 選擇</span><span>Enter 執行</span><span>Esc 關閉</span></footer>
      </section>
    </div>}
  </>;
}
