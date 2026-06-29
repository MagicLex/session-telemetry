#!/usr/bin/env bash
# Arm the in-pod transcript sync in the PERSISTENT project settings (HopsFS).
# This is the only in-pod piece: it copies transcripts to HopsFS while the pod
# is alive. The scheduled Hopsworks job `session-gather` does the accumulation.
python3 - <<'PY'
import json
p="/hopsfs/Users/lex00000/.claude/settings.json"
d=json.load(open(p)); h=d.setdefault("hooks",{})
SYNC="bash /hopsfs/Users/lex00000/session-telemetry/hooks/sync.sh"
if not any(SYNC==x.get("command") for g in h.get("Stop",[]) for x in g.get("hooks",[])):
    h.setdefault("Stop",[]).append({"hooks":[{"type":"command","command":SYNC}]})
json.dump(d,open(p,"w"),indent=2); print("Stop sync hook armed")
PY
