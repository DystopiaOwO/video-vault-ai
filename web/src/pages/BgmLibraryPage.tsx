import { useEffect, useMemo, useState } from "react";
import { api, type BgmTrack } from "../api";
import { copyText } from "../utils/clipboard";
import "./bgm-library-page.css";

type LibraryTrack = BgmTrack & {
  license_url?: string;
  attribution_required?: boolean | number;
};

type LicenseFilter = "all" | "attribution" | "no-attribution" | "missing";
type TrackSort = "title" | "artist" | "duration-desc";

function normalized(value: unknown): string {
  return String(value || "").trim().toLocaleLowerCase();
}

export function moodTokens(track: LibraryTrack): string[] {
  return String(track.mood || "")
    .split(/[,/|、，]/)
    .map((value) => value.trim())
    .filter(Boolean);
}

export function requiresAttribution(track: LibraryTrack): boolean {
  return track.attribution_required === true || Number(track.attribution_required) === 1;
}

export function trackCredit(track: LibraryTrack): string {
  if (track.attribution_text?.trim()) return track.attribution_text.trim();
  return [
    `"${track.title}"`,
    track.artist ? `by ${track.artist}` : "",
    track.license_name ? `(${track.license_name})` : "",
    track.source_url || "",
  ].filter(Boolean).join(" ");
}

export function filterBgmTracks(tracks: LibraryTrack[], query: string, mood: string, license: LicenseFilter): LibraryTrack[] {
  const keyword = normalized(query);
  return tracks.filter((track) => {
    const matchesQuery = !keyword || [track.title, track.artist, track.mood, track.license_name, track.attribution_text]
      .some((value) => normalized(value).includes(keyword));
    const matchesMood = !mood || moodTokens(track).some((token) => normalized(token) === normalized(mood));
    const matchesLicense = license === "all"
      || (license === "attribution" && requiresAttribution(track))
      || (license === "no-attribution" && !requiresAttribution(track) && Boolean(track.license_name))
      || (license === "missing" && !track.license_name);
    return matchesQuery && matchesMood && matchesLicense;
  });
}

export function sortBgmTracks(tracks: LibraryTrack[], sort: TrackSort): LibraryTrack[] {
  return [...tracks].sort((left, right) => {
    if (sort === "duration-desc") return Number(right.duration_seconds || 0) - Number(left.duration_seconds || 0);
    if (sort === "artist") return `${left.artist || ""}\u0000${left.title}`.localeCompare(`${right.artist || ""}\u0000${right.title}`, "zh-Hant");
    return left.title.localeCompare(right.title, "zh-Hant");
  });
}

