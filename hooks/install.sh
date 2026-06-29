#!/usr/bin/env bash
# Idempotently register the telemetry hooks in the PERSISTENT project settings
# (/hopsfs/Users/lex00000/.claude/settings.json, on HopsFS). Survives pod
# recreation. Re-run after a fresh project or if hooks ever go missing.
python3 - <<'PY'
import json
p="/hopsfs/Users/lex00000/.claude/settings.json"
d=json.load(open(p)); h=d.setdefault("hooks",{})
B="bash /hopsfs/Users/lex00000/session-telemetry/hooks"
hooks={"Stop":f"{B}/sync.sh","SessionStart":f"{B}/gather.sh"}
for ev,cmd in hooks.items():
    if not any(cmd==x.get("command") for g in h.get(ev,[]) for x in g.get("hooks",[])):
        h.setdefault(ev,[]).append({"hooks":[{"type":"command","command":cmd}]})
json.dump(d,open(p,"w"),indent=2); print("hooks armed:",list(h))
PY
