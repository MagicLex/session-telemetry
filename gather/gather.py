"""Sync transcripts to persistent storage and upsert session features.

Survives session + pod restart:
  1. Copy pod transcripts (~/.claude/projects/*/*.jsonl, on the EPHEMERAL overlay
     fs) to HopsFS (the persistent FUSE mount). Raw data outlives the pod.
  2. Compute one feature row per COMPLETED session and insert only sessions not
     already in the feature group (idempotent; double-runs are harmless).

The currently-active session (most recently modified transcript) is skipped; it
gets logged on the next run once it is complete. Its raw data is already safe in
HopsFS via step 1.
"""
import glob
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import hopsworks
from hsfs.feature import Feature

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from extract import extract  # noqa: E402

POD_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
TRANSCRIPTS = Path("/hopsfs/Users/lex00000/session-telemetry/transcripts")
FG_NAME = "session_telemetry"
FG_VERSION = 1

INT_COLS = ["n_user_msgs", "n_assistant_msgs", "tool_calls", "distinct_tools",
            "web_calls", "interrupts", "n_skills", "first_user_chars",
            "output_tokens", "input_tokens", "cache_tokens",
            "peak_context_tokens", "dumb_zone"]
STR_COLS = ["session_id", "title", "project"]


def sync_to_hopsfs():
    """Copy pod transcripts to persistent HopsFS. Returns the active session id
    (most recently modified pod transcript) to skip."""
    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    pod = glob.glob(POD_GLOB)
    active = None
    if pod:
        active = Path(max(pod, key=os.path.getmtime)).stem
        for src in pod:
            dst = TRANSCRIPTS / Path(src).name
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"  sync skip {src}: {e}", flush=True)
    return active


def feature_list(sample):
    feats = []
    for k in sample:
        if k in INT_COLS:
            feats.append(Feature(k, "bigint", description=k))
        elif k in STR_COLS:
            feats.append(Feature(k, "string", description=k))
        else:
            feats.append(Feature(k, "double", description=k))
    return feats


def existing_ids(fg):
    try:
        df = fg.read(dataframe_type="pandas")
        return set(df["session_id"].astype(str))
    except Exception:
        return set()  # FG empty / not materialized yet


def main():
    active = sync_to_hopsfs()
    print(f"active session (skipped): {active}", flush=True)

    rows = []
    for f in sorted(TRANSCRIPTS.glob("*.jsonl")):
        sid = f.stem
        if sid == active:
            continue
        try:
            r = extract(f)
        except Exception as e:
            print(f"  extract failed {sid}: {e}", flush=True)
            continue
        if r["n_user_msgs"] >= 2:  # real, completed session
            rows.append(r)
    if not rows:
        print("no completed sessions found", flush=True)
        return

    project = hopsworks.login()
    fs = project.get_feature_store()
    fg = fs.get_or_create_feature_group(
        name=FG_NAME, version=FG_VERSION,
        description="One row per Claude Code session: token/tool/duration "
                    "telemetry + dumb-zone (peak context > 300k) label.",
        primary_key=["session_id"],
        features=feature_list(rows[0]),
        online_enabled=False, statistics_config=True,
    )
    seen = existing_ids(fg)
    new = [r for r in rows if r["session_id"] not in seen]
    print(f"{len(rows)} completed, {len(seen)} already logged, {len(new)} new", flush=True)
    if not new:
        return
    df = pd.DataFrame(new)
    for c in INT_COLS:
        df[c] = df[c].astype("int64")
    fg.insert(df, wait=True)
    print(f"inserted {len(new)} sessions into {FG_NAME}", flush=True)


if __name__ == "__main__":
    main()