export function formatDuration(value: number | undefined): string {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function BgmLibraryPage() {
  const [tracks, setTracks] = useState<LibraryTrack[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [query, setQuery] = useState("");
  const [mood, setMood] = useState("");
  const [license, setLicense] = useState<LicenseFilter>("all");
  const [sort, setSort] = useState<TrackSort>("title");

  useEffect(() => {
    void loadTracks();
  }, []);

  async function loadTracks() {
    setLoading(true);
    setError("");
    try {
      setTracks(await api.bgm() as LibraryTrack[]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "未知錯誤");
    } finally {
      setLoading(false);
    }
  }

  const moods = useMemo(() => [...new Set(tracks.flatMap(moodTokens))].sort((left, right) => left.localeCompare(right, "zh-Hant")), [tracks]);
  const visibleTracks = useMemo(() => sortBgmTracks(filterBgmTracks(tracks, query, mood, license), sort), [license, mood, query, sort, tracks]);
  const totalDuration = tracks.reduce((sum, track) => sum + Number(track.duration_seconds || 0), 0);
  const attributionCount = tracks.filter(requiresAttribution).length;
  const filtersActive = Boolean(query.trim() || mood || license !== "all" || sort !== "title");

  function clearFilters() {
    setQuery("");
    setMood("");
    setLicense("all");
    setSort("title");
  }

  async function copyCredit(track: LibraryTrack) {
    const copied = await copyText(trackCredit(track));
    setMessage(copied ? `「${track.title}」署名文字已複製。` : "無法自動複製署名文字，請展開後手動複製。");
  }

  return <main className="bgm-library-page">
    <aside>
      <h1>BGM 資料庫</h1>
      <nav className="sidebar-links" aria-label="BGM 導覽">
        <a className="nav" href="/">專案工作台</a>
        <a className="nav" href="/classic-bgm">上傳 BGM</a>
      </nav>
      <div className="bgm-sidebar-summary">
        <div><span>曲目</span><b>{tracks.length}</b></div>
        <div><span>總時長</span><b>{formatDuration(totalDuration)}</b></div>
        <div><span>需署名</span><b>{attributionCount}</b></div>
        <div><span>情緒標籤</span><b>{moods.length}</b></div>
      </div>
      <p className="muted">此頁只顯示資料庫公開資訊；本機音檔路徑不會暴露到瀏覽器。</p>
    </aside>

    <section>
      {message && <div className="notice" role="status"><span>{message}</span><button type="button" aria-label="關閉通知" onClick={() => setMessage("")}>×</button></div>}
      <header className="bgm-library-hero">
        <div>
          <span>BGM LIBRARY</span>
          <h2>音樂與授權資料</h2>
          <p>搜尋曲目、核對授權與複製 YouTube 署名文字。</p>
        </div>
        <div className="bgm-hero-actions">
          <button type="button" disabled={loading} onClick={() => void loadTracks()}>{loading ? "更新中…" : "重新整理"}</button>
          <a className="nav" href="/classic-bgm">新增曲目</a>
        </div>
      </header>

      <section className="bgm-filter-panel" aria-label="BGM 篩選">
        <label className="bgm-search">
          <span>搜尋</span>
          <input type="search" aria-label="搜尋 BGM" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="曲名、作者、情緒、授權或署名" />
        </label>
        <label>
          <span>情緒</span>
          <select aria-label="篩選 BGM 情緒" value={mood} onChange={(event) => setMood(event.target.value)}>
            <option value="">全部情緒</option>
            {moods.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <span>授權</span>
          <select aria-label="篩選 BGM 授權" value={license} onChange={(event) => setLicense(event.target.value as LicenseFilter)}>
            <option value="all">全部授權</option>
            <option value="attribution">需要署名</option>
            <option value="no-attribution">已填授權／不需署名</option>
            <option value="missing">授權待補</option>
          </select>
        </label>
        <label>
          <span>排序</span>
          <select aria-label="排序 BGM" value={sort} onChange={(event) => setSort(event.target.value as TrackSort)}>
            <option value="title">曲名</option>
            <option value="artist">作者</option>
            <option value="duration-desc">時長（長到短）</option>
          </select>
        </label>
        <button type="button" disabled={!filtersActive} onClick={clearFilters}>清除篩選</button>
      </section>

      <div className="bgm-result-heading">
        <b>{loading ? "載入曲目中…" : `${visibleTracks.length} / ${tracks.length} 首曲目`}</b>
        {!loading && visibleTracks.length > 0 && <span>點開「授權與署名」查看完整資訊。</span>}
      </div>

      {error && <div className="workspace-state empty" role="alert">
        <span className="workspace-empty-icon" aria-hidden="true">!</span>
        <div><h2>BGM 載入失敗</h2><p>{error}</p><button type="button" onClick={() => void loadTracks()}>重試</button></div>
      </div>}
      {!error && loading && <div className="workspace-state" role="status"><span className="workspace-spinner" aria-hidden="true" /><div><h2>正在載入 BGM</h2><p>取得曲目、時長與授權資訊…</p></div></div>}
      {!error && !loading && tracks.length === 0 && <div className="workspace-state empty">
        <span className="workspace-empty-icon" aria-hidden="true">♫</span>
        <div><h2>尚無 BGM</h2><p>先登錄本機音樂與授權資訊，再回到專案音訊工作區使用。</p><a className="nav" href="/classic-bgm">新增第一首曲目</a></div>
      </div>}
      {!error && !loading && tracks.length > 0 && visibleTracks.length === 0 && <div className="workspace-state empty">
        <span className="workspace-empty-icon" aria-hidden="true">⌕</span>
        <div><h2>找不到符合的曲目</h2><p>調整搜尋字詞、情緒或授權條件。</p><button type="button" onClick={clearFilters}>清除篩選</button></div>
      </div>}

      {!error && !loading && visibleTracks.length > 0 && <div className="bgm-track-grid">
        {visibleTracks.map((track) => <article className="bgm-track-card" key={track.id}>
          <header>
            <div>
              <span className="bgm-track-index">#{track.id}</span>
              <h3>{track.title}</h3>
              <p>{track.artist || "未知作者"}</p>
            </div>
            <span className="bgm-duration">{formatDuration(track.duration_seconds)}</span>
          </header>
          <div className="bgm-track-badges">
            <span className={requiresAttribution(track) ? "warning" : "ok"}>{requiresAttribution(track) ? "需要署名" : "不需署名"}</span>
            <span>{track.license_name || "授權待補"}</span>
            {moodTokens(track).map((token) => <span key={token}>{token}</span>)}
          </div>
          <details className="bgm-credit-details">
            <summary>授權與署名</summary>
            <dl>
              <div><dt>授權</dt><dd>{track.license_name || "尚未填寫"}</dd></div>
              <div><dt>署名</dt><dd><pre>{trackCredit(track)}</pre></dd></div>
            </dl>
            <div className="bgm-credit-actions">
              <button type="button" onClick={() => void copyCredit(track)}>複製署名</button>
              {track.source_url && <a className="nav" href={track.source_url} target="_blank" rel="noreferrer">來源頁面</a>}
              {track.license_url && <a className="nav" href={track.license_url} target="_blank" rel="noreferrer">授權條款</a>}
            </div>
          </details>
        </article>)}
      </div>}
    </section>
  </main>;
}
