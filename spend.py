#!/usr/bin/env python3
"""agents-be-hungry: estimate Claude Code spend from local session transcripts.

Reads ~/.claude/projects/**.jsonl (or $CLAUDE_CONFIG_DIR/projects), read only.
Makes no network calls. Python 3 standard library only.
Works on macOS, Linux and Windows.

  python3 spend.py                 today's spend: totals, models, sessions, subagents
  python3 spend.py --html          write dashboard.html and open it
  python3 spend.py --artifact      write artifact.html, a page to publish as an artifact
  python3 spend.py --json          print everything as JSON
  python3 spend.py --watch         rewrite the dashboard every 60 seconds
  python3 spend.py --days N        history length (default 7)
  python3 spend.py --date YYYY-MM-DD   show a past day
  python3 spend.py --out PATH      where to write the dashboard
"""
import argparse
import html
import json
import os
import re
import sys
import time
import webbrowser
from collections import defaultdict
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ESTIMATE_LINE = ("This is an estimate at API prices. On a Pro or Max plan, you don't pay this; "
                 "it's what the same work would cost on the API.")
LOCAL_LINE = "Nothing here leaves this computer unless you publish it."
PRIVACY_LINE = ("This page shows your messages, subagent names and project folders. "
                "Don't share it as is.")
HIDDEN_LINE = ("Hidden background calls: Claude Code also works behind the scenes (shrinking long chats, "
               "naming sessions, safety checks) and doesn't save those calls in its logs. "
               "Their cost comes from Claude Code's own running total once a session closes, "
               "and is included in every total.")
TOKEN_LINE = "Tokens are word pieces. One token is about 3/4 of a word."
PAGE_TITLE = "Claude Code Spend"
RUNNING_SECONDS = 300
STALE_DAYS = 60
TOP_SESSIONS = 10
TOP_SUBAGENTS = 15
CHART_SERIES = 3          # models drawn in their own colour; the rest are "Other"
PRICE_KEYS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
TOKEN_KEYS = PRICE_KEYS + ("searches",)
INTERRUPT_MARK = "[Request interrupted"

# Where the money went: key, plain label, one-line explanation. Order is the display order.
COST_TYPES = (
    ("output", "Claude's replies",
     "Everything Claude writes: answers, code, tool use and thinking."),
    ("cache_read", "Claude re-reading the chat",
     "Every turn, Claude reads the whole chat again. Cheap per token, but it adds up in long sessions."),
    ("cache_write_5m", "Short-term memory (5 min)",
     "Saving new chat so the next turns can re-read it cheaply for 5 minutes."),
    ("cache_write_1h", "Longer memory (1 hour)",
     "The same, kept for 1 hour. Costs more to save."),
    ("input", "New text sent",
     "Text sent to Claude that wasn't saved to memory."),
    ("searches", "Web searches",
     "Searches Claude ran on the web. Charged per search."),
)
# The same, as words inside a sentence ("48% of today's cost was ...").
COST_PHRASES = {"output": "Claude's replies", "cache_read": "Claude re-reading the chat",
                "cache_write_5m": "saving to short-term memory (5 min)",
                "cache_write_1h": "saving to longer memory (1 hour)",
                "input": "new text sent", "searches": "web searches",
                "hidden": "hidden background calls"}
HIDDEN_ROW = ("hidden", "Hidden background calls",
              "Calls Claude Code makes behind the scenes (shrinking long chats, naming sessions, safety checks) "
              "and doesn't save in its logs. Known once a session closes.")

# The page has its own words. The terminal and --json keep the wording above, unchanged.
# One term per idea, in this order wherever the cost types repeat: key, label, short form, explanation.
PAGE_COST = (
    ("output", "Output", "Output",
     "Everything Claude writes: answers, code, tool calls and thinking."),
    ("cache_read", "Re-reading", "Re-reading",
     "On every turn, Claude re-reads the whole session from the cache. It's cheap per token, "
     "but it adds up in long sessions."),
    ("cache_write_5m", "Caching (5 min)", "Caching 5 min",
     "Saving new parts of the session to the cache, so the next turns can re-read them cheaply. Kept for 5 minutes."),
    ("cache_write_1h", "Caching (1 hour)", "Caching 1 hour",
     "The same, kept for 1 hour. It costs more to save."),
    ("input", "New input", "New input",
     "Text sent to Claude that isn't in the cache."),
    ("searches", "Web searches", "Web searches",
     "Searches Claude ran on the web. Charged per search."),
    ("hidden", "Background calls", "Background calls",
     "Calls Claude Code makes on its own, like summarizing a long session. "
     "They aren't in the logs, so they're known once a session closes."),
)
PAGE_LABEL = {k: label for k, label, _, _ in PAGE_COST}
PAGE_SHORT = {k: short for k, _, short, _ in PAGE_COST}
PAGE_EXPLAIN = {k: explain for k, _, _, explain in PAGE_COST}
PAGE_ESTIMATE_LINE = ("This is an estimate at API prices. On a Pro or Max plan you don't pay this. "
                      "It's what the same work would cost on the API.")
PAGE_BACKGROUND_LINE = ("Background calls: Claude Code also makes calls on its own, like summarizing a long session, "
                        "naming a session or running safety checks. It doesn't save them in its logs, so their cost "
                        "comes from Claude Code's own running total once a session closes. They're included in every total.")
PAGE_TLDR_KEY = "page_tldr"
PAGE_AVG_KEY = "page_average"   # the page's 30-day average; render_html uses it, --json leaves it out
AVG_DAYS = 30
PAGE_TOKEN_LINE = "Tokens are the small pieces of text Claude reads and writes. One token is about 3/4 of a word."


# ---------------------------------------------------------------- prices

def load_prices(path=None):
    path = path or os.path.join(HERE, "prices.json")
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.load(f)


def price_age_days(prices, today=None):
    """Days since prices.json was read from the pricing page, or None if unknown."""
    try:
        read = datetime.strptime(str(prices.get("date_read", "")), "%Y-%m-%d").date()
    except ValueError:
        return None
    return ((today or date.today()) - read).days


def stale_warning(prices, today=None):
    """A warning string when prices.json is old or undated, else None."""
    age = price_age_days(prices, today)
    if age is None:
        return "prices.json has no date, so its age is unknown. Update it from " + str(prices.get("source_url", "the pricing page")) + "."
    if age > STALE_DAYS:
        return ("Prices were last checked {} days ago ({}) and may have changed. Update prices.json from {}."
                .format(age, prices.get("date_read"), prices.get("source_url", "the pricing page")))
    return None


def find_price(prices, model):
    """Exact key, or key followed by an optional -YYYYMMDD date suffix."""
    if not model:
        return None
    models = prices.get("models", {})
    if model in models:
        return models[model]
    m = re.match(r"^(.*)-\d{8}$", model)
    if m and m.group(1) in models:
        return models[m.group(1)]
    return None


def _n(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def usage_numbers(usage):
    """Return dict of token counts from one usage object."""
    cc = usage.get("cache_creation")
    total_cw = _n(usage.get("cache_creation_input_tokens"))
    if isinstance(cc, dict):
        w5 = _n(cc.get("ephemeral_5m_input_tokens"))
        w1 = _n(cc.get("ephemeral_1h_input_tokens"))
        if w5 + w1 < total_cw:  # anything not itemised is billed as 5 minute
            w5 += total_cw - (w5 + w1)
    else:
        w5, w1 = total_cw, 0
    stu = usage.get("server_tool_use")
    searches = _n(stu.get("web_search_requests")) if isinstance(stu, dict) else 0
    return {
        "input": _n(usage.get("input_tokens")),
        "output": _n(usage.get("output_tokens")),
        "cache_write_5m": w5,
        "cache_write_1h": w1,
        "cache_read": _n(usage.get("cache_read_input_tokens")),
        "searches": searches,
    }


def cost_parts(prices, price, tok, usage):
    """Dollar cost of one response split by token type, or None if the model has no price."""
    if price is None:
        return None
    p = {k: price[k] for k in PRICE_KEYS}
    thr = price.get("long_context_threshold_tokens")
    if thr and price.get("long_context"):
        prompt = tok["input"] + tok["cache_read"] + tok["cache_write_5m"] + tok["cache_write_1h"]
        if prompt > thr:
            p = {k: price["long_context"][k] for k in PRICE_KEYS}
    mult = 1.0
    if usage.get("speed") == "fast" and price.get("fast_multiplier"):
        mult *= price["fast_multiplier"]
    if usage.get("inference_geo") == "us":
        mult *= prices.get("us_multiplier", 1.0)
    parts = {k: tok[k] * p[k] / 1e6 * mult for k in PRICE_KEYS}
    parts["searches"] = tok["searches"] * prices.get("web_search_per_request", 0.0)
    return parts


def cost_of(prices, price, tok, usage):
    """Dollar cost of one response, or None if the model has no price."""
    parts = cost_parts(prices, price, tok, usage)
    return None if parts is None else sum(parts.values())


def usage_pieces(obj, msg):
    """[(model, usage)] to price for one response.

    usage.iterations lists every attempt the API made for the request. An
    advisor_message entry is a consultation billed at the advisor model's rates,
    on top of the top-level usage. When more than one non-advisor attempt is
    listed (a fallback), each attempt is billed, so each is priced on its own;
    otherwise the top-level usage is the attempt that answered."""
    usage = msg["usage"]
    model = msg.get("model") or "unknown"
    its = usage.get("iterations")
    if not isinstance(its, list) or not its:
        return [(model, usage)]
    its = [i for i in its if isinstance(i, dict)]
    main = [i for i in its if i.get("type") != "advisor_message"]
    adv = [i for i in its if i.get("type") == "advisor_message"]
    pieces = []
    if len(main) > 1:
        for it in main:
            u = dict(it)
            u.setdefault("speed", usage.get("speed"))
            u.setdefault("inference_geo", usage.get("inference_geo"))
            pieces.append((it.get("model") or model, u))
    else:
        pieces.append((model, usage))
    for it in adv:
        u = dict(it)
        u.setdefault("inference_geo", usage.get("inference_geo"))
        pieces.append((it.get("model") or obj.get("advisorModel") or model, u))
    return pieces


# ---------------------------------------------------------------- reading

def config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.expanduser("~"), ".claude")


def no_transcripts_message(root):
    return ("No Claude Code logs found on this computer.\n"
            "Looked in: {}\n"
            "Claude Code saves its sessions there when it runs on this computer. "
            "Claude Code on the web (claude.ai/code) runs in the cloud, so those sessions "
            "aren't here and can't be counted.\n"
            "If your Claude folder is somewhere else, set CLAUDE_CONFIG_DIR to it."
            ).format(os.path.join(root, "projects"))


def parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone()  # local time zone
    except ValueError:
        return None


