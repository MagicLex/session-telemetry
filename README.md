# Session Telemetry (meta ML)

![Session Telemetry](assets/banner.svg)

[![status](https://img.shields.io/badge/status-WIP-f59e0b?labelColor=0b0e11&style=flat)](#status)
[![Hopsworks](https://img.shields.io/badge/built_on-Hopsworks-1CB182?labelColor=0b0e11&style=flat)](https://www.hopsworks.ai/)

WIP. Collection is running; the models are not built yet.

A slow-burn meta system on Claude Code sessions, with two telemetry grains:

- **Realtime, event-level** (`session_events`, online store): one numeric row per
  transcript event. Token flow, timings, tool mix, security flags. Anonymized at
  extraction: content never enters the store.
- **Session-level** (`session_telemetry`, offline): one feature row per session,
  aggregated. Peak context, counts, duration, interrupts.

**Targets: not decided yet.** Current lean, two heads on the same data:

- **Anomaly detection** on the realtime event stream (odd token flow, loops,
  security flags). Weak labels available for evaluation: interrupts, tool
  errors, killed sessions.
- **Degraded-session entry**: predict from early signals that a session is
  heading into the degraded zone (live context past ~300k tokens, where models
  get worse), so it can warn before the blowup.

Built on [Hopsworks](https://www.hopsworks.ai/), forked from the
[readme-vaporware-score](https://github.com/MagicLex/readme-vaporware-score) base.

## Surviving session and pod restart (the load-bearing design)

The transcripts live at `~/.claude/projects/<proj>/<session>.jsonl`, which is the
pod's **`overlay`** filesystem: ephemeral, wiped on container recreation. So the
gatherer does not depend on it.

- **In-pod Stop hook** (the only irreducibly in-pod piece: no job can read
  another pod's `~/.claude`) copies transcripts to HopsFS
  (`/hopsfs/Users/.../session-telemetry/transcripts/`) after each turn. It runs
  while the pod is alive, so when the pod dies the data is already persisted. The
  hook is registered in the **persistent project settings**
  (`/hopsfs/.../.claude/settings.json` on HopsFS, not the ephemeral
  `~/.claude/settings.json`), so a new pod re-arms it automatically. Re-arm by
  hand with `hooks/install.sh`.
- **Scheduled Hopsworks job** `session-gather` (hourly) is the accumulator. It
  reads the persisted transcripts from HopsFS (via the dataset API, so it needs
  no pod), computes features, and upserts the feature group. Runs on managed
  compute regardless of whether any session is open. `event_time` =
  last-activity, so a session that grows (or crashed mid-way and resumed)
  converges: the latest insert wins on read. Only new-or-grown sessions are
  inserted, so runs are idempotent.
- If a session crashes: everything up to the last completed turn is already in
  HopsFS (synced every turn), and the next job run logs it. At most the one
  in-progress turn is lost.

## Event stream (the matrix view)

`gather/events.py` extracts one row per transcript event into the
**online-enabled** feature group `session_events` (PK `session_id+event_idx`,
event_time `ts`). Numeric-only, anonymized at extraction: content never leaves
the extractor, only token usage, sizes, timings and two categoricals
(event_type, tool). Security flags computed in-extractor: `secret_hits`
(secret-shaped regex match count, never the match), `is_error`, `interrupt`,
`sandbox_off`. Accumulated hourly by the `session-events` job (same pattern as
`session-gather`, offset to :15). This is the online store a live anomaly /
security detector would read from.

## Features per session

Token usage (in/out/cache, peak context), message counts, tool-call count and
diversity, web searches, duration, interruptions, skills used, session title,
git branch. Label: peak live context > 300k.

## Status

- [x] Feature extractor (`gather/extract.py`) validated against transcripts
- [x] Gather script (`gather/gather.py`): sync to HopsFS + idempotent FG upsert
- [x] Feature group `session_telemetry` created + existing sessions backfilled
- [x] In-pod Stop sync hook armed in persistent project settings (`hooks/`)
- [x] Scheduled `session-gather` job (hourly) = the reliable accumulator
- [x] Event-level online FG `session_events` + hourly `session-events` job (backfilled ~20k events)
- [ ] Models. Target undecided; leaning anomaly detection on the event stream
      plus degraded-session entry prediction. Data accumulates meanwhile.
