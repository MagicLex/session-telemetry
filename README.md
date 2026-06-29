# Session Telemetry (meta ML)

A slow-burn meta system: log one feature row per Claude Code session, accumulate
over time, then train a model that predicts a session's behaviour from its early
signals.

**Target:** the "dumb zone" classifier. Will a session's live context blow past
300k tokens (the bloat zone where models degrade)? Predicted from early-session
features (first user message, task type, early tool-call rate, project), so it
can warn before the blowup.

Built on [Hopsworks](https://www.hopsworks.ai/), forked from the
[readme-vaporware-score](https://github.com/MagicLex/readme-vaporware-score) base.

## Surviving session and pod restart (the load-bearing design)

The transcripts live at `~/.claude/projects/<proj>/<session>.jsonl`, which is the
pod's **`overlay`** filesystem: ephemeral, wiped on container recreation. So the
gatherer does not depend on it.

- **Stop hook** copies the live transcript(s) to HopsFS
  (`/hopsfs/Users/.../session-telemetry/transcripts/`, the persistent FUSE mount)
  after each turn. Raw data survives pod death.
- **SessionStart hook** runs an idempotent gather: read the HopsFS transcript
  copies, compute per-session features, and insert only sessions not already in
  the feature group (keyed by `session_id`). A missed end or a pod restart
  self-heals on the next session start. Double-runs are harmless.
- The feature group lives in Hopsworks. Raw and features both persist.

## Features per session

Token usage (in/out/cache, peak context), message counts, tool-call count and
diversity, web searches, duration, interruptions, skills used, session title,
git branch. Label: peak live context > 300k.

## Status

- [x] Feature extractor validated against existing transcripts
- [ ] Gather script + feature group + backfill of existing sessions
- [ ] Stop + SessionStart hooks wired in `settings.json`
- [ ] Meta model (trains once enough sessions accumulate, ~a week)