def first_text(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                return b["text"]
    return None


def clean_prompt(t):
    for line in t.strip().splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return ""


def prompt_of(o):
    """The first line of a real typed prompt on a user line, or None."""
    if o.get("isMeta") or o.get("isCompactSummary") or o.get("isVisibleInTranscriptOnly"):
        return None
    txt = first_text(o.get("message"))
    if not txt:
        return None
    s = txt.lstrip()
    if s.startswith(("<command-", "<local-command", "<system-reminder", INTERRUPT_MARK)):
        return None
    return clean_prompt(txt) or None


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _agent_files(sub):
    """agent-*.jsonl files anywhere under a subagents folder (os.walk, no glob)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(sub):
        dirnames.sort()
        for n in sorted(filenames):
            if n.startswith("agent-") and n.endswith(".jsonl"):
                out.append(os.path.join(dirpath, n))
    return out


def session_files(root, min_mtime):
    """{(project, session_id): {'main': path or None, 'subs': [paths]}} for sessions
    that have at least one file modified at or after min_mtime. Uses os.listdir and
    os.walk, not glob, so folder names with [ ] * ? characters work."""
    sessions = {}
    base = os.path.join(root, "projects")
    for pname in _listdir(base):
        proj = os.path.join(base, pname)
        if not os.path.isdir(proj):
            continue
        for name in _listdir(proj):
            full = os.path.join(proj, name)
            if name.endswith(".jsonl") and os.path.isfile(full):
                sid = name[:-6]
                sessions.setdefault((pname, sid), {"main": None, "subs": []})["main"] = full
            elif os.path.isdir(full):
                subs = _agent_files(os.path.join(full, "subagents"))
                if subs:
                    sessions.setdefault((pname, name), {"main": None, "subs": []})["subs"].extend(subs)
    keep = {}
    for k, v in sessions.items():
        files = ([v["main"]] if v["main"] else []) + v["subs"]
        try:
            if max(os.path.getmtime(f) for f in files) >= min_mtime:
                keep[k] = v
        except (OSError, ValueError):
            continue
    return keep


def has_transcripts(root):
    """True if at least one .jsonl transcript exists under root/projects."""
    base = os.path.join(root, "projects")
    for pname in _listdir(base):
        proj = os.path.join(base, pname)
        for name in _listdir(proj):
            if name.endswith(".jsonl"):
                return True
            if os.path.isdir(os.path.join(proj, name, "subagents")):
                return True
    return False


def _safe_lines(fh):
    """Yield lines one at a time; stop quietly if the file vanishes mid-read."""
    try:
        for line in fh:
            yield line
    except (OSError, ValueError):
        return


def read_meta(agent_path):
    """The agent-<id>.meta.json next to a subagent transcript, or {}."""
    p = agent_path[:-6] + ".meta.json"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            o = json.load(f)
        return o if isinstance(o, dict) else {}
    except (OSError, ValueError):
        return {}


def scan_history(root, first_day, prices, skip, seen_a):
    """Daily totals only, for the 30, 60 and 90 day averages.

    Reads the sessions that scan() did not read (not in skip) and keeps only what a daily
    total needs: each response's day and cost, and the tally lines. Lines that cannot be
    one of those are skipped without parsing. Responses scan() already counted (seen_a)
    are not counted twice. Returns {"records": [...], "snapshots": {session: [...]}}
    in the shape tally_gaps() reads."""
    start = datetime.combine(first_day, datetime.min.time()).astimezone()
    sessions = session_files(root, start.timestamp() - 86400)
    records = []
    seen = {}               # (message.id, requestId) -> (usage signature, [record indexes])
    snapshots = defaultdict(list)
    for key in sorted(sessions):
        if key in skip:
            continue
        v = sessions[key]
        files = ([v["main"]] if v["main"] else []) + sorted(v["subs"])
        for f in files:
            is_sub = f != v["main"]
            try:
                fh = open(f, "rb", buffering=1 << 20)      # bytes: lines that are skipped are never decoded
            except OSError:
                continue
            last_ts = None
            pend = []       # lines skipped since the last line that gave a timestamp
            with fh:
                for line in _safe_lines(fh):
                    if b'"usage"' not in line and b"cost-state" not in line:
                        pend.append(line)
                        continue
                    try:
                        o = json.loads(line.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(o, dict):
                        continue
                    ts = parse_ts(o.get("timestamp"))
                    if ts is not None:
                        last_ts = ts
                        pend = []
                    t = o.get("type")
                    if t == "cost-state":
                        if is_sub or not isinstance(o.get("totalCostUSD"), (int, float)):
                            continue
                        if ts is None:
                            # the tally is dated by the last line before it that has a timestamp
                            for ln in reversed(pend):
                                try:
                                    po = json.loads(ln.decode("utf-8", errors="replace"))
                                except ValueError:
                                    continue
                                pts = parse_ts(po.get("timestamp")) if isinstance(po, dict) else None
                                if pts is not None:
                                    last_ts = pts
                                    break
                            pend = []
                        snap_ts = last_ts
                        if snap_ts is None and _n(o.get("startTime")):
                            snap_ts = datetime.fromtimestamp((_n(o["startTime"]) + _n(o.get("totalDuration"))) / 1000).astimezone()
                        mu = o.get("modelUsage") if isinstance(o.get("modelUsage"), dict) else {}
                        snapshots[key].append({"ts": snap_ts, "total": float(o["totalCostUSD"]),
                                               "models": {m: _n(u.get("costUSD")) for m, u in mu.items() if isinstance(u, dict)}})
                        continue
                    if t != "assistant" or ts is None:
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                        continue
                    usage = msg["usage"]
                    mid, rid = msg.get("id"), o.get("requestId")
                    k2 = (mid, rid) if mid else None
                    if k2 is not None and k2 in seen_a:
                        continue
                    sig = json.dumps(usage, sort_keys=True)
                    if k2 is not None and k2 in seen:
                        old_sig, old_idx = seen[k2]
                        if old_sig == sig:
                            continue
                        for i in old_idx:
                            records[i]["dead"] = True
                    new_idx = []
                    for model, u in usage_pieces(o, msg):
                        tok = usage_numbers(u)
                        if sum(tok.values()) == 0:
                            continue
                        parts = cost_parts(prices, find_price(prices, model), tok, u)
                        new_idx.append(len(records))
                        records.append({"day": ts.date(), "ts": ts, "model": model, "session": key,
                                        "cost": None if parts is None else sum(parts.values()), "dead": False})
                    if k2 is not None:
                        seen[k2] = (sig, new_idx)
    return {"records": [r for r in records if not r["dead"]], "snapshots": dict(snapshots)}


def scan(root, first_day, last_day, prices, history_days=0):
    """Read transcripts once. Returns raw, deduplicated data for summarize().
    history_days > days read: also read the older days, for daily totals only (scan_history)."""
    start_local = datetime.combine(first_day, datetime.min.time()).astimezone()
    sessions = session_files(root, start_local.timestamp() - 86400)
    records = []            # one per priced piece of a response; dead=True when replaced
    seen = {}               # (message.id, requestId) -> (usage signature, [record indexes])
    meta = {}               # (project, sid) -> dict(start, prompt, mtime)
    agents = {}             # agent file path -> info
    requests = {}           # api-request id -> dict(ts, source, file, index)
    refs = set()            # requestRef values on assistant lines
    interrupts = {}         # file -> sorted line indexes of "[Request interrupted" user lines
    file_lines = {}         # file -> number of lines
    compactions = []        # dict(ts, session, pre_tokens)
    snapshots = defaultdict(list)   # session -> [dict(ts, total, models)]
    q = defaultdict(int)    # counters
    lines_by_day = defaultdict(int)
    zero_by_day = defaultdict(int)
    for key in sorted(sessions):
        v = sessions[key]
        files = ([v["main"]] if v["main"] else []) + sorted(v["subs"])
        info = meta.setdefault(key, {"start": None, "prompt": None, "mtime": 0.0})
        for f in files:
            is_sub = f != v["main"]
            try:
                mtime = os.path.getmtime(f)
                fh = open(f, encoding="utf-8", errors="replace")
            except OSError:
                continue
            q["files"] += 1
            info["mtime"] = max(info["mtime"], mtime)
            ag = None
            if is_sub:
                m = read_meta(f)
                aid = os.path.basename(f)[len("agent-"):-len(".jsonl")]
                ag = agents[f] = {"id": aid, "session": key, "meta": m, "has_meta": bool(m),
                                  "first": None, "last": None, "mtime": mtime, "prompt": None}
            last_ts = None
            idx = -1
            ints = interrupts.setdefault(f, [])
            with fh:
                for idx, line in enumerate(_safe_lines(fh)):
                    try:
                        o = json.loads(line)
                    except ValueError:  # half-written or corrupt line: skip
                        q["bad_lines"] += 1
                        continue
                    if not isinstance(o, dict):
                        continue
                    t = o.get("type")
                    ts = parse_ts(o.get("timestamp"))
                    if ts is not None:
                        last_ts = ts
                        if ag is not None:
                            ag["first"] = ts if ag["first"] is None or ts < ag["first"] else ag["first"]
                            ag["last"] = ts if ag["last"] is None or ts > ag["last"] else ag["last"]
                        elif info["start"] is None or ts < info["start"]:
                            info["start"] = ts
                    if t == "user":
                        txt = first_text(o.get("message"))
                        if txt and txt.lstrip().startswith(INTERRUPT_MARK):
                            ints.append(idx)
                        if ag is not None:
                            if ag["prompt"] is None:
                                ag["prompt"] = prompt_of(o)
                        elif info["prompt"] is None:
                            info["prompt"] = prompt_of(o)
                        continue
                    if t == "api-request":
                        if isinstance(o.get("id"), str):
                            requests[o["id"]] = {"ts": ts, "source": o.get("querySource"), "file": f, "index": idx}
                        continue
                    if t == "system":
                        if o.get("subtype") == "compact_boundary" and ts is not None:
                            cm = o.get("compactMetadata") if isinstance(o.get("compactMetadata"), dict) else {}
                            compactions.append({"ts": ts, "session": key, "pre_tokens": _n(cm.get("preTokens"))})
                        continue
                    if t == "cost-state":
                        if not is_sub and isinstance(o.get("totalCostUSD"), (int, float)):
                            snap_ts = last_ts
                            if snap_ts is None and _n(o.get("startTime")):
                                snap_ts = datetime.fromtimestamp((_n(o["startTime"]) + _n(o.get("totalDuration"))) / 1000).astimezone()
                            mu = o.get("modelUsage") if isinstance(o.get("modelUsage"), dict) else {}
                            snapshots[key].append({"ts": snap_ts, "total": float(o["totalCostUSD"]),
                                                   "models": {m: _n(u.get("costUSD")) for m, u in mu.items() if isinstance(u, dict)}})
                        continue
                    if t != "assistant" or ts is None:
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                        continue
                    if isinstance(o.get("requestRef"), str):
                        refs.add(o["requestRef"])
                    d = ts.date()
                    in_range = first_day <= d <= last_day
                    if in_range:
                        lines_by_day[d] += 1
                    usage = msg["usage"]
                    mid, rid = msg.get("id"), o.get("requestId")
                    sig = json.dumps(usage, sort_keys=True)
                    k2 = (mid, rid) if mid else None
                    if k2 is not None and k2 in seen:
                        old_sig, old_idx = seen[k2]
                        if old_sig == sig:
                            continue
                        # same response written again with different usage: keep the last one
                        q["dupe_conflicts"] += 1
                        for i in old_idx:
                            records[i]["dead"] = True
                    pieces = usage_pieces(o, msg)
                    new_idx = []
                    for n_piece, (model, u) in enumerate(pieces):
                        tok = usage_numbers(u)
                        if sum(tok.values()) == 0:
                            if n_piece == 0 and in_range:
                                zero_by_day[d] += 1
                            continue
                        price = find_price(prices, model)
                        parts = cost_parts(prices, price, tok, u)
                        new_idx.append(len(records))
                        records.append({"day": d, "hour": ts.hour, "ts": ts, "model": model,
                                        "session": key, "is_sub": is_sub, "agent": f if is_sub else None,
                                        "cost": None if parts is None else sum(parts.values()),
                                        "parts": parts, "tok": tok, "resp": 1 if n_piece == 0 else 0,
                                        "dead": False})
                    if k2 is not None:
                        seen[k2] = (sig, new_idx)
            file_lines[f] = idx + 1
    records = [r for r in records if not r["dead"]]
    unmatched = []
    for rid, r in requests.items():
        if rid in refs or r["ts"] is None:
            continue
        nxt = [i for i in interrupts.get(r["file"], []) if r["index"] < i <= r["index"] + 15]
        if nxt:
            why = "interrupted"
        elif file_lines.get(r["file"], 0) - r["index"] <= 12:
            why = "end of file"
        else:
            why = "no response written"
        unmatched.append({"ts": r["ts"], "day": r["ts"].date(), "why": why, "source": r["source"]})
    req_by_day = defaultdict(int)
    for r in requests.values():
        if r["ts"] is not None:
            req_by_day[r["ts"].date()] += 1
    history = None
    if history_days > (last_day - first_day).days + 1:
        history = scan_history(root, last_day - timedelta(days=history_days - 1), prices, set(sessions), seen)
    return {"records": records, "meta": meta, "agents": agents, "lines_by_day": lines_by_day,
            "zero_by_day": zero_by_day, "unmatched": unmatched, "req_by_day": req_by_day,
            "compactions": compactions, "snapshots": dict(snapshots), "counts": dict(q),
            "first_day": first_day, "last_day": last_day, "prices": prices, "history": history}


# ---------------------------------------------------------------- tally gap

def tally_gaps(data):
    """Claude Code's own running tally (cost-state lines, written when a session
    closes) against the usage written to the transcripts.

    Claude Code makes calls it never writes to the transcript: compaction,
    titles, summaries, safety checks, memory and similar. Its tally includes
    them. For each pair of tally snapshots, the gap is the tally's increase
    minus the logged cost between them, split per model and spread over days in
    proportion to the logged cost. Returns per (day, model) gap, per (day,
    session) gap, and the sessions whose usage after their last snapshot has no
    tally yet, per day."""
    by_session = defaultdict(list)
    for r in data["records"]:
        if r["cost"] is not None:
            by_session[r["session"]].append(r)
    gap_dm = defaultdict(float)
    gap_ds = defaultdict(float)
    open_ds = defaultdict(set)
    negative = 0
    for key, recs in by_session.items():
        recs.sort(key=lambda r: r["ts"])
        snaps = [s for s in data["snapshots"].get(key, []) if s["ts"] is not None]
        prev_ts, prev_models = None, {}
        i = 0
        for s in snaps:
            interval = []
            while i < len(recs) and recs[i]["ts"] <= s["ts"]:
                interval.append(recs[i])
                i += 1
            logged_m = defaultdict(float)
            logged_d = defaultdict(float)
            for r in interval:
                logged_m[r["model"]] += r["cost"]
                logged_d[r["day"]] += r["cost"]
            total_logged = sum(logged_d.values())
            for m in set(s["models"]) | set(logged_m):
                g = s["models"].get(m, prev_models.get(m, 0.0)) - prev_models.get(m, 0.0) - logged_m.get(m, 0.0)
                if abs(g) < 1e-9:
                    continue
                if g < 0:
                    negative += 1
                if total_logged > 0:
                    for d, c in logged_d.items():
                        gap_dm[(d, m)] += g * c / total_logged
                        gap_ds[(d, key)] += g * c / total_logged
                else:
                    gap_dm[(s["ts"].date(), m)] += g
                    gap_ds[(s["ts"].date(), key)] += g
            prev_ts, prev_models = s["ts"], dict(s["models"])
        for r in recs[i:]:
            open_ds[r["day"]].add(key)
    return {"day_model": gap_dm, "day_session": gap_ds, "open": open_ds, "negative": negative}


# ---------------------------------------------------------------- summary

def _blank_tok():
    return {k: 0 for k in TOKEN_KEYS}


def _add_tok(a, b):
    for k in TOKEN_KEYS:
        a[k] += b[k]


def _iso(d):
    return d.isoformat(timespec="seconds") if isinstance(d, datetime) else (d.isoformat() if d else None)


# ---------------------------------------------------------------- tldr

MIN_TLDR_TOTAL = 1.0      # under this many dollars the day gets an empty state
MIN_SAVING = 0.10         # a way to spend less is listed only when it saves about this much
REREAD_SHARE = 0.25       # re-reading must be this share of the day's cost
REREAD_CUT = 0.5          # a fresh session or earlier compact is assumed to cut this share of a long session's re-reading
INFO_SHARE = 0.30         # top subagents as a share of the day, or calls not in transcripts as a share (0.10)


def _short(m):
    return model_name(m)


def _sonnet_price(prices):
    keys = sorted(k for k in prices.get("models", {}) if "sonnet" in k)
    return prices["models"][keys[-1]] if keys else None, (keys[-1] if keys else None)


# TLDR wording. TLDR_TEXT is what the terminal and --json show; TLDR_PAGE is the page's.
# Both run the same rules over the same numbers, so every figure matches.
TLDR_TEXT = {
    "empty": "Too little spend for a summary (under $1.00).",
    "phrases": COST_PHRASES,
    "top": "{:.0f}% of {} cost was {}.",
    "model": "{} was {:.0f}% of the cost.",
    "model_subs": " {} of {} used it.", "sub_one": "helper agent",
    "subs": "{} were {:.0f}% of the cost: {} across {}.", "subs_who": "Helper agents", "subs_unit": "agent",
    "biggest": "Your biggest session cost {}, {:.0f}% of the day.",
    "yours_one": "Your longest session", "yours_many": "Your {} longest sessions",
    "reread": "Start a new session when you switch topics, or type /compact to shrink a long one. "
              "{} {} the whole chat on every turn.",
    "reread_basis": "Re-reading in {} cost {}. A fresh start is assumed to cut about {:.0f}% of that: {} x {:.2f} = {}.",
    "opus": "Use Sonnet, not Opus, for routine helper agents. {} ran on Opus. Biggest: {}.", "opus_unit": "helper",
    "opus_unit_name": "agent",
    "opus_basis": "Helper agents on Opus cost {}. The same work at {} prices: {}. {} - {} = {}.",
    "hour": "Use short-term memory (5 min) instead of longer memory (1 hour), "
            "unless you often pause for more than 5 minutes. Longer memory costs more to save.",
    "hour_basis": "Saving to longer memory cost {}. The same at the 5-minute rate: {}. {} - {} = {}.",
    "check": "Check if these helper agents needed to run: {}.",
    "check_basis": "Your 3 costliest helper agents cost {}, {:.0f}% of the day. No saving estimated.",
    "background": "Hidden background calls (like shrinking long chats) were {:.0f}% of the total. "
                  "Fewer long sessions means fewer of them.",
    "background_basis": "Claude Code's own running total shows {} not in its logs, {:.0f}% of {}. No saving estimated.",
    "mean_reread": "Most of your cost is Claude re-reading the chat on every turn. Long sessions cost more than long replies.",
    "mean_output": "Most of your cost is Claude's replies. The model you pick and how much each agent writes matter most.",
    "mean_other": "Your biggest cost is {}.",
    "saved": "The tips below could have saved about {} of {} {}.",
    "none_saved": "No savings found in {} pattern.",
}
TLDR_PAGE = {
    "empty": "Not enough spend for a TLDR. It needs at least $1.00.",
    "phrases": {"output": "Claude's output", "cache_read": "Claude re-reading the session",
                "cache_write_5m": "caching for 5 minutes", "cache_write_1h": "caching for 1 hour",
                "input": "new input", "searches": "web searches", "hidden": "background calls"},
    "top": "{:.0f}% of {} cost was {}.",
    "model": "{} was {:.0f}% of the cost.",
    "model_subs": " {} of {} used it.", "sub_one": "subagent",
    "subs": "{} were {:.0f}% of the cost: {} across {}.", "subs_who": "Subagents", "subs_unit": "subagent",
    "biggest": "Your biggest session cost {}, {:.0f}% of the day.",
    "yours_one": "Your longest session", "yours_many": "Your {} longest sessions",
    "reread": "Start a new session when you switch topics, or type /compact to shrink a long one. "
              "{} {} everything on every turn.",
    "reread_basis": "Re-reading in {} cost {}. Starting fresh is assumed to cut that by about {:.0f}%: {} x {:.2f} = {}.",
    "opus": "Use Sonnet, not Opus, for routine subagents. {} ran on Opus. Biggest: {}.", "opus_unit": "subagent",
    "opus_unit_name": "subagent",
    "opus_basis": "Subagents on Opus cost {}. The same work at {} prices: {}. {} - {} = {}.",
    "hour": "Cache for 5 minutes instead of 1 hour, unless you often pause for more than 5 minutes. "
            "Caching for 1 hour costs more.",
    "hour_basis": "Caching for 1 hour cost {}. The same at the 5-minute rate: {}. {} - {} = {}.",
    "check": "Check whether these subagents needed to run: {}.",
    "check_basis": "Your 3 costliest subagents cost {}, {:.0f}% of the day. No savings estimated.",
    "background": "Background calls (like summarizing long sessions) were {:.0f}% of the total. "
                  "Fewer long sessions means fewer of them.",
    "background_basis": "Claude Code's own running total shows {} that isn't in its logs, {:.0f}% of {}. No savings estimated.",
    "mean_reread": "Most of your cost is Claude re-reading the session on every turn. Long sessions cost more than long replies.",
    "mean_output": "Most of your cost is Claude's output. The model you pick and how much each agent writes matter most.",
    "mean_other": "Your biggest cost is {}.",
    "saved": "The tips below could have saved about {} of {} {}.",
    "none_saved": "No savings found in {} sessions.",
}


def build_tldr(data, day_recs, token_types, model_rows, sub_rows, sess_rows, total, quality, day="today's", w=None):
    """Findings, meaning and up to three ways to spend less, from fixed rules over the day's numbers.
    No model call, no network. Savings are estimates at the prices in prices.json.
    w is the wording: TLDR_TEXT (the default) or TLDR_PAGE."""
    w = w or TLDR_TEXT
    if total < MIN_TLDR_TOTAL or not sess_rows:
        return {"empty": True, "findings": [],
                "meaning": w["empty"],
                "recommendations": []}
    prices = data["prices"]
    pct = lambda v: 100.0 * v / total

    # findings
    findings = []
    top = max(token_types, key=lambda t: t["cost"])
    if top["cost"] > 0:
        findings.append(w["top"].format(top["percent"], day, w["phrases"][top["key"]]))
    priced = [m for m in model_rows if m["priced"] and m["cost"] > 0]
    if priced:
        tm = priced[0]
        line = w["model"].format(_short(tm["model"]), pct(tm["cost"]))
        if sub_rows:
            on = sum(1 for r in sub_rows if tm["model"] in r["models"])
            line += w["model_subs"].format(on, plural(len(sub_rows), w["sub_one"]))
        findings.append(line)
    sub_total = sum(r["cost"] for r in sub_rows)
    if sub_rows and pct(sub_total) >= 10:
        findings.append(w["subs"].format(
            w["subs_who"], pct(sub_total), money(sub_total), plural(len(sub_rows), w["subs_unit"])))
    else:
        b = sess_rows[0]
        findings.append(w["biggest"].format(money(b["total"]), pct(b["total"])))

    # ways to spend less
    recs = []
    ptype = {t["key"]: t for t in token_types}

    # a. long sessions re-reading their history
    cr = ptype["cache_read"]["cost"]
    if cr / total >= REREAD_SHARE:
        per = defaultdict(float)
        for r in day_recs:
            if not r["is_sub"] and r["parts"] is not None:
                per[r["session"]] += r["parts"]["cache_read"]
        longest = sorted(per.items(), key=lambda kv: -kv[1])[:3]
        base = sum(v for _, v in longest)
        sv = base * REREAD_CUT
        if sv >= MIN_SAVING:
            yours = w["yours_one"] if len(longest) == 1 else w["yours_many"].format(len(longest))
            recs.append({"action": w["reread"].format(yours, "re-reads" if len(longest) == 1 else "re-read"),
                         "saving_usd": sv,
                         "basis": w["reread_basis"].format(
                             yours.lower(), money(base), 100 * REREAD_CUT, money(base), REREAD_CUT, money(sv))})

    # b. Opus subagents priced at Sonnet
    sp, sname = _sonnet_price(prices)
    if sp:
        by_agent = defaultdict(lambda: [0.0, 0.0])
        for r in day_recs:
            if r["is_sub"] and r["cost"] is not None and "opus" in r["model"]:
                at_sonnet = sum(r["tok"][k] * sp[k] / 1e6 for k in PRICE_KEYS) + r["parts"]["searches"]
                by_agent[r["agent"]][0] += r["cost"]
                by_agent[r["agent"]][1] += at_sonnet
        if by_agent:
            ids = {r["id"]: r["name"] for r in sub_rows}
            rows = sorted(((a, v[0], v[1]) for a, v in by_agent.items()), key=lambda x: -(x[1] - x[2]))
            opus_cost = sum(x[1] for x in rows)
            son_cost = sum(x[2] for x in rows)
            sv = opus_cost - son_cost
            if sv >= MIN_SAVING:
                names = ", ".join("{} ({})".format(ids.get((data["agents"].get(a) or {}).get("id"), w["opus_unit_name"]), money(c))
                                  for a, c, _ in rows[:3])
                recs.append({"action": w["opus"].format(plural(len(rows), w["opus_unit"]), names),
                             "saving_usd": sv,
                             "basis": w["opus_basis"].format(
                                 money(opus_cost), _short(sname), money(son_cost), money(opus_cost), money(son_cost), money(sv))})

    # c. 1-hour cache writes at the 5-minute rate
    h1 = 0.0
    h5 = 0.0
    for r in day_recs:
        if r["parts"] is None or not r["tok"]["cache_write_1h"]:
            continue
        pr = find_price(prices, r["model"])
        if not pr or not pr["cache_write_1h"]:
            continue
        h1 += r["parts"]["cache_write_1h"]
        h5 += r["parts"]["cache_write_1h"] * pr["cache_write_5m"] / pr["cache_write_1h"]
    if h1 - h5 >= MIN_SAVING:
        recs.append({"action": w["hour"],
                     "saving_usd": h1 - h5,
                     "basis": w["hour_basis"].format(money(h1), money(h5), money(h1), money(h5), money(h1 - h5))})

    recs.sort(key=lambda r: -r["saving_usd"])
    # d and e carry no saving estimate; they fill open places only
    info = []
    tops = [r for r in sub_rows[:3] if r["cost"] > 0]
    tcost = sum(r["cost"] for r in tops)
    if len(sub_rows) >= 3 and tcost / total >= INFO_SHARE and tcost >= 1.0:
        info.append({"action": w["check"].format(
                         ", ".join("{} ({})".format(r["name"], money(r["cost"])) for r in tops)),
                     "saving_usd": 0.0,
                     "basis": w["check_basis"].format(money(tcost), pct(tcost))})
    nl = quality["not_logged"] or 0.0
    if nl / total >= 0.10:
        info.append({"action": w["background"].format(pct(nl)),
                     "saving_usd": 0.0,
                     "basis": w["background_basis"].format(money(nl), pct(nl), money(total))})
    recs = (recs + info)[:3]
    saved = sum(r["saving_usd"] for r in recs)

    # meaning
    if top["key"] == "cache_read" and top["percent"] >= 40:
        m1 = w["mean_reread"]
    elif top["key"] == "output" and top["percent"] >= 40:
        m1 = w["mean_output"]
    else:
        m1 = w["mean_other"].format(w["phrases"][top["key"]])
    m2 = (w["saved"].format(money(saved), day, money(total))
          if saved > 0 else w["none_saved"].format(day))
    return {"empty": False, "findings": findings, "meaning": m1 + " " + m2, "recommendations": recs}



def window_average(data, gaps, target, days=AVG_DAYS, now=None):
    """The page's 30-day comparison figures. The window is the `days` full days before target
    (target itself is not in it). `average` is the cost per day over those days, background calls
    included, zero days counted. `by_now` is the average over the same days of each day's logged
    cost from midnight up to the local time of day of `now` (a datetime), background calls left out
    because they are only known when a session closes; None when now is not given.
    `target_logged` is target's own logged cost. Both averages are None (the page says how many more
    days it needs) unless the logs reach back to the first day of the window: the first day with
    logged usage must be on or before it."""
    hist = data.get("history")
    recs = data["records"] + (hist["records"] if hist else [])
    start = target - timedelta(days=days)
    cut = None if now is None else now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    logged = defaultdict(float)
    by_now = 0.0
    target_logged = 0.0
    first = None
    for r in recs:
        d = r["day"]
        if d > target:
            continue
        if first is None or d < first:
            first = d
        c = r["cost"]
        if c is None:
            continue
        if d == target:
            target_logged += c
        elif d >= start:
            logged[d] += c
            if cut is not None:
                t = r["ts"]
                if t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6 <= cut:
                    by_now += c
    hidden = defaultdict(float)
    for (d, _m), v in gaps["day_model"].items():
        hidden[d] += v
    if hist:
        hg = tally_gaps({"records": hist["records"], "snapshots": hist["snapshots"]})
        for (d, _m), v in hg["day_model"].items():
            hidden[d] += v
    value = None
    if first is not None and first <= start:
        value = sum(logged[start + timedelta(days=i)] + hidden[start + timedelta(days=i)] for i in range(days)) / days
    needed = 0 if value is not None else ((first - start).days if first is not None else days)
    return {"days": days, "average": value, "first_day": first.isoformat() if first else None, "needed": needed,
            "by_now": (by_now / days) if (value is not None and cut is not None) else None,
            "target_logged": target_logged, "by_time": clock(now) if now is not None else None}


def summarize(data, target, ndays=7, now=None):
    """Everything the terminal, the page and --json show, as plain JSON-ready data."""
    now = now or time.time()
    recs = data["records"]
    first = target - timedelta(days=ndays - 1)
    window = [first + timedelta(days=i) for i in range(ndays)]
    day_recs = [r for r in recs if r["day"] == target]
    win_recs = [r for r in recs if first <= r["day"] <= target]
    gaps = tally_gaps(data)

    # days
    days = []
    for d in window:
        bym = defaultdict(float)
        for r in win_recs:
            if r["day"] == d and r["cost"] is not None:
                bym[r["model"]] += r["cost"]
        nl = sum(v for (dd, m), v in gaps["day_model"].items() if dd == d)
        days.append({"date": d.isoformat(), "logged": sum(bym.values()), "total": sum(bym.values()) + nl,
                     "by_model": dict(bym), "not_logged": nl, "open_sessions": len(gaps["open"].get(d, ()))})
    total = days[-1]["total"]
    logged_total = days[-1]["logged"]
    yday = target - timedelta(days=1)
    yesterday_total = (sum(r["cost"] for r in recs if r["day"] == yday and r["cost"] is not None)
                       + sum(v for (dd, m), v in gaps["day_model"].items() if dd == yday))

    # models: every model seen in the window, or in a tally for the window
    models = {}

    def mrow(name):
        return models.setdefault(name, {"model": name, "priced": True, "cost": 0.0, "responses": 0,
                                        "tokens": _blank_tok(), "window_cost": 0.0, "window_responses": 0,
                                        "logged_cost": 0.0, "window_logged_cost": 0.0,
                                        "not_logged": 0.0, "window_not_logged": 0.0})
    for r in win_recs:
        m = mrow(r["model"])
        if r["cost"] is None:
            m["priced"] = False
        else:
            m["window_logged_cost"] += r["cost"]
        m["window_responses"] += r["resp"]
        if r["day"] == target:
            m["responses"] += r["resp"]
            _add_tok(m["tokens"], r["tok"])
            if r["cost"] is not None:
                m["logged_cost"] += r["cost"]
    for (d, name), g in gaps["day_model"].items():
        if first <= d <= target:
            m = mrow(name)
            if not win_recs or name not in {r["model"] for r in win_recs}:
                m["priced"] = find_price(data["prices"], name) is not None
            m["window_not_logged"] += g
            if d == target:
                m["not_logged"] += g
    for m in models.values():
        m["cost"] = m["logged_cost"] + m["not_logged"]
        m["window_cost"] = m["window_logged_cost"] + m["window_not_logged"]
    model_rows = sorted(models.values(), key=lambda m: (-m["cost"], -m["window_cost"], m["model"]))

    # where the money went
    parts = {k: 0.0 for k, _, _ in COST_TYPES}
    toks = _blank_tok()
    for r in day_recs:
        _add_tok(toks, r["tok"])
        if r["parts"] is not None:
            for k in parts:
                parts[k] += r["parts"][k]
    token_types = []
    for k, label, explain in COST_TYPES:
        token_types.append({"key": k, "label": label, "explain": explain, "tokens": toks[k],
                            "cost": parts[k], "percent": (100.0 * parts[k] / total) if total else 0.0})
    hk, hlabel, hexplain = HIDDEN_ROW
    hidden_day = days[-1]["not_logged"]
    token_types.append({"key": hk, "label": hlabel, "explain": hexplain, "tokens": None,
                        "cost": hidden_day, "percent": (100.0 * hidden_day / total) if total else 0.0})

    # hours
    hours = []
    for h in range(24):
        bym = defaultdict(float)
        for r in day_recs:
            if r["hour"] == h and r["cost"] is not None:
                bym[r["model"]] += r["cost"]
        hours.append({"hour": h, "total": sum(bym.values()), "by_model": dict(bym)})

    # subagents
    subs = {}
    for r in day_recs:
        if not r["is_sub"]:
            continue
        a = data["agents"].get(r["agent"], {})
        s = subs.setdefault(r["agent"], {"agent": r["agent"], "cost": 0.0, "responses": 0, "tokens": _blank_tok(),
                                         "models": defaultdict(float), "unpriced": 0, "session": r["session"], "a": a})
        if r["cost"] is None:
            s["unpriced"] += r["resp"]
        else:
            s["cost"] += r["cost"]
        s["models"][r["model"]] += r["cost"] or 0.0
        s["responses"] += r["resp"]
        _add_tok(s["tokens"], r["tok"])
    sub_rows = []
    for s in subs.values():
        a = s["a"]
        m = a.get("meta") or {}
        name = m.get("description") if isinstance(m.get("description"), str) and m.get("description").strip() else None
        start, end = a.get("first"), a.get("last")
        if m.get("finished") is True:
            status = "finished"
        elif now - a.get("mtime", 0) <= RUNNING_SECONDS:
            status = "running"
        else:
            status = "stopped"
        sinfo = data["meta"].get(s["session"], {})
        sub_rows.append({
            "name": name or (a.get("prompt") or "agent-" + str(a.get("id", ""))),
            "named": bool(name), "id": a.get("id"), "type": m.get("agentType"),
            "requested_model": m.get("model"),
            "models": [k for k, _ in sorted(s["models"].items(), key=lambda kv: -kv[1])],
            "project": s["session"][0], "session": s["session"][1],
            "session_prompt": sinfo.get("prompt") or "",
            "cost": s["cost"], "unpriced_responses": s["unpriced"], "tokens": s["tokens"],
            "responses": s["responses"], "start": _iso(start), "end": _iso(end),
            "duration_s": int((end - start).total_seconds()) if start and end else None,
            "status": status, "has_meta": a.get("has_meta", False)})
    sub_rows.sort(key=lambda r: (-r["cost"], r["name"]))

    # sessions
    sess = {}
    for r in day_recs:
        s = sess.setdefault(r["session"], {"main": 0.0, "sub": 0.0, "unpriced": 0, "agents": set(),
                                           "models": defaultdict(float)})
        if r["is_sub"]:
            s["agents"].add(r["agent"])
        s["models"][r["model"]] += r["cost"] or 0.0
        if r["cost"] is None:
            s["unpriced"] += r["resp"]
            continue
        s["sub" if r["is_sub"] else "main"] += r["cost"]
    sess_rows = []
    for key, v in sess.items():
        info = data["meta"].get(key, {})
        is_open = key in gaps["open"].get(target, ())
        g = gaps["day_session"].get((target, key))
        sess_rows.append({
            "project": key[0], "session": key[1], "start": _iso(info.get("start")),
            "prompt": info.get("prompt") or "", "main": v["main"], "sub": v["sub"],
            "logged": v["main"] + v["sub"], "total": v["main"] + v["sub"] + (g or 0.0),
            "unpriced_responses": v["unpriced"],
            "subagents": len(v["agents"]),
            "models": [k for k, _ in sorted(v["models"].items(), key=lambda kv: -kv[1])],
            "running": target == date.today() and (now - info.get("mtime", 0)) <= RUNNING_SECONDS,
            "not_logged": None if (is_open and not g) else (g or 0.0),
            "tally": "open" if is_open else "closed"})
    sess_rows.sort(key=lambda r: (-r["total"], r["session"]))

    # projects
    proj = defaultdict(lambda: {"logged": 0.0, "hidden": 0.0, "sessions": set(), "subagents": set()})
    for r in day_recs:
        p = proj[r["session"][0]]
        p["sessions"].add(r["session"][1])
        if r["is_sub"]:
            p["subagents"].add(r["agent"])
        if r["cost"] is not None:
            p["logged"] += r["cost"]
    for (d, key), g in gaps["day_session"].items():
        if d == target and key[0] in proj:
            proj[key[0]]["hidden"] += g
    proj_rows = sorted(({"project": k, "cost": v["logged"] + v["hidden"], "logged": v["logged"],
                         "not_logged": v["hidden"], "sessions": len(v["sessions"]),
                         "subagents": len(v["subagents"])} for k, v in proj.items()),
                       key=lambda r: (-r["cost"], r["project"]))

    # chart series: the biggest models over the window keep their own colour
    order = sorted(((m["window_logged_cost"], m["model"]) for m in model_rows if m["window_logged_cost"] > 0), reverse=True)
    series = [name for _, name in order[:CHART_SERIES]]

    # data quality
    um_day = [u for u in data["unmatched"] if u["day"] == target]
    why = defaultdict(int)
    for u in um_day:
        why[u["why"]] += 1
    comp_day = [c for c in data["compactions"] if c["ts"].date() == target]
    pr = data["prices"]
    quality = {
        "files_read": data["counts"].get("files", 0),
        "unreadable_lines": data["counts"].get("bad_lines", 0),
        "assistant_lines": data["lines_by_day"].get(target, 0),
        "responses": sum(r["resp"] for r in day_recs),
        "zero_usage_skipped": data["zero_by_day"].get(target, 0),
        "dupe_conflicts": data["counts"].get("dupe_conflicts", 0),
        "requests_logged": data["req_by_day"].get(target, 0),
        "requests_no_usage": len(um_day),
        "requests_no_usage_why": dict(why),
        "requests_no_usage_by_day": {d.isoformat(): sum(1 for u in data["unmatched"] if u["day"] == d) for d in window},
        "compactions": len(comp_day),
        "compacted_tokens": sum(c["pre_tokens"] for c in comp_day),
        "not_logged": days[-1]["not_logged"],
        "open_sessions": days[-1]["open_sessions"],
        "negative_tally_intervals": gaps["negative"],
        "unpriced_models": [m["model"] for m in model_rows if not m["priced"]],
        "logged_total": logged_total,
        "prices_date": pr.get("date_read"), "prices_age_days": price_age_days(pr),
        "prices_source": pr.get("source_url"),
    }
    day_word = "today's" if target == date.today() else "the day's"
    return {
        "date": target.isoformat(), "generated": _iso(datetime.fromtimestamp(now).astimezone()),
        "estimate_line": ESTIMATE_LINE, "days_shown": ndays,
        "total": total, "logged_total": logged_total, "not_logged": days[-1]["not_logged"],
        "open_sessions": days[-1]["open_sessions"], "yesterday": yesterday_total, "change": total - yesterday_total,
        "average": sum(d["total"] for d in days) / len(days),
        "running_sessions": sum(1 for r in sess_rows if r["running"]),
        "token_types": token_types, "models": model_rows, "sessions": sess_rows,
        "subagents": sub_rows, "projects": proj_rows, "hours": hours, "days": days,
        "chart_series": series, "quality": quality,
        "tldr": build_tldr(data, day_recs, token_types, model_rows, sub_rows, sess_rows, total, quality, day_word),
        # the same TLDR in the page's words; render_html uses it, --json leaves it out
        PAGE_TLDR_KEY: build_tldr(data, day_recs, token_types, model_rows, sub_rows, sess_rows, total, quality,
                                  day_word, TLDR_PAGE),
        PAGE_AVG_KEY: window_average(data, gaps, target, now=datetime.fromtimestamp(now)),
    }


# ---------------------------------------------------------------- formatting

def money(x):
    return "${:,.2f}".format(x)


def signed_money(x):
    return ("+" if x >= 0 else "-") + money(abs(x))


def num(x):
    return "{:,}".format(int(x))


def short_num(x):
    x = float(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return "{:.1f}{}".format(x / div, suf)
    return str(int(x))


def plural(n, word, many=None):
    """'1 session', '2 sessions'."""
    return "{} {}".format(num(n), word if n == 1 else (many or word + "s"))


def model_name(m):
    """'claude-opus-5-5' -> 'Opus 5.5'; 'claude-3-5-sonnet-20241022' -> '3.5 Sonnet'."""
    if not m:
        return ""
    s = re.sub(r"-\d{8}$", "", m)
    s = s[len("claude-"):] if s.startswith("claude-") else s
    out, digits = [], []
    for part in s.split("-"):
        if part.isdigit():
            digits.append(part)
            continue
        if digits:
            out.append(".".join(digits))
            digits = []
        out.append(part[:1].upper() + part[1:])
    if digits:
        out.append(".".join(digits))
    return " ".join(x for x in out if x) or m


def models_text(ms):
    return ", ".join(model_name(m) for m in ms)


def clock(dt):
    """'12:05 am'."""
    h = dt.hour % 12 or 12
    return "{}:{:02d} {}".format(h, dt.minute, "am" if dt.hour < 12 else "pm")


def hhmm(s):
    return clock(datetime.fromisoformat(s)) if s else "--"


def time_range(a, b):
    """'11:30-11:53 pm', or '11:50 am-12:10 pm' across noon."""
    if not a or not b:
        return hhmm(a or b)
    x, y = hhmm(a), hhmm(b)
    if x[-2:] == y[-2:]:
        return x[:-3] + "-" + y
    return x + "-" + y


def when(s, target):
    if not s:
        return ""
    d = datetime.fromisoformat(s)
    if d.date().isoformat() == target:
        return clock(d)
    return "{} {}, {}".format(d.strftime("%b"), d.day, clock(d))


def day_name(iso, today=None):
    """'Today, Nov 16', 'Yesterday, Nov 15', 'Fri, Nov 14', with the year when it is not this year."""
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    today = today or date.today()
    md = "{} {}".format(d.strftime("%b"), d.day)
    if d == today:
        return "Today, " + md
    if d == today - timedelta(days=1):
        return "Yesterday, " + md
    return "{}, {}{}".format(d.strftime("%a"), md, "" if d.year == today.year else ", {}".format(d.year))


def plain_date(iso):
    """'2026-11-15' -> 'Nov 15, 2026'; anything else as is."""
    try:
        d = datetime.strptime(str(iso), "%Y-%m-%d")
    except ValueError:
        return str(iso)
    return "{} {}, {}".format(d.strftime("%b"), d.day, d.year)


def plain_md(iso):
    """'2026-10-20' -> 'Oct 20'."""
    d = datetime.strptime(iso, "%Y-%m-%d")
    return "{} {}".format(d.strftime("%b"), d.day)


def ago(days):
    if days is None:
        return "date unknown"
    return "today" if days == 0 else plural(days, "day") + " ago"


def change_text(x, vs):
    """'$137.63 less', '$12.00 more', 'same'."""
    if abs(x) < 0.005:
        return "Same as " + vs
    return "{} {} than {}".format(money(abs(x)), "more" if x > 0 else "less", vs)


def tok_short(x):
    """Shortened token count for display; under 10,000 shown in full."""
    return num(x) if abs(x) < 10000 else short_num(x)


STATUS_WORDS = {"finished": "done", "running": "running", "stopped": "stopped"}


def duration(sec):
    if sec is None:
        return ""
    h, m = divmod(int(sec) // 60, 60)
    return "{}h {:02d}m".format(h, m) if h else "{}m".format(m)


def cache_writes(t):
    return t["cache_write_5m"] + t["cache_write_1h"]


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 3] + "..."


def why_text(why):
    names = {"interrupted": "stopped by you", "end of file": "cut off at the end of a session, or still running",
             "no response written": "no answer saved"}
    return ", ".join("{} {}".format(v, names.get(k, k)) for k, v in sorted(why.items()))


# ---------------------------------------------------------------- terminal

def _table(head, rows, right):
    """Aligned plain-text table. right: set of column indexes to right-align."""
    widths = [len(h) for h in head]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    out = []
    for r in [head] + rows:
        cells = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(r)]
        out.append("  " + "  ".join(cells).rstrip())
    return out


def tldr_text(t):
    o = ["", "  TLDR"]
    if t["empty"]:
        o.append("    " + t["meaning"])
        o.append("")
        return o
    o.append("    What happened")
    o.extend("      - " + f for f in t["findings"])
    o.append("    What it means")
    o.append("      " + t["meaning"])
    o.append("    " + tips_heading(len(t["recommendations"])))
    if not t["recommendations"]:
        o.append("      Nothing to suggest for this day.")
    for i, r in enumerate(t["recommendations"], 1):
        tail = " (could save about {})".format(money(r["saving_usd"])) if r["saving_usd"] > 0 else ""
        o.append("      {}. {}{}".format(i, r["action"], tail))
    o.append("")
    return o


def tips_heading(n):
    if n == 1:
        return "Best way to spend less"
    return "Top {} ways to spend less".format(n) if n else "Ways to spend less"


def open_text(n):
    """The one plain line about sessions whose hidden calls aren't known yet."""
    if n == 1:
        return "1 session is still open; its background calls get added when it closes."
    return "{} are still open; their background calls get added when they close.".format(plural(n, "session"))


