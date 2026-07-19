from __future__ import annotations

from video_vault.encoding_check import repository_root, run_encoding_check


def main() -> int:
    return run_encoding_check(repository_root())


if __name__ == "__main__":
    raise SystemExit(main())
