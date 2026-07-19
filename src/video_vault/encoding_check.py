"""Repository-scoped UTF-8 and mojibake checks."""

from __future__ import annotations

from pathlib import Path


BAD_TOKENS = tuple(chr(code) for code in (0xFFFD, 0x929D, 0x5697, 0x875D, 0x64A0, 0x6470, 0x61BF, 0x8758, 0xEAF1, 0xEBEF, 0xF387, 0xF699))
TEXT_EXTENSIONS = {".css", ".html", ".md", ".ps1", ".py", ".toml", ".ts", ".tsx", ".yaml", ".yml"}
SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", "cache", "node_modules", "dist", "renders", "output", "outputs"}


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRECTORIES for part in path.parts)


def _text_files(repo_root: Path):
    if not repo_root.exists():
        return
    for path in repo_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS and not _is_skipped(path.relative_to(repo_root)):
            yield path


def find_encoding_issues(repo_root: Path) -> dict[str, list[Path]]:
    root = Path(repo_root)
    issues = {"mojibake": [], "invalid_utf8": []}
    for path in _text_files(root) or ():
        try:
            data = path.read_bytes()
            if b"\x00" in data:
                continue
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            issues["invalid_utf8"].append(path)
            continue
        except OSError:
            continue
        if any(token in text for token in BAD_TOKENS):
            issues["mojibake"].append(path)
    return issues


def find_mojibake(repo_root: Path) -> list[Path]:
    """Return all text files that contain mojibake or invalid UTF-8."""
    issues = find_encoding_issues(repo_root)
    return sorted(set(issues["mojibake"] + issues["invalid_utf8"]))


def run_encoding_check(repo_root: Path) -> int:
    root = Path(repo_root).resolve()
    issues = find_encoding_issues(root)
    if not any(issues.values()):
        return 0
    if issues["mojibake"]:
        print("mojibake token detected:")
        print("\n".join(str(path.relative_to(root)) for path in issues["mojibake"]))
    if issues["invalid_utf8"]:
        print("invalid UTF-8 detected:")
        print("\n".join(str(path.relative_to(root)) for path in issues["invalid_utf8"]))
    return 1


__all__ = ["BAD_TOKENS", "find_encoding_issues", "find_mojibake", "repository_root", "run_encoding_check"]
