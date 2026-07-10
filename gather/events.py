"""Event-level telemetry -> ONLINE feature group `session_events`.

The matrix view: one row per transcript event (user msg, assistant turn, tool
call), numeric-only. Anonymized by construction: content never leaves the
extractor, only counts, sizes, timings, token usage and two small categoricals
(event_type, tool name). Security flags are computed in-extractor on the raw
text and stored as counts/bits.

Same deployment model as gather.py: runs as a scheduled Hopsworks job reading
the HopsFS-persisted transcripts (synced in-pod by the Stop hook), also works
from the terminal. Events are immutable once written (PK session_id+event_idx),
so re-runs only insert indices not yet logged and upserts are harmless.
"""
import glob
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import hopsworks
from hsfs.feature import Feature

POD_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
FUSE_DIR = Path("/hopsfs/Users/lex00000/session-telemetry/transcripts")
HOPSFS_DIR = "Users/lex00000/session-telemetry/transcripts"
FG_NAME = "session_events"
FG_VERSION = 1

# secret-shaped strings; we store the MATCH COUNT, never the match
SECRET_RE = re.compile(
    r'(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{36}'
    r'|xox[baprs]-|password\s*[=:]\s*\S+|api[_-]?key\s*[=:]\s*\S+)', re.I)

INT_COLS = ["event_idx", "gap_ms", "chars", "in_tok", "out_tok", "cache_read",
            "cache_create", "context_tok", "n_tools", "is_tool_result",
            "is_error", "secret_hits", "interrupt", "sandbox_off"]
STR_COLS = ["session_id", "event_type", "tool"]


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content
                   if isinstance(b, dict) and b.get("type") == "text")
    return 0


def extract_events(path):
    """One transcript -> numeric event rows. No content is retained."""
    path = Path(path)
    session_id = path.stem
    rows, prev_dt, idx = [], None, 0
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
        ts = r.get("timestamp")
        if t not in ("user", "assistant") or not ts:
            continue
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        gap_ms = int((dt - prev_dt).total_seconds() * 1000) if prev_dt else 0
        prev_dt = dt
        msg = r.get("message", {}) if isinstance(r.get("message"), dict) else {}
        content = msg.get("content", "")
        usage = msg.get("usage", {}) if t == "assistant" else {}
        tools, tool_result, err, sandbox_off = [], 0, 0, 0
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tools.append(b.get("name", "?"))
                    if isinstance(b.get("input"), dict) and \
                            b["input"].get("dangerouslyDisableSandbox"):
                        sandbox_off = 1
                if b.get("type") == "tool_result":
                    tool_result = 1
                    if b.get("is_error"):
                        err = 1
        blob = content if isinstance(content, str) else json.dumps(content)
        it = usage.get("input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cc = usage.get("cache_creation_input_tokens", 0)
        rows.append({
            "session_id": session_id, "event_idx": idx, "ts": dt,
            "event_type": t, "tool": tools[0] if tools else "",
            "gap_ms": max(gap_ms, 0), "chars": _text_len(content),
            "in_tok": it, "out_tok": usage.get("output_tokens", 0),
            "cache_read": cr, "cache_create": cc, "context_tok": it + cr + cc,
            "n_tools": len(tools), "is_tool_result": tool_result,
            "is_error": err, "secret_hits": len(SECRET_RE.findall(blob)),
            "interrupt": 1 if "[Request interrupted" in blob else 0,
            "sandbox_off": sandbox_off,
        })
        idx += 1
    return rows


def get_transcripts(project):
    """Same contract as gather.py: pod -> HopsFS sync, else dataset API."""
    pod = glob.glob(POD_GLOB)
    if pod:
        FUSE_DIR.mkdir(parents=True, exist_ok=True)
        for src in pod:
            try:
                shutil.copy2(src, FUSE_DIR / Path(src).name)
            except Exception as e:
                print(f"  sync skip {src}: {e}", flush=True)
    if FUSE_DIR.exists() and any(FUSE_DIR.glob("*.jsonl")):
        return list(FUSE_DIR.glob("*.jsonl"))
    da = project.get_dataset_api()
    local = Path("/tmp/st_transcripts"); local.mkdir(exist_ok=True)
    for p in da.list(HOPSFS_DIR):
        if p.endswith(".jsonl"):
            try:
                da.download(p, str(local), overwrite=True)
            except Exception as e:
                print(f"  download skip {p}: {e}", flush=True)
    return list(local.glob("*.jsonl"))


def feature_list():
    feats = [Feature("ts", "timestamp", description="Event time")]
    for k in STR_COLS:
        feats.append(Feature(k, "string", description=k))
    for k in INT_COLS:
        feats.append(Feature(k, "bigint", description=k))
    return feats


def logged_max_idx(fg):
    """session_id -> highest event_idx already in the FG."""
    try:
        df = fg.select(["session_id", "event_idx"]).read(dataframe_type="pandas")
        return df.groupby("session_id")["event_idx"].max().to_dict()
    except Exception:
        return {}


def main():
    project = hopsworks.login()
    fs = project.get_feature_store()

    rows = []
    for f in get_transcripts(project):
        try:
            rows.extend(extract_events(f))
        except Exception as e:
            print(f"  extract failed {f.name}: {e}", flush=True)
    if not rows:
        print("no events", flush=True)
        return

    fg = fs.get_or_create_feature_group(
        name=FG_NAME, version=FG_VERSION,
        description="One row per Claude Code transcript event, numeric-only "
                    "(anonymized at extraction). Token flow + security flags "
                    "(secret_hits, is_error, interrupt, sandbox_off).",
        primary_key=["session_id", "event_idx"], event_time="ts",
        features=feature_list(),
        online_enabled=True, statistics_config=True,
    )
    seen = logged_max_idx(fg)
    new = [r for r in rows
           if r["event_idx"] > seen.get(r["session_id"], -1)]
    print(f"{len(rows)} events total, {len(new)} new", flush=True)
    if not new:
        return
    df = pd.DataFrame(new)
    for c in INT_COLS:
        df[c] = df[c].astype("int64")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    fg.insert(df, wait=True)
    print(f"inserted {len(new)} events into {FG_NAME}", flush=True)


if __name__ == "__main__":
    main()
