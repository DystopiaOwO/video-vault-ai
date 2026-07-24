const WORKSPACE_SECTION_IDS = [
  "workspace-overview",
  "workspace-storyboard",
  "workspace-review",
  "workspace-media",
  "workspace-audio",
  "workspace-script",
] as const;

type WorkspaceSectionId = typeof WORKSPACE_SECTION_IDS[number];

type NavigationWindow = Window & {
  __videoVaultWorkspaceNavigationCleanup?: () => void;
};

function prefersReducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

function projectSearchInput(): HTMLInputElement | null {
  return document.querySelector<HTMLInputElement>('input[aria-label="搜尋專案"]');
}

function workspaceSection(id: WorkspaceSectionId): HTMLElement | null {
  return document.getElementById(id);
}

function workspaceLink(id: WorkspaceSectionId): HTMLAnchorElement | null {
  return document.querySelector<HTMLAnchorElement>(`.workspace-nav a[href="#${id}"]`);
}

export function focusProjectSearch(): boolean {
  const input = projectSearchInput();
  if (!input) return false;
  input.focus();
  input.select();
  return true;
}

export function navigateToWorkspace(index: number): boolean {
  const id = WORKSPACE_SECTION_IDS[index];
  if (!id) return false;
  const section = workspaceSection(id);
  if (!section) return false;

  history.replaceState(null, "", `#${id}`);
  section.scrollIntoView({
    behavior: prefersReducedMotion() ? "auto" : "smooth",
    block: "start",
  });
  section.focus({ preventScroll: true });
  setCurrentWorkspace(id);
  return true;
}

function setCurrentWorkspace(id: WorkspaceSectionId) {
  for (const sectionId of WORKSPACE_SECTION_IDS) {
    const link = workspaceLink(sectionId);
    if (!link) continue;
    if (sectionId === id) link.setAttribute("aria-current", "location");
    else link.removeAttribute("aria-current");
  }
}

function decorateShortcuts() {
  const search = projectSearchInput();
  search?.removeAttribute("aria-keyshortcuts");
  search?.setAttribute("title", "搜尋專案");

  WORKSPACE_SECTION_IDS.forEach((id, index) => {
    const link = workspaceLink(id);
    link?.setAttribute("aria-keyshortcuts", `Alt+${index + 1}`);
    if (link) link.title = `${link.textContent?.trim() || "工作區"}（Alt+${index + 1}）`;
  });
}

function editableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
}

export function installWorkspaceNavigationEnhancements(): () => void {
  let observedSections: HTMLElement[] = [];
  let intersectionObserver: IntersectionObserver | null = null;
  let refreshQueued = false;
  let disposed = false;
  const visibility = new Map<WorkspaceSectionId, number>();

  const refresh = () => {
    refreshQueued = false;
    if (disposed) return;
    decorateShortcuts();
    const sections = WORKSPACE_SECTION_IDS
      .map((id) => workspaceSection(id))
      .filter((section): section is HTMLElement => Boolean(section));

    if (sections.length === observedSections.length && sections.every((section, index) => section === observedSections[index])) return;

    intersectionObserver?.disconnect();
    visibility.clear();
    observedSections = sections;
    if (!sections.length) return;

    if (typeof IntersectionObserver === "undefined") {
      const hashId = window.location.hash.slice(1) as WorkspaceSectionId;
      setCurrentWorkspace(WORKSPACE_SECTION_IDS.includes(hashId) ? hashId : WORKSPACE_SECTION_IDS[0]);
      return;
    }

    intersectionObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const id = entry.target.id as WorkspaceSectionId;
        if (!WORKSPACE_SECTION_IDS.includes(id)) return;
        visibility.set(id, entry.isIntersecting ? entry.intersectionRatio : 0);
      });
      const active = [...visibility.entries()].sort((left, right) => right[1] - left[1])[0];
      if (active?.[1] > 0) setCurrentWorkspace(active[0]);
    }, {
      rootMargin: "-18% 0px -62% 0px",
      threshold: [0, .15, .35, .6],
    });
    sections.forEach((section) => intersectionObserver?.observe(section));
  };

  const queueRefresh = () => {
    if (refreshQueued || disposed) return;
    refreshQueued = true;
    queueMicrotask(refresh);
  };

  const handleKeyDown = (event: KeyboardEvent) => {
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || editableTarget(event.target)) return;
    const index = Number(event.key) - 1;
    if (Number.isInteger(index) && navigateToWorkspace(index)) event.preventDefault();
  };

  const handleHashChange = () => {
    const id = window.location.hash.slice(1) as WorkspaceSectionId;
    if (WORKSPACE_SECTION_IDS.includes(id)) setCurrentWorkspace(id);
  };

  const mutationObserver = new MutationObserver(queueRefresh);
  mutationObserver.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("keydown", handleKeyDown);
  window.addEventListener("hashchange", handleHashChange);
  refresh();

  return () => {
    disposed = true;
    mutationObserver.disconnect();
    intersectionObserver?.disconnect();
    document.removeEventListener("keydown", handleKeyDown);
    window.removeEventListener("hashchange", handleHashChange);
  };
}

export function registerWorkspaceNavigationEnhancements() {
  const navigationWindow = window as NavigationWindow;
  navigationWindow.__videoVaultWorkspaceNavigationCleanup?.();
  navigationWindow.__videoVaultWorkspaceNavigationCleanup = installWorkspaceNavigationEnhancements();
}
