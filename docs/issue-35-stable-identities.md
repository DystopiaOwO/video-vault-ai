# Issue #35 — Stable segment and project-media identities

## Scope of this batch

This branch establishes the identity foundation required by Issue #35:

- persistent `segment_uuid` and revision fields in SQLite;
- persistent project-media UUIDs independent of `clip_001` display order;
- deterministic backfill for existing rows;
- one-to-one segment matching across reanalysis;
- explicit split, merge, removal, new-segment, and ambiguity reports;
- automatic migration of project plan, segment review, Storyboard, audio, and color state;
- orphan and collision records instead of silent state loss;
- project approval invalidation after a reanalysis identity migration.

## Identity rules

### Project media

`project_media_uuid` is the stable identity for a video inside a project. `clip_001`, `clip_002`, and similar values remain display-order aliases only. Reordering or inserting media does not change existing project-media UUIDs.

### Perceived segments

`segment_uuid` is stored with the perceived segment row. A reanalysis uses temporal overlap, midpoint proximity, and a small semantic tie-breaker to find one-to-one matches. A confident one-to-one match keeps the UUID and increments `revision`.

A split keeps the previous UUID on the best primary child and creates deterministic child UUIDs for the other children. A merge keeps the best primary UUID. Both operations are reported with `requires_review=true`; no automatic claim is made that user intent can be transferred safely.

### Existing user state

After a video is reanalyzed, every linked project is checked before its plan is rebuilt. Legacy IDs such as `clip_001_00012000` are mapped to the stable UUID and migrated in:

- `project_plan.json`;
- `feedback/segment_review.json` and the legacy root review file;
- `storyboard.json`;
- `audio_settings.json`;
- `color_consistency.json`.

Unmatched state is retained in its source file and listed in `validation/segment_identity_migration_latest.json`. Duplicate destinations are merged and reported as conflicts. A migration invalidates prior approval and returns the project to `needs_review`.

## Deliberately deferred

Issue #28 remains responsible for perception-run entities, atomic multi-file publishing, crash recovery, and complete revision-history retention. This batch records identity migration history but does not replace the broader publishing architecture planned there.
