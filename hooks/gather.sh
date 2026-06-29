#!/usr/bin/env bash
# SessionStart hook: idempotent telemetry gather, detached so it never blocks
# session startup. Logs to /tmp; failures are harmless (retried next session).
nohup /srv/hops/venv/bin/python \
  /hopsfs/Users/lex00000/session-telemetry/gather/gather.py \
  >/tmp/session_gather.log 2>&1 &
exit 0
