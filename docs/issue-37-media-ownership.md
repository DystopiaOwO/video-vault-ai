# Issue #37 — Shared immutable source with project-local media state

## Product decision

The supported ownership model is **Option B: one shared immutable source asset with project-local effective state**.

A `videos` row identifies the imported physical source. A `project_videos` row identifies how that source is used inside one project. The project-media identity is `project_media_uuid`; `clip_001` remains a display-order alias only.

## Project-local fields

Each project-media relation snapshots and owns:

- display name;
- category override;
- user summary override;
- analysis/workflow status;
- perception revision and timestamp.

Creating another project relation snapshots the current source defaults but does not share later project-local edits. Reordering a project preserves the existing project-media UUID and metadata.

## Naming contract

Project perception may calculate a suggested display name and category, but it must not rename the physical source or update the global `videos.current_path`, `videos.filename`, or `videos.category` fields.

The legacy non-project inbox workflow keeps its existing rename behavior for compatibility. Project jobs are detected through `project_media_uuid` and use project-local metadata only.

## Summary contract

The project-aware summary API writes only the selected project-media relation. The legacy three-argument summary function fails closed when a video belongs to more than one project, preventing an old client from silently overwriting every project.

## Shared analysis during the transition

Frames and perceived segments are still shared effective inputs in this foundation batch. A reanalysis therefore affects every linked project. The stable-identity migration from Issue #35 enumerates every linked project, migrates its state, invalidates prior approval, and returns each project to `needs_review`.

Issue #28 will move analysis into explicit perception runs with atomic publishing and history. Issue #36 will finish separating user annotations from AI perception output and wire project-local summary projection through every legacy view.

## Safety properties covered here

- Naming in project A does not alter project B or the shared source path.
- Project-local summary edits do not alter global frame summaries or another project.
- Legacy shared-summary writes fail rather than corrupt multiple projects.
- Reanalysis of a truly shared effective input invalidates every linked project.
- Removing one project relation does not delete the shared source or another project's relation.
