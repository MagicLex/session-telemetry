"""Gather session telemetry into the feature group. Runs as a SCHEDULED
Hopsworks job (the reliable accumulator) and also works from the terminal.

Why a job: the transcripts are born in the ephemeral terminal pod. An in-pod
Stop hook copies them to HopsFS while the pod is alive (the one step that MUST
be in-pod, since no job can read another pod's filesystem). This job then runs
on managed compute, on a schedule, reading the persisted copies from HopsFS and
upserting the feature group. It does not depend on any session being open.

Robustness:
- event_time = the session's last-activity timestamp, so a session that grows
  (or crashed mid-way and resumes) converges: the latest insert wins on read.
- the in-flight session (last activity within STALE_MIN) is skipped; it gets
  logged once it goes quiet.
- only new-or-grown sessions are inserted, so re-runs are cheap and idempotent.
"""
import glob
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import hopsworks
from hsfs.feature import Feature

DUMB_ZONE_TOKENS = 300_000
POD_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content
                   if isinstance(b, dict) and b.get("type") == "text")
    return 0


def extract(path) -> dict:
    """One transcript -> one feature row. Label = peak context > 300k tokens.
    Inlined (not imported) so this file deploys as a self-contained job."""
    path = Path(path)
    session_id = path.stem
    out_tok = in_tok = cache_tok = peak_context = 0
    n_user = n_asst = tool_calls = web = interrupts = 0
    skills, tools = set(), set()
    title = project = last_activity = ""
    first_user_chars = duration_ms = 0
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
        if r.get("sessionId"):
            session_id = r["sessionId"]
        if r.get("timestamp") and r["timestamp"] > last_activity:
            last_activity = r["timestamp"]
        if r.get("aiTitle"):
            title = r["aiTitle"]
        if r.get("cwd") and not project:
            project = str(r["cwd"]).rstrip("/").split("/")[-1]
        if r.get("attributionSkill"):
            skills.add(r["attributionSkill"])
        if r.get("interruptedMessageId"):
            interrupts += 1
        if isinstance(r.get("durationMs"), (int, float)):
            duration_ms += r["durationMs"]
        m = r.get("message")
        if t == "user":
            n_user += 1
            if first_user_chars == 0 and isinstance(m, dict):
                first_user_chars = _text_len(m.get("content"))
        if isinstance(m, dict):
            u = m.get("usage")
            if u:
                it = u.get("input_tokens", 0)
                cr = u.get("cache_read_input_tokens", 0)
                cc = u.get("cache_creation_input_tokens", 0)
                out_tok += u.get("output_tokens", 0); in_tok += it; cache_tok += cr + cc
                peak_context = max(peak_context, it + cr + cc)
                stu = u.get("server_tool_use") or {}
                web += stu.get("web_search_requests", 0) + stu.get("web_fetch_requests", 0)
            if t == "assistant":
                n_asst += 1
                for b in (m.get("content", []) if isinstance(m.get("content"), list) else []):
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_calls += 1; tools.add(b.get("name", "?"))
    return {
        "session_id": session_id, "last_activity": last_activity,
        "title": (title or "")[:120], "project": project or "unknown",
        "n_user_msgs": n_user, "n_assistant_msgs": n_asst, "tool_calls": tool_calls,
        "distinct_tools": len(tools), "web_calls": web, "interrupts": interrupts,
        "n_skills": len(skills), "first_user_chars": first_user_chars,
        "output_tokens": out_tok, "input_tokens": in_tok, "cache_tokens": cache_tok,
        "peak_context_tokens": peak_context, "duration_min": round(duration_ms / 60000.0, 2),
        "dumb_zone": int(peak_context > DUMB_ZONE_TOKENS),
    }
FUSE_DIR = Path("/hopsfs/Users/lex00000/session-telemetry/transcripts")
HOPSFS_DIR = "Users/lex00000/session-telemetry/transcripts"
FG_NAME = "session_telemetry"
FG_VERSION = 1
STALE_MIN = 15  # a session quiet for this long is considered complete