def vs_word(s):
    return "yesterday" if s["date"] == date.today().isoformat() else "the day before"


def render_text(s, warning=None):
    o = []
    q = s["quality"]
    o.append("Claude Code spend: {}: {}".format(day_name(s["date"]), money(s["total"])))
    o.append("  {}   Daily average (last {} days) {}   Updated {}".format(
        change_text(s["change"], vs_word(s)), s["days_shown"], money(s["average"]), hhmm(s["generated"])))
    if q["not_logged"]:
        o.append("  Includes {} of hidden background calls (logged {}).".format(money(q["not_logged"]), money(s["logged_total"])))
    if q["open_sessions"]:
        o.append("  " + open_text(q["open_sessions"]))
    o.extend(tldr_text(s["tldr"]))
    if q["not_logged"] or q["open_sessions"]:
        o.append("  " + HIDDEN_LINE)
    o.append(ESTIMATE_LINE)
    if warning:
        o.append("Warning: " + warning)

    o.append("")
    o.append("Where the money went  (" + TOKEN_LINE + ")")
    o += _table(["", "Tokens", "Cost", "Share"],
                [[t["label"], "--" if t["tokens"] is None else
                  (plural(t["tokens"], "search", "searches") if t["key"] == "searches" else num(t["tokens"])),
                  money(t["cost"]), "{:.1f}%".format(t["percent"])]
                 for t in s["token_types"]], {1, 2, 3})

    o.append("")
    o.append("By model  (token counts are for this day only; costs include hidden background calls)")
    rows = []
    for m in s["models"]:
        t = m["tokens"]
        rows.append([model_name(m["model"]), money(m["cost"]) if m["priced"] else "no price", num(m["responses"]),
                     short_num(t["output"]), short_num(t["cache_read"]), short_num(cache_writes(t)),
                     short_num(t["input"]), money(m["window_cost"]) if m["priced"] else "no price",
                     money(m["window_not_logged"]) if m["window_not_logged"] else ""])
    o += _table(["Model", "Cost", "Answers", "Replies", "Re-read", "Memory", "New text",
                 "{}-day cost".format(s["days_shown"]), "Hidden calls"], rows, {1, 2, 3, 4, 5, 6, 7, 8})

    o.append("")
    o.append("Biggest sessions  (costliest first, helper agents included)")
    rows = []
    for r in s["sessions"][:TOP_SESSIONS]:
        rows.append([money(r["total"]), money(r["main"]), money(r["sub"]),
                     "not yet" if r["not_logged"] is None else (money(r["not_logged"]) if r["not_logged"] else ""),
                     str(r["subagents"]),
                     when(r["start"], s["date"]), cut(r["project"], 24), models_text(r["models"]),
                     cut(r["prompt"], 48) + ("  [running]" if r["running"] else "")])
    o += _table(["Total", "Main chat", "Helpers", "Hidden", "Agents", "Started", "Project", "Models", "First message"],
                rows, {0, 1, 2, 3, 4})

    o.append("")
    o.append("Biggest helper agents  (subagents: extra agents a session starts for a side job)")
    rows = []
    for r in s["subagents"][:TOP_SUBAGENTS]:
        rows.append([money(r["cost"]), cut(r["name"], 34), models_text(r["models"]),
                     num(r["responses"]), short_num(sum(r["tokens"][k] for k in PRICE_KEYS)),
                     time_range(r["start"], r["end"]), duration(r["duration_s"]),
                     STATUS_WORDS.get(r["status"], r["status"]), r["session"][:8]])
    o += _table(["Cost", "Name", "Model", "Answers", "Tokens", "Time", "Took", "Status", "Session"], rows, {0, 3, 4, 6})
    if not s["subagents"]:
        o.append("  none on this day")

    o.append("")
    o.append("By project")
    o += _table(["Cost", "Sessions", "Helpers", "Project folder"],
                [[money(p["cost"]), str(p["sessions"]), str(p["subagents"]), p["project"]] for p in s["projects"]],
                {0, 1, 2})

    o.append("")
    o.append("By hour  (your local time; hidden background calls aren't tied to an hour, so they aren't here)")
    series = s["chart_series"]
    short = [model_name(m) for m in series]

    def split(by_model):
        other = sum(v for m, v in by_model.items() if m not in series)
        return [money(by_model.get(m, 0.0)) for m in series] + [money(other)]
    hrs = [h for h in s["hours"] if h["total"] > 0]
    o += _table(["Hour", "Cost"] + short + ["Other"],
                [[hour_name(h["hour"]), money(h["total"])] + split(h["by_model"]) for h in hrs],
                set(range(1, len(series) + 3)))

    o.append("")
    o.append("Last {}".format(plural(s["days_shown"], "day")))
    o += _table(["Day", "Cost"] + short + ["Other", "Hidden calls", "Still open"],
                [[datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %b %d").replace(" 0", " "), money(d["total"])] + split(d["by_model"])
                 + [money(d["not_logged"]) if d["not_logged"] else "", str(d["open_sessions"]) if d["open_sessions"] else ""]
                 for d in s["days"]], set(range(1, len(series) + 5)))

    o.append("")
    o.append("How this was counted")
    for k, v, _ in counted_rows(q):
        o.append("  {}: {}".format(k, v))
    return "\n".join(o)


def hour_name(h):
    """0 -> '12 am', 13 -> '1 pm'."""
    return "{} {}".format(h % 12 or 12, "am" if h < 12 else "pm")


def counted_rows(q):
    """(label, value, tooltip) rows for 'How this was counted', shared by the terminal and the page."""
    rows = [
        ("Log files read", num(q["files_read"]),
         "Claude Code's saved session files that were read for this page."),
        ("Claude reply lines read", num(q["assistant_lines"]),
         "Lines in the logs where Claude answered, on this day."),
        ("Answers counted", "{} (repeats removed)".format(num(q["responses"])),
         "The same answer can be saved more than once; each is counted once."),
        ("Empty lines skipped", num(q["zero_usage_skipped"]),
         "Lines with no tokens, like error notes. They cost nothing."),
        ("Calls with no cost saved", "{} of {}{}".format(
            num(q["requests_no_usage"]), num(q["requests_logged"]),
            " ({})".format(why_text(q["requests_no_usage_why"])) if q["requests_no_usage"] else ""),
         "Calls to Claude that started but have no token count in the logs, so they aren't in the total."),
        ("Chat shrinks (/compact)", "{}; {} tokens shrunk. Their cost is a hidden background call.".format(
            plural(q["compactions"], "time"), tok_short(q["compacted_tokens"])),
         "When a chat gets long, Claude Code sums it up to make room. That call is a hidden background call."),
        ("Hidden background calls", "{} from Claude Code's own running total, included in the total, for closed sessions{}".format(
            money(q["not_logged"]),
            "; {} still open, not added yet".format(plural(q["open_sessions"], "session")) if q["open_sessions"] else ""),
         "Calls Claude Code makes behind the scenes and doesn't save in its logs. They are part of every total."),
        ("Models with no price", ", ".join(q["unpriced_models"]) or "none",
         "Models missing from the price list (prices.json). Their cost is left out of every total."),
        ("Prices", "checked {} ({}), from {}".format(plain_date(q["prices_date"]), ago(q["prices_age_days"]), q["prices_source"]),
         "Anthropic's API price page, saved in prices.json."),
    ]
    if q["dupe_conflicts"]:
        rows.insert(3, ("Saved twice, counts differed", "{} (kept the last)".format(num(q["dupe_conflicts"])),
                        "The same answer was saved twice with different token counts; the last one is used."))
    if q["unreadable_lines"]:
        rows.insert(1, ("Broken lines skipped", num(q["unreadable_lines"]),
                        "Lines that couldn't be read, usually half-written at the moment of reading."))
    return rows


# ---------------------------------------------------------------- html

def _asset(name):
    """Text of a shared page file (page.css, page.js) next to spend.py."""
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def page_tips_heading(n):
    if n == 1:
        return "Best tip to reduce spend"
    return "Top {} tips to reduce spend".format(n) if n else "Tips to reduce spend"


def page_open_text(n):
    """The one plain line about sessions whose background calls aren't known yet."""
    if n == 1:
        return "1 session is still open. Its background calls get added when it closes."
    return "{} are still open. Their background calls get added when they close.".format(plural(n, "session"))


def page_why_text(why):
    names = {"interrupted": "stopped by you", "end of file": "cut off when a session closed, or still running",
             "no response written": "no reply saved"}
    return ", ".join("{} {}".format(v, names.get(k, k)) for k, v in sorted(why.items()))


def page_counted_rows(q):
    """(label, value, tooltip) rows for the page's 'The math be mathing.' Same facts as counted_rows, page words."""
    rows = [
        ("Log files read", num(q["files_read"]),
         "Claude Code's saved session files that were read for this page."),
        ("Reply lines read", num(q["assistant_lines"]),
         "Lines in the logs where Claude replied, on this day."),
        ("Replies counted", "{} (repeats removed)".format(num(q["responses"])),
         "The same reply can be saved more than once. Each one is counted once."),
        ("Empty lines skipped", num(q["zero_usage_skipped"]),
         "Lines with no tokens, like error notes. They cost nothing."),
        ("Calls with no token count", "{} of {}{}".format(
            num(q["requests_no_usage"]), num(q["requests_logged"]),
            " ({})".format(page_why_text(q["requests_no_usage_why"])) if q["requests_no_usage"] else ""),
         "Calls to Claude that started but have no token count in the logs, so they aren't in the total."),
        ("Session summaries (/compact)", "{}, {} tokens summarized. Each one is a background call.".format(
            plural(q["compactions"], "time"), tok_short(q["compacted_tokens"])),
         "When a session gets long, Claude Code summarizes it to make room. That's a background call."),
        ("Background calls", "{} from Claude Code's own running total, for closed sessions. Included in every total.{}".format(
            money(q["not_logged"]),
            " {} still open, not added yet.".format(plural(q["open_sessions"], "session")) if q["open_sessions"] else ""),
         "Calls Claude Code makes on its own and doesn't save in its logs. They're part of every total."),
        ("Models with no price", ", ".join(q["unpriced_models"]) or "None",
         "Models missing from the price list (prices.json). Their cost is left out of every total."),
        ("Prices", "Checked {} ({}) from {}".format(plain_date(q["prices_date"]), ago(q["prices_age_days"]), q["prices_source"]),
         "Anthropic's API price page, saved in prices.json."),
    ]
    if q["dupe_conflicts"]:
        rows.insert(3, ("Saved twice, counts differed", "{} (kept the last)".format(num(q["dupe_conflicts"])),
                        "The same reply was saved twice with different token counts. The last one is used."))
    if q["unreadable_lines"]:
        rows.insert(1, ("Broken lines skipped", num(q["unreadable_lines"]),
                        "Lines that couldn't be read, usually because they were still being written."))
    return rows


# Sidebar links: section id, label, icon. Each label is the heading of the section it
# links to, shortened only where the heading is long; the order is the page order.
# The page is one document, so the CSS and the navigation script are inlined from
# page.css and page.js.
SECTIONS = (("tldr", "TLDR", "check"),
            ("cost-by-type", "Cost by type", "file"), ("cost-by-model", "Cost by model", "grid"),
            ("biggest-sessions", "Biggest sessions", "clients"), ("biggest-subagents", "Biggest subagents", "branch"),
            ("cost-by-day", "Cost by day", "calendar"), ("cost-by-hour", "Cost by hour", "clock"),
            ("the-math-be-mathing", "The math be mathing", "book"))
SPRITE = (
    '<svg class="sprite" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs>'
    '<symbol id="i-compass" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m16 8-3 5-5 3 3-5z"/></symbol>'
    '<symbol id="i-file" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h4"/></symbol>'
    '<symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></symbol>'
    '<symbol id="i-book" viewBox="0 0 24 24"><path d="M12 5c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V4c-4-1-7-1-10 1zm0 0v15"/></symbol>'
    '<symbol id="i-check" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></symbol>'
    '<symbol id="i-clients" viewBox="0 0 24 24"><circle cx="8" cy="7" r="3"/><path d="M2 20v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M18 14c3 1 4 3 4 6"/></symbol>'
    '<symbol id="i-branch" viewBox="0 0 24 24"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 7v10M8 12h8"/></symbol>'
    '<symbol id="i-calendar" viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="16" rx="1"/><path d="M3 10h18M8 3v4M16 3v4"/></symbol>'
    '<symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></symbol>'
    '<symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></symbol>'
    '<symbol id="i-close" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></symbol>'
    '</defs></svg>')


def _series_class(model, series):
    return "s{}".format(series.index(model) + 1) if model in series else "s4"


def _stack(by_model, series):
    """[(class, label, value)] in fixed series order, with everything else as Other."""
    vals = defaultdict(float)
    for m, v in by_model.items():
        vals[m if m in series else "Other"] += v
    out = [(_series_class(m, series), m, vals[m]) for m in series if vals.get(m)]
    if vals.get("Other"):
        out.append(("s4", "Other", vals["Other"]))
    return out


def stacked_chart(labels, stacks, totals, extra=None, label_every=1, show_totals=True, w=960, h=260,
                  pid="hatch", title="stacked bar chart"):
    """Vertical stacked bars. stacks: per bar [(class, name, value)]. extra: per bar
    value (hidden background calls) drawn hatched on top, inside the bar. totals
    are the full bar heights, extra included. pid is the id of the hatch pattern,
    unique per chart on the page."""
    pad_b, pad_t = 28, 28
    n = len(labels)
    mx = max(totals) if totals and max(totals) > 0 else 1.0
    slot = w / max(n, 1)
    bw = min(slot * 0.62, 64)
    ph = h - pad_b - pad_t
    p = ['<svg viewBox="0 0 {} {}" role="img" aria-label="{}">'.format(w, h, html.escape(title)),
         '<defs><pattern id="{}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         '<rect width="2" height="6" fill="var(--gap)"/></pattern></defs>'.format(html.escape(pid)),
         '<line x1="0" y1="{0}" x2="{1}" y2="{0}" stroke="var(--line)"/>'.format(h - pad_b, w)]
    for i in range(n):
        x = i * slot + (slot - bw) / 2
        y = h - pad_b
        for cls, name, v in stacks[i]:
            bh = ph * v / mx
            if bh <= 0:
                continue
            y -= bh
            p.append('<rect class="{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="2"><title>{}: {} {}</title></rect>'.format(
                cls, x, y, bw, max(bh - 1, 0.5), html.escape(str(labels[i])),
                html.escape(name if name == "Other" else model_name(name)), money(v)))
        if extra and extra[i] > 0:
            bh = ph * extra[i] / mx
            y -= bh
            p.append('<rect class="gapfill" fill="url(#{})" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="2"><title>{}: background calls {}</title></rect>'.format(
                html.escape(pid), x, y, bw, max(bh - 1, 0.5), html.escape(str(labels[i])), money(extra[i])))
        if totals[i] > 0 and (show_totals or totals[i] >= 0.25 * max(totals)):
            p.append('<text class="total{}" x="{:.1f}" y="{:.1f}" text-anchor="middle">{}</text>'.format(
                "" if show_totals else " sm", x + bw / 2, y - 8, money(totals[i])))
        if i % label_every == 0:
            p.append('<text x="{:.1f}" y="{}" text-anchor="middle">{}</text>'.format(
                x + bw / 2, h - 8, html.escape(str(labels[i]))))
    p.append("</svg>")
    return "".join(p)


def legend(series, has_other, has_gap=False):
    items = ['<span title="{}"><i class="sw {}"></i>{}</span>'.format(html.escape(m), _series_class(m, series), html.escape(model_name(m)))
             for m in series]
    if has_other:
        items.append('<span><i class="sw s4"></i>Other models</span>')
    if has_gap:
        items.append('<span title="{}"><i class="sw gapsw"></i>{}</span>'.format(
            html.escape(PAGE_BACKGROUND_LINE), PAGE_LABEL["hidden"]))
    return '<div class="legend">{}</div>'.format("".join(items))


def _td(v, cls="r"):
    return '<td class="{}">{}</td>'.format(cls, v)


def _trunc(text):
    """Long text cut with an ellipsis by the page; the full text is the tooltip."""
    return '<span class="trunc" title="{0}">{0}</span>'.format(html.escape(text or ""))


def _th(label, tip, right=False):
    """Table header with its one-line plain explanation as a tooltip."""
    return '<th scope="col"{} title="{}">{}</th>'.format(' class="r"' if right else "", html.escape(tip), html.escape(label))


def _table_open(label, cls="", style=""):
    """Opens a table inside its own keyboard-scrollable region; close with '</tbody></table></div>'."""
    return '<div class="scroll table-scroll"{} tabindex="0" role="region" aria-label="{}"><table{}>'.format(
        ' style="{}"'.format(style) if style else "", html.escape(label), ' class="{}"'.format(cls) if cls else "")


def _chart_open(label):
    return '<div class="scroll chart" tabindex="0" role="region" aria-label="{}">'.format(html.escape(label))


def _tokens_tip(t):
    """Token counts by type, in the page's order, for a tooltip."""
    return "Tokens: output {}, re-reading {}, caching {}, new input {}.".format(
        num(t["output"]), num(t["cache_read"]), num(cache_writes(t)), num(t["input"]))


def _tok_td(n, cls="r"):
    """Token count, shortened, with the exact number as a tooltip."""
    return '<td class="{}" title="{} tokens">{}</td>'.format(cls, num(n), tok_short(n))


def _nb(text):
    """Escaped text with '(5 min)' and '(1 hour)' kept on one line."""
    return html.escape(text).replace(" min)", "&nbsp;min)").replace(" hour)", "&nbsp;hour)")


def _model_html(m):
    return '<span title="{}">{}</span>'.format(html.escape(m), html.escape(model_name(m)))


def _models_html(ms):
    return ", ".join(_model_html(m) for m in ms)


def render_html(s, refresh=False, warning=None):
    esc = html.escape
    q = s["quality"]
    series = s["chart_series"]
    target = s["date"]
    p = ['<!doctype html>\n<html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         '<meta name="color-scheme" content="light">']
    if refresh:
        p.append('<meta http-equiv="refresh" content="60">')
    p.append("<title>{}</title><style>\n{}</style></head><body>".format(PAGE_TITLE, _asset("page.css")))
    p.append(SPRITE)
    p.append('<a class="skip" href="#main">Skip to content</a>')

    # sidebar: brand, the Sections button for phones, and one link per section
    updated = "Updated " + when(s["generated"], "")
    p.append('<aside class="sidebar" aria-label="Page navigation"><a class="brand" href="#overview">{}'
             '<span title="When this page was made.">{}</span><span>{}</span></a>'.format(esc(PAGE_TITLE), esc(updated), esc(LOCAL_LINE)))
    p.append('<button class="menu-toggle" type="button" aria-label="Open menu" aria-expanded="false" aria-controls="journey-nav">'
             '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-menu"/></svg></button>')
    p.append('<span class="mobile-updated" title="When this page was made.">{}</span>'.format(esc(updated)))
    p.append('<span class="mobile-updated">{}</span>'.format(esc(LOCAL_LINE)))
    p.append('<nav class="journey-nav" id="journey-nav" aria-label="Page sections">{}</nav></aside>'.format("".join(
        '<a class="journey-link" href="#{}"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-{}"/></svg>'
        '<span class="nav-label">{}</span><span class="location-dot"></span></a>'.format(sid, icon, esc(label))
        for sid, label, icon in SECTIONS)))
    p.append('<main class="page" id="main"><div class="content">')

    # hero
    p.append('<header id="overview" class="hero" aria-label="Overview"><div class="hero-grid"><div>'
             '<p class="date">{}</p><p class="intro">{}</p></div>'
             '<div class="hero-metric"><div class="value">{}</div><span class="label">Estimated cost at API prices</span></div></div>'
             '<div class="stats">'.format(esc(day_name(target)), esc(PAGE_ESTIMATE_LINE), money(s["total"])))

    def stat(label, value, tip):
        p.append('<div class="stat" title="{}"><span>{}</span><b>{}</b></div>'.format(esc(tip), esc(label), esc(value)))
    a = s[PAGE_AVG_KEY]
    nd = a["days"]
    is_today = target == date.today().isoformat()
    label1 = "vs. {}-day average by now".format(nd) if is_today else "vs. {}-day daily average".format(nd)
    label2 = "{}-day daily average".format(nd)

    def more_less(x):
        return "Same" if abs(x) < 0.005 else "{} {}".format(money(abs(x)), "more" if x > 0 else "less")
    if a["average"] is not None:
        if is_today:
            stat(label1, more_less(a["target_logged"] - a["by_now"]),
                 "Today so far: {}. Average by {} over the last {} days: {}. Background calls are left out of both, "
                 "because they're only known when a session closes.".format(
                     money(a["target_logged"]), a["by_time"], nd, money(a["by_now"])))
        else:
            stat(label1, more_less(s["total"] - a["average"]),
                 "This day's total: {}. Average cost per day over the {} days before {}: {}. "
                 "Background calls are included in both.".format(
                     money(s["total"]), nd, plain_md(target), money(a["average"])))
        stat(label2, money(a["average"]),
             "Average cost per day over the {} days before {}.".format(nd, plain_md(target)))
    else:
        value = "{} more {} of data required".format(a["needed"], "day" if a["needed"] == 1 else "days")
        tip = ("Not enough days of logs yet: they go back to {}.".format(plain_md(a["first_day"])) if a["first_day"]
               else "Not enough days of logs yet.")
        stat(label1, value, tip)
        stat(label2, value, tip)
    p.append('</div></header>')
    if warning:
        p.append('<p class="note warn">Warning: {}</p>'.format(esc(warning)))
    if q["open_sessions"]:
        p.append('<p class="status-line"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-clock"/></svg>'
                 '<span>{}</span></p>'.format(esc(page_open_text(q["open_sessions"]))))
    if q["not_logged"] or q["open_sessions"]:
        p.append('<details class="estimate-details"><summary>How this estimate works</summary><div class="detail-body">')
        if q["not_logged"]:
            p.append('<p>Includes {} in background calls. The logs show {}.</p>'.format(
                esc(money(q["not_logged"])), esc(money(s["logged_total"]))))
        p.append('<p>{}</p></div></details>'.format(esc(PAGE_BACKGROUND_LINE)))

    # tldr
    t = s.get(PAGE_TLDR_KEY) or s["tldr"]
    p.append('<section class="section" id="tldr"><article class="block tldr">'
             '<h2 title="Too long, didn\'t read: the short version.">TLDR</h2>')
    if t["empty"]:
        p.append('<p class="lead">{}</p>'.format(esc(t["meaning"])))
    else:
        p.append('<div class="tl"><ul>{}</ul></div>'.format(
            "".join("<li>{}</li>".format(esc(f)) for f in t["findings"])))
        p.append('<div class="tl insight"><h3>What&#x27;s that mean?</h3><p>{}</p></div>'.format(esc(t["meaning"])))
        p.append('<div class="tl"><h3 title="Hover a dollar figure to see the math.">{}</h3>'.format(
            page_tips_heading(len(t["recommendations"]))))
        if t["recommendations"]:
            p.append("<ol>")
            for r in t["recommendations"]:
                pill = ('<span class="tag run" title="{}">could save {}</span>'.format(esc(r["basis"]), money(r["saving_usd"]))
                        if r["saving_usd"] > 0 else '<span class="tag" title="{}">no estimate</span>'.format(esc(r["basis"])))
                p.append('<li><span>{}</span>{}</li>'.format(esc(r["action"]), pill))
            p.append("</ol>")
        else:
            p.append("<p>Nothing to suggest for this day.</p>")
        p.append("</div>")
    p.append('</article></section>')

    # cost by type: the table, its split bar and the token tiles all keep the PAGE_COST order;
    # colors go by size, so the biggest type has the strongest color
    p.append('<section class="section" id="cost-by-type"><article class="block"><h2>Cost by type</h2>'
             '<p class="lead">What the cost paid for: Claude\'s output, re-reading the session, caching, new input, '
             'web searches and background calls. {}</p>'.format(esc(PAGE_TOKEN_LINE)))
    ranked = sorted(s["token_types"], key=lambda t: -t["cost"])
    rank = {t["key"]: min(i + 1, 5) for i, t in enumerate(ranked)}
    if s["total"] > 0:
        p.append('<div class="split">')
        for t in s["token_types"]:
            if t["cost"] > 0:
                p.append('<i class="s{}" style="width:{:.3f}%" title="{}: {} ({:.1f}%)"></i>'.format(
                    rank[t["key"]], t["percent"], esc(PAGE_LABEL[t["key"]]), money(t["cost"]), t["percent"]))
        p.append('</div>')
    p.append('{}<thead><tr>{}{}{}{}</tr></thead><tbody>'.format(
        _table_open("Cost by type table"),
        _th("Type", "What the cost paid for."),
        _th("Tokens", PAGE_TOKEN_LINE, True),
        _th("Cost", "Estimated cost at API prices.", True),
        _th("Share", "Share of the day's cost.", True)))
    for t in s["token_types"]:
        sw = '<i class="sw s{}"></i>'.format(rank[t["key"]]) if t["cost"] > 0 else '<i class="sw"></i>'
        cell = (_td("--") if t["tokens"] is None else
                _td(plural(t["tokens"], "search", "searches")) if t["key"] == "searches" else _tok_td(t["tokens"]))
        p.append('<tr><td class="p">{}{}<div class="dim small">{}</div></td>{}{}{}</tr>'.format(
            sw, _nb(PAGE_LABEL[t["key"]]), esc(PAGE_EXPLAIN[t["key"]]), cell,
            _td(money(t["cost"])), _td("{:.1f}%".format(t["percent"]))))
    p.append('</tbody></table></div></article>')

    # token tiles, in the same order as the table above
    tk = {t["key"]: t["tokens"] for t in s["token_types"] if t["tokens"] is not None}
    p.append('<article class="block"><h3 title="{}">Tokens by type</h3><div class="tiles">'.format(esc(PAGE_TOKEN_LINE)))
    for lab, val, tip in ((PAGE_LABEL["output"], tk["output"], "Tokens Claude wrote."),
                          (PAGE_LABEL["cache_read"], tk["cache_read"], "Tokens Claude re-read from the cache."),
                          ("Caching", tk["cache_write_5m"] + tk["cache_write_1h"], "Tokens saved to the cache, 5 min and 1 hour together."),
                          (PAGE_LABEL["input"], tk["input"], "Tokens sent to Claude that weren't in the cache.")):
        p.append('<div class="tile" title="{}"><span>{}</span><b title="{} tokens">{}</b></div>'.format(
            esc(tip), esc(lab), num(val), tok_short(val)))
    p.append('</div></article></section>')

    # cost by model
    days_text = plural(s["days_shown"], "day")
    p.append('<section class="section" id="cost-by-model"><article class="block"><h2>Cost by model</h2>'
             '<p class="lead">Every Claude model used in the last {}. '
             'Token counts are for this day only. Costs include background calls.</p>'.format(days_text))
    p.append('{}<thead><tr>{}</tr></thead><tbody>'.format(_table_open("Cost by model table", "wide-table"), "".join([
        _th("Model", "The Claude model. Hover a name for its full ID."),
        _th("Cost", "This day's cost, background calls included.", True),
        _th("Replies", "How many times this model replied.", True),
        _th(PAGE_SHORT["output"], "Tokens Claude wrote.", True),
        _th(PAGE_SHORT["cache_read"], "Tokens Claude re-read from the cache.", True),
        _th(PAGE_SHORT["cache_write_5m"], "Tokens saved to the cache for 5 minutes.", True),
        _th(PAGE_SHORT["cache_write_1h"], "Tokens saved to the cache for 1 hour.", True),
        _th(PAGE_SHORT["input"], "Tokens sent to Claude that weren't in the cache.", True),
        _th("{}-day cost".format(s["days_shown"]), "Cost over the last {}, background calls included.".format(days_text), True),
        _th(PAGE_LABEL["hidden"] + "*", "The part of the {}-day cost that's background calls. See the note below.".format(s["days_shown"]), True)])))
    for m in s["models"]:
        t = m["tokens"]
        p.append('<tr><td class="nw">{}{}</td>{}{}{}{}{}{}{}{}{}</tr>'.format(
            _model_html(m["model"]), "" if m["priced"] else '<span class="tag unp">no price</span>',
            _td(money(m["cost"]) if m["priced"] else "no price"), _td(num(m["responses"])),
            _tok_td(t["output"]), _tok_td(t["cache_read"]), _tok_td(t["cache_write_5m"]),
            _tok_td(t["cache_write_1h"]), _tok_td(t["input"]),
            _td(money(m["window_cost"]) if m["priced"] else "no price"),
            _td(money(m["window_not_logged"]) if m["window_not_logged"] else "")))
    if not s["models"]:
        p.append('<tr><td colspan="10" class="dim">No models used in these days.</td></tr>')
    p.append('</tbody></table></div><p class="note">* Included in the cost columns. Background calls come from Claude Code\'s '
             'own running total, for closed sessions. A model marked "no price" isn\'t in the price list (prices.json), '
             'so it\'s left out of every total.</p></article>')

    p.append('</section>')

    # biggest sessions: the money columns run in the order they add up to the total
    shown = min(len(s["sessions"]), TOP_SESSIONS)
    p.append('<section class="section" id="biggest-sessions"><article class="block">'
             '<h2 title="A session is one Claude Code conversation, from start until you close it.">Biggest sessions</h2>'
             '<p class="lead">Costliest first, subagents included. Showing {}.</p>'.format(
                 plural(shown, "session") if shown == len(s["sessions"]) else "{} of {}".format(shown, plural(len(s["sessions"]), "session"))))
    p.append('{}<thead><tr>{}</tr></thead><tbody>'.format(_table_open("Biggest sessions table", "wide-table"), "".join([
        _th("Started", "When the session started, your local time."),
        _th("Project", "The folder Claude Code worked in."),
        _th("First message", "The first thing you typed in the session. Hover to see all of it."),
        _th("Models", "Models used, biggest cost first."),
        _th("Subagent count", "How many subagents this session started.", True),
        _th("Main session", "Cost of the session itself, without its subagents.", True),
        _th("Subagents", "Cost of the subagents this session started.", True),
        _th(PAGE_SHORT["hidden"], "The part of the total that's background calls. \"Not yet\" means the session is still open, so they aren't added yet.", True),
        _th("Total", "Main session plus subagents plus background calls.", True)])))
    for r in s["sessions"][:TOP_SESSIONS]:
        badge = '<span class="tag run" title="Active in the last 5 minutes.">running</span>' if r["running"] else ""
        nl = ('<span title="Still open. Added when it closes.">not yet</span>' if r["not_logged"] is None
              else (money(r["not_logged"]) if r["not_logged"] else ""))
        p.append('<tr><td class="nw">{}</td><td class="nw">{}</td><td class="p"><div style="display:flex;align-items:center">{}{}</div></td><td class="nw">{}</td>{}{}{}{}{}</tr>'.format(
            esc(when(r["start"], target)), esc(r["project"]), _trunc(r["prompt"]), badge,
            _models_html(r["models"]),
            _td(num(r["subagents"])), _td(money(r["main"])), _td(money(r["sub"])), _td(nl),
            _td(money(r["total"]), "r tot")))
    if not s["sessions"]:
        p.append('<tr><td colspan="9" class="dim">No sessions on this day.</td></tr>')
    p.append('</tbody></table></div></article>')

    # biggest subagents
    shown = min(len(s["subagents"]), TOP_SUBAGENTS)
    p.append('</section><section class="section" id="biggest-subagents"><article class="block">'
             '<h2 title="Subagents are extra agents a session starts to take on part of the work.">'
             'Biggest subagents</h2><p class="lead">Extra agents a session starts to take on part of the work. Costliest first, '
             'across all sessions, priced by the model that did the work. Showing {}.</p>'.format(
                 plural(shown, "subagent") if shown == len(s["subagents"]) else "{} of {}".format(shown, plural(len(s["subagents"]), "subagent"))))
    p.append('{}<thead><tr>{}</tr></thead><tbody>'.format(_table_open("Biggest subagents table", "wide-table"), "".join([
        _th("Name", "The subagent's name, or its first instruction if it has none."),
        _th("Model", "The model that did the work."),
        _th("Started from", "The first message of the session that started it."),
        _th("Cost", "Estimated cost at API prices.", True),
        _th("Tokens", "All tokens. Hover a number to see them by type.", True),
        _th("Replies", "How many times it replied.", True),
        _th("Time", "When it ran, your local time."),
        _th("Took", "How long it ran.", True),
        _th("Status", "Done, running (active in the last 5 minutes) or stopped.")])))
    for r in s["subagents"][:TOP_SUBAGENTS]:
        tk = r["tokens"]
        tip = _tokens_tip(tk)
        req = r["requested_model"]
        asked = (' <span class="dim">(asked for {})</span>'.format(esc(req[:1].upper() + req[1:]))
                 if isinstance(req, str) and req not in ("inherit", "") and not any(req in m for m in r["models"]) else "")
        name = esc(r["name"]) if r["named"] else '<span class="dim">{}</span>'.format(esc(r["name"]))
        status = STATUS_WORDS.get(r["status"], r["status"])
        p.append('<tr><td class="p">{}</td><td class="nw">{}{}</td><td class="p dim">{}</td>{}<td class="r" title="{}">{}</td>{}'
                 '<td class="nw">{}</td>{}<td>{}</td></tr>'.format(
                     name, _models_html(r["models"]), asked,
                     _trunc(r["session_prompt"] or r["session"]), _td(money(r["cost"]), "r tot"),
                     esc(tip), tok_short(sum(tk[k] for k in PRICE_KEYS)), _td(num(r["responses"])),
                     esc(time_range(r["start"], r["end"])), _td(duration(r["duration_s"])),
                     '<span class="tag run" style="margin-left:0">running</span>' if r["status"] == "running" else esc(status)))
    if not s["subagents"]:
        p.append('<tr><td colspan="9" class="dim">No subagents on this day.</td></tr>')
    p.append('</tbody></table></div></article></section>')

    # cost by day
    dstacks = [_stack(dd["by_model"], series) for dd in s["days"]]
    has_other = any(c == "s4" for st in dstacks for c, _, _ in st)
    gaps = [dd["not_logged"] for dd in s["days"]]
    n = len(s["days"])
    labels = [datetime.strptime(dd["date"], "%Y-%m-%d").strftime("%a %d" if n <= 14 else "%b %d").replace(" 0", " ") for dd in s["days"]]
    p.append('<section class="section" id="cost-by-day"><article class="block"><h2>Cost by day</h2>'
             '<p class="lead">The last {}. Each bar is the day\'s full cost.{}</p>{}{}{}</div>'.format(
        plural(n, "day"), " The striped part is background calls." if any(g > 0 for g in gaps) else "",
        legend(series, has_other, any(g > 0 for g in gaps)), _chart_open("Cost by day chart"),
        stacked_chart(labels, dstacks, [dd["total"] for dd in s["days"]], extra=gaps,
                      label_every=1 if n <= 14 else max(1, n // 10), show_totals=n <= 14,
                      pid="hatch-week", title="Cost by day, split by model")))
    p.append('{}<thead><tr>{}{}{}{}</tr></thead><tbody>'.format(
        _table_open("Cost by day table", "narrow", "margin-top:24px"),
        _th("Day", "Newest first."),
        _th("Cost", "Estimated cost at API prices, background calls included.", True),
        _th(PAGE_SHORT["hidden"], "The part of the cost that's background calls.", True),
        _th("Still open", "Sessions not closed yet, so their background calls aren't added yet.", True)))
    for dd, lab in zip(reversed(s["days"]), reversed(labels)):
        p.append('<tr><td class="nw">{}</td>{}{}{}</tr>'.format(esc(lab), _td(money(dd["total"])),
                                                    _td(money(dd["not_logged"]) if dd["not_logged"] else ""),
                                                    _td(num(dd["open_sessions"]) if dd["open_sessions"] else "")))
    p.append('</tbody></table></div></article>')

    # cost by hour
    hstacks = [_stack(h["by_model"], series) for h in s["hours"]]
    has_other = any(c == "s4" for st in hstacks for c, _, _ in st)
    p.append('</section><section class="section" id="cost-by-hour"><article class="block"><h2>Cost by hour</h2><p class="lead">Your local time. Hover a bar to see each model\'s cost. '
             'Background calls aren\'t tied to an hour, so they aren\'t here.</p>{}{}{}</div></article></section>'.format(
        legend(series, has_other), _chart_open("Cost by hour chart"),
        stacked_chart([hour_name(h["hour"]).replace(" am", "a").replace(" pm", "p") for h in s["hours"]], hstacks,
                      [h["total"] for h in s["hours"]], label_every=1, show_totals=False,
                      pid="hatch-hour", title="Cost by hour, split by model")))

    # how it's counted
    p.append('<section class="section" id="the-math-be-mathing"><article class="block"><h2>The math be mathing.</h2>'
             '<p class="lead">What was read on this day, and what couldn\'t be counted.</p><dl>')
    for k, v, tip in page_counted_rows(q):
        p.append('<div class="kv"><dt title="{}">{}</dt><dd>{}</dd></div>'.format(esc(tip), esc(k), esc(v)))
    p.append('</dl></article>')

    # sharing note
    p.append('</section>')
    p.append('<footer class="page-footer"><span>Made with &#x2764;&#xFE0F; by <a href="https://keigowda.com" target="_blank" rel="noopener">Kei</a></span><a href="#overview">Back to top</a></footer>')
    p.append("</div></main><script>\n{}</script></body></html>".format(_asset("page.js")))
    return "".join(p)


def write_file(text, out):
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out)


def artifact_path():
    """Fixed path of the page published as an artifact, so each publish replaces the last."""
    return os.path.join(HERE, "artifact.html")


def try_open(path):
    """Open in a browser; never fail when none exists."""
    try:
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        return bool(webbrowser.open(Path(path).resolve().as_uri()))
    except Exception:
        return False


# ---------------------------------------------------------------- main

def run_once(args, prices, target, root):
    first = target - timedelta(days=max(args.days, 1) - 1)
    # only a page shows the 30-day average, so only a page reads the extra days
    page = (args.html or args.artifact or args.watch) and not args.json
    data = scan(root, first, target, prices, history_days=AVG_DAYS + 1 if page else 0)
    return summarize(data, target, args.days)


def _utf8_streams():
    """Keep odd characters in prompts from crashing a Windows console."""
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv=None):
    _utf8_streams()
    ap = argparse.ArgumentParser(description="Estimate Claude Code spend from local transcripts.")
    ap.add_argument("--html", action="store_true", help="write dashboard.html and open it")
    ap.add_argument("--artifact", action="store_true", help="write artifact.html, a page to publish as an artifact")
    ap.add_argument("--json", action="store_true", help="print everything as JSON")
    ap.add_argument("--watch", action="store_true", help="rewrite the dashboard every 60 seconds")
    ap.add_argument("--days", type=int, default=7, help="days of history (default 7)")
    ap.add_argument("--date", help="show a past day, YYYY-MM-DD")
    ap.add_argument("--out", help="dashboard path (default: dashboard.html next to spend.py)")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = ap.parse_args(argv)
    if args.days < 1:
        ap.error("--days must be 1 or more")
    try:
        target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    except ValueError:
        ap.error("--date must be YYYY-MM-DD")
    try:
        prices = load_prices()
    except (OSError, ValueError) as e:
        print("Cannot read prices.json next to spend.py: {}".format(e), file=sys.stderr)
        return 1
    root = config_dir()
    out = os.path.abspath(os.path.expanduser(args.out)) if args.out else os.path.join(HERE, "dashboard.html")
    art = artifact_path()
    warning = stale_warning(prices)

    if not has_transcripts(root):
        if args.json:
            print(json.dumps({"error": "no transcripts", "message": no_transcripts_message(root)}, indent=2))
        else:
            print(no_transcripts_message(root))
        return 0

    def write(s, refresh, path):
        try:
            write_file(render_html(s, refresh, warning), path)
        except OSError as e:
            print("Cannot write {}: {}".format(path, e), file=sys.stderr)
            return False
        return True

    if args.watch:
        opened = False
        try:
            while True:
                tgt = target if args.date else date.today()
                s = run_once(args, prices, tgt, root)
                if write(s, True, out):
                    print("{} wrote {} total {}".format(datetime.now().strftime("%H:%M:%S"), out, money(s["total"])), flush=True)
                    if not opened and not args.no_open:
                        try_open(out)
                    opened = True
                time.sleep(60)
        except KeyboardInterrupt:
            return 0
    s = run_once(args, prices, target, root)
    if args.json:
        s.pop(PAGE_TLDR_KEY, None)
        s.pop(PAGE_AVG_KEY, None)
        s["prices_warning"] = warning
        print(json.dumps(s, indent=2))
        return 0
    print(render_text(s, warning))
    code = 0
    if args.artifact:
        if write(s, False, art):
            print("\nArtifact page: " + art)
        else:
            code = 1
    if args.html:
        if not write(s, False, out):
            return 1
        print("\nDashboard: " + out)
        if not args.no_open and not try_open(out):
            print("Could not open a browser here. Open the file above yourself.")
    return code


if __name__ == "__main__":
    sys.exit(main())
