"""Per-session feature extraction from a Claude Code transcript JSONL.

One transcript -> one feature row. The label is the "dumb zone": did the live
context window ever exceed 300k tokens (peak context = max over turns of
input + cache_read + cache_creation tokens).
"""
import json
from pathlib import Path

DUMB_ZONE_TOKENS = 300_000


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content
                   if isinstance(b, dict) and b.get("type") == "text")
    return 0


def extract(path) -> dict:
    path = Path(path)
    session_id = path.stem
    out_tok = in_tok = cache_tok = 0
    peak_context = 0
    n_user = n_asst = tool_calls = web = interrupts = 0
    skills, tools = set(), set()
    title = ""
    project = ""
    first_user_chars = 0
    duration_ms = 0

    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        t = r.get("type")
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
                out_tok += u.get("output_tokens", 0)
                in_tok += it
                cache_tok += cr + cc
                peak_context = max(peak_context, it + cr + cc)
                stu = u.get("server_tool_use") or {}
                web += stu.get("web_search_requests", 0) + stu.get("web_fetch_requests", 0)
            if t == "assistant":
                n_asst += 1
                c = m.get("content", [])
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            tool_calls += 1
                            tools.add(b.get("name", "?"))

    return {
        "session_id": session_id,
        "title": (title or "")[:120],
        "project": project or "unknown",
        "n_user_msgs": n_user,
        "n_assistant_msgs": n_asst,
        "tool_calls": tool_calls,
        "distinct_tools": len(tools),
        "web_calls": web,
        "interrupts": interrupts,
        "n_skills": len(skills),
        "first_user_chars": first_user_chars,
        "output_tokens": out_tok,
        "input_tokens": in_tok,
        "cache_tokens": cache_tok,
        "peak_context_tokens": peak_context,
        "duration_min": round(duration_ms / 60000.0, 2),
        "dumb_zone": int(peak_context > DUMB_ZONE_TOKENS),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(extract(sys.argv[1]), indent=2))
