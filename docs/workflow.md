# Workflow

1. Put raw clips in `00_inbox`.
2. Run `python -m video_vault dry-run`.
3. Run `scan`, then `ingest`.
4. Run `extract-frames`, `make-proxy`, `index`, `analyze`, `report`.

`analyze` is mock-only for now, so the whole pipeline works before paying the AI integration cost.
