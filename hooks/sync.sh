#!/usr/bin/env bash
# Stop hook: persist Claude Code transcripts to HopsFS. The pod's ~/.claude is
# the ephemeral overlay fs; HopsFS survives pod restart. Fast, never blocks.
mkdir -p /hopsfs/Users/lex00000/session-telemetry/transcripts 2>/dev/null
cp ~/.claude/projects/*/*.jsonl /hopsfs/Users/lex00000/session-telemetry/transcripts/ 2>/dev/null
exit 0
