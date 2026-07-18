from __future__ import annotations

from pathlib import Path
import sys

BAD = tuple(chr(code) for code in (0xFFFD, 0x929D, 0x5697, 0x875D, 0x64A0, 0x6470, 0x61BF, 0x8758, 0xEAF1, 0xEBEF, 0xF387, 0xF699))
ROOTS = ("src", "tests", "scripts", "README.md", "AGENTS.md")
EXTS = {".py", ".md", ".html", ".ps1", ".yaml", ".yml"}


def main() -> int:
    bad_files = []
    for root in ROOTS:
        path = Path(root)
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.suffix.lower() in EXTS]
        for file in files:
            text = file.read_text(encoding="utf-8", errors="replace")
            if any(token in text for token in BAD):
                bad_files.append(str(file))
    if bad_files:
        print("mojibake detected:")
        print("\n".join(bad_files))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
