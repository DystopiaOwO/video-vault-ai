# Issue 67 Approved Render Contract

This document is the product and persistence contract for the approved-render
pipeline introduced by Issue 67. It deliberately favours a safe re-approval
over attempting to guess an old project's immutable state.

## 1. Approval snapshot schema and migration

`approval_snapshot.schema_version = 1` is an immutable, content-addressed
record written below `08_projects/project_<id>/approvals/`. It contains the
canonical manifest, its deterministic hash, the effective storyboard/audio/
color/profile state and fingerprints for every render asset.

`review_status.json` contains only the current snapshot id and hash. A render
job persists a copy of that snapshot and never recompiles mutable project
state. Existing approvals without a v1 snapshot are treated as legacy and
must be approved once again; they are never silently upgraded from mutable
files. Snapshot creation is staged and atomically published only after every
asset fingerprint and effective state validates.

## 2. DJI and LUT contract

The product uses **Option A**: `dji_lut`, `dji_dlog`, and `dji_dlog_m` are
user-managed LUT modes. There are no bundled DJI transforms in this project.
The backend owns the mode requirement, resource validation and fingerprint;
the browser consumes that contract and must not duplicate path rules.

Each enabled DJI mode requires a readable regular `.cube` file. Validation
includes an FFmpeg `lut3d` parse probe before approval. A snapshot records the
resolved path, stable resource id, size, mtime nanoseconds and SHA-256.

## 3. Encoder resolution and fallback

Each formal render resolves one encoder contract before the first segment.
`libx264`/`cpu` is explicit. `auto` makes one controlled NVENC probe: success
pins all segments to `h264_nvenc`; an eligible device/initialisation failure
pins all segments to `libx264` and records the fallback reason. Segment
rendering may not independently fallback after this point.

The contract includes implementation, preset/rate-control policy, H.264
profile/level, GOP/B-frame policy, exact FPS rational/time base, pixel format,
FFmpeg version and capability-contract version. Cache reuse requires an exact
contract match. A runtime encoder failure fails the render rather than mixing
bitstreams; the user may retry the whole job with the recorded CPU fallback.

## 4. Loudness and final QC tolerance

Formal output is SDR Rec.709 (`bt709` primaries, transfer and matrix;
`tv` range), yuv420p, CFR and 48 kHz stereo. Full-project mixed audio uses a
two-pass loudnorm measurement. The current target is -14 LUFS integrated and
-1.0 dBTP maximum true peak.

Hard failures: decode error, missing streams, profile/color mismatch,
non-monotonic timestamps, frame-count inconsistency, audio/video tail drift
over max(150 ms, 3 frames), integrated loudness outside +/-1.0 LU and true
peak above target + 0.1 dB. Heuristics such as black, frozen frames, silence
and clipping are warnings only and do not block a technically valid output.

Final MP4 and its report are staged to temporary paths. They are published
together only after hard QC passes; cancellation remains accepted until the
publish boundary and becomes `cancel_too_late` after it.
