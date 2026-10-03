# Linear Backup — Video Vault AI (VID)

- Exported: 2026-10-03 22:13 (+08:00)
- Source team: `VID` / Videoai
- Project: `Video Vault AI`
- Issues: **46** (VID-5–VID-50)
- Verification: missing=0, duplicates=0
- Comments preserved: **250**
- Attachment references preserved: **54**
- Issue document references preserved: **0**
- Project documents preserved: **2**

## Files

- `manifest.json` — export summary and verification.
- `linear-metadata.json` — team/project metadata, statuses, labels, cycles, project status updates/comments, document index.
- `documents.json` — full project document payloads and document comments.
- `issues-01-VID-05-VID-12.json` — full issue payloads + comments.
- `issues-02-VID-13-VID-20.json` — full issue payloads + comments.
- `issues-03-VID-21-VID-28.json` — full issue payloads + comments.
- `issues-04-VID-29-VID-36.json` — full issue payloads + comments.
- `issues-05-VID-37-VID-44.json` — full issue payloads + comments.
- `issues-06-VID-45-VID-50.json` — full issue payloads + comments.

## Restore notes

The JSON preserves original Linear IDs, descriptions, timestamps, state history, parent links, relations, labels, attachment/document metadata, and comments as returned by Linear.
If the Linear team is recreated later, restore team statuses/labels first, then project/documents, then parent issues before child issues, then comments/relations.
Historical state-transition timestamps and original Linear internal UUIDs are archival evidence; a recreated team may receive new internal IDs.

## Verification

This backup was re-read from the GitHub backup branch after writing. The exported issue set contains every identifier from VID-5 through VID-50 exactly once.

## Linear-hosted attachment payloads

Three attachments were hosted on Linear's signed upload CDN rather than GitHub. Their payloads were fetched before cleanup:

- VID-18: E4B formal perception blocker evidence (Markdown)
- VID-49: Coffee primary preview v3 (PNG, stored as base64)
- VID-50: Coffee primary preview v2 (PNG, stored as base64)

Run `python restore_attachments.py` inside this backup directory to reconstruct the two PNG files losslessly. The other 51 attachment references point to GitHub issues, PRs, commits, or external repository references and remain preserved in the issue JSON.

Comment pagination was verified: all 46 issues report `hasNextPage=false`; the 250 preserved comments are the complete issue-comment set at export time.