INT_COLS = ["n_user_msgs", "n_assistant_msgs", "tool_calls", "distinct_tools",
            "web_calls", "interrupts", "n_skills", "first_user_chars",
            "output_tokens", "input_tokens", "cache_tokens",
            "peak_context_tokens", "dumb_zone"]
STR_COLS = ["session_id", "title", "project"]


def get_transcripts(project):
    """Return local paths to all transcript copies. In the terminal, first sync
    the pod's transcripts to HopsFS. In a job, read them from HopsFS (FUSE if
    mounted, else download via the dataset API)."""
    pod = glob.glob(POD_GLOB)
    if pod:  # terminal: persist pod -> HopsFS
        FUSE_DIR.mkdir(parents=True, exist_ok=True)
        for src in pod:
            try:
                shutil.copy2(src, FUSE_DIR / Path(src).name)
            except Exception as e:
                print(f"  sync skip {src}: {e}", flush=True)
    if FUSE_DIR.exists() and any(FUSE_DIR.glob("*.jsonl")):
        return list(FUSE_DIR.glob("*.jsonl"))
    # job without FUSE access: pull from HopsFS via the dataset API
    da = project.get_dataset_api()
    local = Path("/tmp/st_transcripts"); local.mkdir(exist_ok=True)
    for p in da.list(HOPSFS_DIR):
        if p.endswith(".jsonl"):
            try:
                da.download(p, str(local), overwrite=True)
            except Exception as e:
                print(f"  download skip {p}: {e}", flush=True)
    return list(local.glob("*.jsonl"))


def feature_list(sample):
    feats = []
    for k in sample:
        if k == "last_activity":
            feats.append(Feature(k, "timestamp", description="Last activity (event time)"))
        elif k in INT_COLS:
            feats.append(Feature(k, "bigint", description=k))
        elif k in STR_COLS:
            feats.append(Feature(k, "string", description=k))
        else:
            feats.append(Feature(k, "double", description=k))
    return feats


def logged_max(fg):
    """session_id -> latest last_activity already in the FG."""
    try:
        df = fg.read(dataframe_type="pandas")
        df["last_activity"] = pd.to_datetime(df["last_activity"], utc=True)
        return df.groupby("session_id")["last_activity"].max().to_dict()
    except Exception:
        return {}


def main():
    project = hopsworks.login()
    fs = project.get_feature_store()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_MIN)

    rows = []
    for f in get_transcripts(project):
        try:
            r = extract(f)
        except Exception as e:
            print(f"  extract failed {f.name}: {e}", flush=True)
            continue
        if r["n_user_msgs"] < 2 or not r["last_activity"]:
            continue
        r["last_activity"] = pd.to_datetime(r.pop("last_activity"), utc=True)
        if r["last_activity"] > cutoff:
            continue  # in-flight session; log it next run
        rows.append(r)
    if not rows:
        print("no completed sessions", flush=True)
        return

    fg = fs.get_or_create_feature_group(
        name=FG_NAME, version=FG_VERSION,
        description="One row per Claude Code session: token/tool/duration "
                    "telemetry + dumb-zone (peak context > 300k) label.",
        primary_key=["session_id"], event_time="last_activity",
        features=feature_list(rows[0]),
        online_enabled=False, statistics_config=True,
    )
    seen = logged_max(fg)
    new = [r for r in rows if r["session_id"] not in seen
           or r["last_activity"] > seen[r["session_id"]]]
    print(f"{len(rows)} completed, {len(new)} new-or-grown", flush=True)
    if not new:
        return
    df = pd.DataFrame(new)
    for c in INT_COLS:
        df[c] = df[c].astype("int64")
    fg.insert(df, wait=True)
    print(f"inserted {len(new)} sessions into {FG_NAME}", flush=True)


if __name__ == "__main__":
    main()
