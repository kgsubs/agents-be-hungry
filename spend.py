#!/usr/bin/env python3
"""claude-spend: estimate Claude Code spend from local session transcripts.

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
ESTIMATE_LINE = ("Estimate at API list prices. On a Pro or Max plan this is what "
                 "the same usage would cost on the API, not what you are billed.")
PRIVACY_LINE = ("This page contains your prompts, agent names and project folder names. "
                "Do not share it as-is.")
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
    ("output", "Writing",
     "Text, code, tool calls and thinking that Claude wrote."),
    ("cache_read", "Re-reading the conversation",
     "Each turn Claude reads the conversation so far again, from a cheap cache."),
    ("cache_write_5m", "Saving to the 5-minute cache",
     "New context stored so the next turns can re-read it cheaply for 5 minutes."),
    ("cache_write_1h", "Saving to the 1-hour cache",
     "The same, kept for an hour, at a higher price."),
    ("input", "New input, not cached",
     "Text sent to Claude that was not read from or saved to the cache."),
    ("searches", "Web searches",
     "Searches Claude ran on the web, charged per search."),
)


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
        return "prices.json has no valid date_read, so its age is unknown. Refresh it from " + str(prices.get("source_url", "the pricing page")) + "."
    if age > STALE_DAYS:
        return ("prices.json was read {} days ago ({}). Prices may have changed; refresh it from {}."
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
    return ("No Claude Code transcripts found.\n"
            "Looked in: {}\n"
            "Claude Code writes sessions there when it runs on this computer. "
            "Claude Code on the web (claude.ai/code) runs in the cloud, so its sessions "
            "are not on this computer and cannot be counted here.\n"
            "If your Claude config folder is somewhere else, set CLAUDE_CONFIG_DIR to it."
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


def scan(root, first_day, last_day, prices):
    """Read transcripts once. Returns raw, deduplicated data for summarize()."""
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
    return {"records": records, "meta": meta, "agents": agents, "lines_by_day": lines_by_day,
            "zero_by_day": zero_by_day, "unmatched": unmatched, "req_by_day": req_by_day,
            "compactions": compactions, "snapshots": dict(snapshots), "counts": dict(q),
            "first_day": first_day, "last_day": last_day, "prices": prices}


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
        days.append({"date": d.isoformat(), "total": sum(bym.values()), "by_model": dict(bym),
                     "not_logged": nl, "open_sessions": len(gaps["open"].get(d, ()))})
    total = days[-1]["total"]
    yesterday_total = sum(r["cost"] for r in recs if r["day"] == target - timedelta(days=1) and r["cost"] is not None)

    # models: every model seen in the window, or in a tally for the window
    models = {}

    def mrow(name):
        return models.setdefault(name, {"model": name, "priced": True, "cost": 0.0, "responses": 0,
                                        "tokens": _blank_tok(), "window_cost": 0.0, "window_responses": 0,
                                        "not_logged": 0.0, "window_not_logged": 0.0})
    for r in win_recs:
        m = mrow(r["model"])
        if r["cost"] is None:
            m["priced"] = False
        else:
            m["window_cost"] += r["cost"]
        m["window_responses"] += r["resp"]
        if r["day"] == target:
            m["responses"] += r["resp"]
            _add_tok(m["tokens"], r["tok"])
            if r["cost"] is not None:
                m["cost"] += r["cost"]
    for (d, name), g in gaps["day_model"].items():
        if first <= d <= target:
            m = mrow(name)
            if not win_recs or name not in {r["model"] for r in win_recs}:
                m["priced"] = find_price(data["prices"], name) is not None
            m["window_not_logged"] += g
            if d == target:
                m["not_logged"] += g
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
            "total": v["main"] + v["sub"], "unpriced_responses": v["unpriced"],
            "subagents": len(v["agents"]),
            "models": [k for k, _ in sorted(v["models"].items(), key=lambda kv: -kv[1])],
            "running": target == date.today() and (now - info.get("mtime", 0)) <= RUNNING_SECONDS,
            "not_logged": None if (is_open and not g) else (g or 0.0),
            "tally": "open" if is_open else "closed"})
    sess_rows.sort(key=lambda r: (-r["total"], r["session"]))

    # projects
    proj = defaultdict(lambda: {"cost": 0.0, "sessions": set(), "subagents": set()})
    for r in day_recs:
        p = proj[r["session"][0]]
        p["sessions"].add(r["session"][1])
        if r["is_sub"]:
            p["subagents"].add(r["agent"])
        if r["cost"] is not None:
            p["cost"] += r["cost"]
    proj_rows = sorted(({"project": k, "cost": v["cost"], "sessions": len(v["sessions"]),
                         "subagents": len(v["subagents"])} for k, v in proj.items()),
                       key=lambda r: (-r["cost"], r["project"]))

    # chart series: the biggest models over the window keep their own colour
    order = sorted(((m["window_cost"], m["model"]) for m in model_rows if m["window_cost"] > 0), reverse=True)
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
        "prices_date": pr.get("date_read"), "prices_age_days": price_age_days(pr),
        "prices_source": pr.get("source_url"),
    }
    return {
        "date": target.isoformat(), "generated": _iso(datetime.fromtimestamp(now).astimezone()),
        "estimate_line": ESTIMATE_LINE, "days_shown": ndays,
        "total": total, "yesterday": yesterday_total, "change": total - yesterday_total,
        "average": sum(d["total"] for d in days) / len(days),
        "running_sessions": sum(1 for r in sess_rows if r["running"]),
        "token_types": token_types, "models": model_rows, "sessions": sess_rows,
        "subagents": sub_rows, "projects": proj_rows, "hours": hours, "days": days,
        "chart_series": series, "quality": quality,
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


def hhmm(s):
    return datetime.fromisoformat(s).strftime("%H:%M") if s else "--:--"


def when(s, target):
    if not s:
        return ""
    d = datetime.fromisoformat(s)
    return d.strftime("%H:%M") if d.date().isoformat() == target else d.strftime("%b %d %H:%M")


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
    names = {"interrupted": "interrupted", "end of file": "cut off when the session closed or still in flight",
             "no response written": "no response written"}
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


def render_text(s, warning=None):
    o = []
    q = s["quality"]
    o.append("Claude Code spend on {}: {}".format(s["date"], money(s["total"])))
    o.append("  vs day before {}   {}-day average {}   updated {}".format(
        signed_money(s["change"]), s["days_shown"], money(s["average"]), hhmm(s["generated"])))
    if q["not_logged"]:
        o.append("  Not in transcripts (from Claude Code's own tally): about {} more".format(money(q["not_logged"])))
    if q["open_sessions"]:
        o.append("  {} session(s) still open: their calls not written to transcripts are not known yet".format(q["open_sessions"]))
    o.append(ESTIMATE_LINE)
    if warning:
        o.append("Warning: " + warning)

    o.append("")
    o.append("Where the money went")
    o += _table(["", "Tokens", "Cost", "Share"],
                [[t["label"], num(t["tokens"]), money(t["cost"]), "{:.1f}%".format(t["percent"])]
                 for t in s["token_types"]], {1, 2, 3})

    o.append("")
    o.append("By model (window = last {} days)".format(s["days_shown"]))
    rows = []
    for m in s["models"]:
        t = m["tokens"]
        rows.append([m["model"], money(m["cost"]) if m["priced"] else "unpriced", num(m["responses"]),
                     short_num(t["output"]), short_num(t["cache_read"]), short_num(cache_writes(t)),
                     short_num(t["input"]), money(m["window_cost"]) if m["priced"] else "unpriced",
                     money(m["window_not_logged"]) if m["window_not_logged"] else ""])
    o += _table(["Model", "Today", "Resp", "Output", "Cache read", "Cache write", "Input", "Window",
                 "Not in transcripts"], rows, {1, 2, 3, 4, 5, 6, 7, 8})

    o.append("")
    o.append("Biggest sessions")
    rows = []
    for r in s["sessions"][:TOP_SESSIONS]:
        rows.append([money(r["total"]), money(r["main"]), money(r["sub"]), str(r["subagents"]),
                     when(r["start"], s["date"]), cut(r["project"], 24),
                     ", ".join(x.replace("claude-", "") for x in r["models"]),
                     cut(r["prompt"], 48) + ("  [running]" if r["running"] else "")])
    o += _table(["Total", "Main", "Subagents", "#", "Started", "Project", "Models", "First prompt"], rows, {0, 1, 2, 3})

    o.append("")
    o.append("Biggest subagents")
    rows = []
    for r in s["subagents"][:TOP_SUBAGENTS]:
        rows.append([money(r["cost"]), cut(r["name"], 34), ", ".join(x.replace("claude-", "") for x in r["models"]),
                     num(r["responses"]), short_num(sum(r["tokens"][k] for k in PRICE_KEYS)),
                     "{}-{}".format(hhmm(r["start"]), hhmm(r["end"])), duration(r["duration_s"]), r["status"],
                     r["session"][:8]])
    o += _table(["Cost", "Name", "Model", "Resp", "Tokens", "Time", "Took", "Status", "Session"], rows, {0, 3, 4, 6})
    if not s["subagents"]:
        o.append("  none on this day")

    o.append("")
    o.append("By project")
    o += _table(["Cost", "Sessions", "Subagents", "Project"],
                [[money(p["cost"]), str(p["sessions"]), str(p["subagents"]), p["project"]] for p in s["projects"]],
                {0, 1, 2})

    o.append("")
    o.append("By hour")
    series = s["chart_series"]
    short = [m.replace("claude-", "") for m in series]

    def split(by_model):
        other = sum(v for m, v in by_model.items() if m not in series)
        return [money(by_model.get(m, 0.0)) for m in series] + [money(other)]
    hrs = [h for h in s["hours"] if h["total"] > 0]
    o += _table(["Hour", "Cost"] + short + ["Other"],
                [["{:02d}:00".format(h["hour"]), money(h["total"])] + split(h["by_model"]) for h in hrs],
                set(range(1, len(series) + 3)))

    o.append("")
    o.append("Last {} days".format(s["days_shown"]))
    o += _table(["Day", "Logged"] + short + ["Other", "Not in transcripts", "Open sessions"],
                [[datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %b %d"), money(d["total"])] + split(d["by_model"])
                 + [money(d["not_logged"]) if d["not_logged"] else "", str(d["open_sessions"]) if d["open_sessions"] else ""]
                 for d in s["days"]], set(range(1, len(series) + 5)))

    o.append("")
    o.append("Data quality")
    o.append("  Files read {}; assistant lines that day {}; responses after dedupe {}; zero-usage lines skipped {}".format(
        num(q["files_read"]), num(q["assistant_lines"]), num(q["responses"]), num(q["zero_usage_skipped"])))
    o.append("  Requests logged that day {}; with no usage logged {}{}".format(
        num(q["requests_logged"]), num(q["requests_no_usage"]),
        " ({})".format(why_text(q["requests_no_usage_why"])) if q["requests_no_usage"] else ""))
    o.append("  Compactions that day {} ({} tokens summarized); their usage is not written to transcripts".format(
        num(q["compactions"]), num(q["compacted_tokens"])))
    if q["unpriced_models"]:
        o.append("  Unpriced models (not in the total): " + ", ".join(q["unpriced_models"]))
    if q["dupe_conflicts"]:
        o.append("  Responses written twice with different usage (last kept): {}".format(num(q["dupe_conflicts"])))
    o.append("  Prices read {} ({} days ago)".format(q["prices_date"], q["prices_age_days"]))
    return "\n".join(o)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#fff;--ink:#267e5c;--mute:#7da595;--rule:#93bfae;--hl:rgba(176,217,200,.5);--band:#f3f9f6;
--s1:#267e5c;--s2:#b0d9c8;--s3:#7da595;--s4:#4d4d4d;--warn:#9a5b00;--gap:#93bfae}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1a15;--ink:#b0d9c8;--mute:#7da595;--rule:#2f5444;
--hl:rgba(38,126,92,.35);--band:#14231c;--s1:#93bfae;--s2:#267e5c;--s3:#d0ebe0;--s4:#8a8a8a;--warn:#f0c36d;--gap:#7da595}}
:root[data-theme="dark"]{--bg:#0f1a15;--ink:#b0d9c8;--mute:#7da595;--rule:#2f5444;
--hl:rgba(38,126,92,.35);--band:#14231c;--s1:#93bfae;--s2:#267e5c;--s3:#d0ebe0;--s4:#8a8a8a;--warn:#f0c36d;--gap:#7da595}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.4 Arial,sans-serif}
main{max-width:1060px;margin:0 auto;padding:40px 16px 64px}
h1,h2,h3{font-weight:normal;margin:0}
h1{font-size:18px;color:var(--mute)}
h2{font-size:28px;margin:0 0 6px}
.lead{color:var(--mute);margin:0 0 16px;font-size:15px}
section{padding:32px 0;border-top:1px solid var(--rule)}
.big{font-size:72px;line-height:1.05;margin:8px 0 4px}
.stats{display:flex;flex-wrap:wrap;gap:12px 40px;margin:16px 0 8px}
.stat span{display:block;color:var(--mute);font-size:14px}
.stat b{font-weight:normal;font-size:26px}
.note{color:var(--mute);font-size:14px;margin:6px 0}
.warn{color:var(--warn)}
.band{background:var(--band);padding:14px 16px;margin:16px 0 0}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:15px}
th,td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-weight:normal;color:var(--mute);font-size:13px;white-space:nowrap}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.nw{white-space:nowrap}
table.narrow{width:auto;min-width:50%}table.narrow th,table.narrow td{padding-right:32px}
td.p{min-width:220px}
tr:hover td{background:var(--hl)}
.dim{color:var(--mute)}.small{font-size:14px;padding-left:17px}
.tag{display:inline-block;border:1px solid var(--rule);padding:0 7px;font-size:12px;color:var(--mute);margin-left:6px;white-space:nowrap}
.tag.run{border-color:var(--ink);color:var(--ink)}
.sw{display:inline-block;width:11px;height:11px;margin-right:6px;vertical-align:-1px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px;margin:4px 0 8px}
.split{display:flex;height:22px;margin:8px 0 18px}
.split i{display:block;height:100%;border-right:2px solid var(--bg)}
svg{width:100%;height:auto;display:block}
.chart svg{min-width:640px}
svg text{fill:var(--mute);font:12px Arial,sans-serif}
svg .total{fill:var(--ink)}
.s1{fill:var(--s1);background:var(--s1)}.s2{fill:var(--s2);background:var(--s2)}
.s3{fill:var(--s3);background:var(--s3)}.s4{fill:var(--s4);background:var(--s4)}
.gapfill{fill:url(#hatch);stroke:var(--gap);stroke-width:1}
.gapsw{background:repeating-linear-gradient(45deg,var(--gap) 0 2px,transparent 2px 5px);outline:1px solid var(--gap)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 24px;margin:0;font-size:15px}
dt{color:var(--mute)}dd{margin:0}
@media (max-width:640px){body{font-size:16px}.big{font-size:52px}h2{font-size:24px}dl{grid-template-columns:1fr}dd{margin-bottom:8px}}
"""


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


def stacked_chart(labels, stacks, totals, extra=None, label_every=1, show_totals=True, w=960, h=240):
    """Vertical stacked bars. stacks: per bar [(class, name, value)]. extra: per bar
    value drawn hatched on top (calls not in transcripts)."""
    pad_b, pad_t = 26, 24
    n = len(labels)
    tops = [totals[i] + ((extra[i] if extra and extra[i] > 0 else 0)) for i in range(n)]
    mx = max(tops) if tops and max(tops) > 0 else 1.0
    slot = w / max(n, 1)
    bw = min(slot * 0.72, 64)
    ph = h - pad_b - pad_t
    p = ['<svg viewBox="0 0 {} {}" role="img" aria-label="stacked bar chart">'.format(w, h),
         '<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         '<rect width="2" height="6" fill="var(--gap)"/></pattern></defs>',
         '<line x1="0" y1="{0}" x2="{1}" y2="{0}" stroke="var(--rule)"/>'.format(h - pad_b, w)]
    for i in range(n):
        x = i * slot + (slot - bw) / 2
        y = h - pad_b
        for cls, name, v in stacks[i]:
            bh = ph * v / mx
            if bh <= 0:
                continue
            y -= bh
            p.append('<rect class="{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}"><title>{}: {} {}</title></rect>'.format(
                cls, x, y, bw, max(bh - 1, 0.5), html.escape(str(labels[i])), html.escape(name), money(v)))
        if extra and extra[i] > 0:
            bh = ph * extra[i] / mx
            y -= bh
            p.append('<rect class="gapfill" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}"><title>{}: not in transcripts {}</title></rect>'.format(
                x, y, bw, max(bh - 1, 0.5), html.escape(str(labels[i])), money(extra[i])))
        if totals[i] > 0 and (show_totals or totals[i] >= 0.25 * max(totals)):
            lab = money(totals[i]) if show_totals else "${:,.0f}".format(totals[i])
            p.append('<text class="total" x="{:.1f}" y="{:.1f}" text-anchor="middle">{}</text>'.format(
                x + bw / 2, y - 6, lab))
        if i % label_every == 0:
            p.append('<text x="{:.1f}" y="{}" text-anchor="middle">{}</text>'.format(
                x + bw / 2, h - 8, html.escape(str(labels[i]))))
    p.append("</svg>")
    return "".join(p)


def legend(series, has_other, has_gap=False):
    items = ['<span><i class="sw {}"></i>{}</span>'.format(_series_class(m, series), html.escape(m)) for m in series]
    if has_other:
        items.append('<span><i class="sw s4"></i>Other models</span>')
    if has_gap:
        items.append('<span><i class="sw gapsw"></i>Not in transcripts (Claude Code tally)</span>')
    return '<div class="legend">{}</div>'.format("".join(items))


def _td(v, cls="r"):
    return '<td class="{}">{}</td>'.format(cls, v)


def render_html(s, refresh=False, warning=None):
    esc = html.escape
    q = s["quality"]
    series = s["chart_series"]
    target = s["date"]
    p = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">']
    if refresh:
        p.append('<meta http-equiv="refresh" content="60">')
    p.append("<title>{}</title><style>{}</style></head><body><main>".format(PAGE_TITLE, CSS))

    # header
    d = datetime.strptime(target, "%Y-%m-%d")
    p.append('<h1>Claude Code spend, {}</h1>'.format(esc(d.strftime("%A %B %d, %Y").replace(" 0", " "))))
    p.append('<div class="big">{}</div>'.format(money(s["total"])))
    p.append('<div class="stats">')
    p.append('<div class="stat"><span>vs day before</span><b>{}</b></div>'.format(signed_money(s["change"])))
    p.append('<div class="stat"><span>{}-day average</span><b>{}</b></div>'.format(s["days_shown"], money(s["average"])))
    if q["not_logged"]:
        p.append('<div class="stat"><span>not in transcripts</span><b>+{}</b></div>'.format(money(q["not_logged"])))
    p.append('<div class="stat"><span>updated</span><b>{}</b></div>'.format(hhmm(s["generated"])))
    p.append('</div>')
    p.append('<p class="note">{}</p>'.format(esc(ESTIMATE_LINE)))
    if q["open_sessions"]:
        p.append('<p class="note">{} session{} still open. Claude Code also makes calls it does not write to the transcripts '
                 '(compaction, titles, summaries, safety checks); their cost shows here once a session closes.</p>'.format(
                     q["open_sessions"], "" if q["open_sessions"] == 1 else "s"))
    if warning:
        p.append('<p class="note warn">Warning: {}</p>'.format(esc(warning)))
    p.append('<p class="note warn">{}</p>'.format(esc(PRIVACY_LINE)))

    # where the money went
    p.append('<section><h2>Where the money went</h2><p class="lead">What the day\'s cost paid for.</p>')
    if s["total"] > 0:
        p.append('<div class="split">')
        for i, t in enumerate(sorted(s["token_types"], key=lambda t: -t["cost"])):
            if t["cost"] > 0:
                p.append('<i class="s{}" style="width:{:.3f}%" title="{}: {} ({:.1f}%)"></i>'.format(
                    min(i + 1, 4), t["percent"], esc(t["label"]), money(t["cost"]), t["percent"]))
        p.append('</div>')
    rank = {t["key"]: min(i + 1, 4) for i, t in enumerate(sorted(s["token_types"], key=lambda t: -t["cost"]))}
    p.append('<div class="scroll"><table><tr><th>Paid for</th><th class="r">Tokens</th>'
             '<th class="r">Cost</th><th class="r">Share</th></tr>')
    for t in s["token_types"]:
        sw = '<i class="sw s{}"></i>'.format(rank[t["key"]]) if t["cost"] > 0 else '<i class="sw"></i>'
        p.append('<tr><td class="p">{}{}<div class="dim small">{}</div></td>{}{}{}</tr>'.format(
            sw, esc(t["label"]), esc(t["explain"]), _td(num(t["tokens"]) if t["key"] != "searches" else num(t["tokens"]) + " searches"),
            _td(money(t["cost"])), _td("{:.1f}%".format(t["percent"]))))
    p.append('</table></div></section>')

    # by model
    p.append('<section><h2>By model</h2><p class="lead">Every model used in the last {} days, today first. '
             'Tokens are for this day.</p>'.format(s["days_shown"]))
    p.append('<div class="scroll"><table><tr><th>Model</th><th class="r">This day</th><th class="r">Responses</th>'
             '<th class="r">Writing</th><th class="r">Re-reading</th><th class="r">Saving 5m</th><th class="r">Saving 1h</th>'
             '<th class="r">New input</th><th class="r">{}-day total</th><th class="r">Not in transcripts*</th></tr>'.format(s["days_shown"]))
    for m in s["models"]:
        t = m["tokens"]
        p.append('<tr><td class="nw">{}{}</td>{}{}{}{}{}{}{}{}{}</tr>'.format(
            esc(m["model"]), "" if m["priced"] else '<span class="tag">unpriced</span>',
            _td(money(m["cost"]) if m["priced"] else "unpriced"), _td(num(m["responses"])),
            _td(num(t["output"])), _td(num(t["cache_read"])), _td(num(t["cache_write_5m"])),
            _td(num(t["cache_write_1h"])), _td(num(t["input"])),
            _td(money(m["window_cost"]) if m["priced"] else "unpriced"),
            _td(money(m["window_not_logged"]) if m["window_not_logged"] else "")))
    if not s["models"]:
        p.append('<tr><td colspan="10" class="dim">No usage in this window.</td></tr>')
    p.append('</table></div><p class="note">* From Claude Code\'s own tally, over the {} days, for sessions that have closed. '
             'Unpriced models have no entry in prices.json and are not in any total.</p></section>'.format(s["days_shown"]))

    # sessions
    p.append('<section><h2>Biggest sessions</h2><p class="lead">The {} costliest sessions this day, subagents included.</p>'.format(TOP_SESSIONS))
    p.append('<div class="scroll"><table><tr><th>Started</th><th>Project</th><th>First prompt</th><th>Models</th>'
             '<th class="r">Main</th><th class="r">Subagents</th><th class="r">Agents</th><th class="r">Total</th>'
             '<th class="r">Not in transcripts</th></tr>')
    for r in s["sessions"][:TOP_SESSIONS]:
        badge = '<span class="tag run">running</span>' if r["running"] else ""
        nl = "open" if r["not_logged"] is None else (money(r["not_logged"]) if r["not_logged"] else "")
        p.append('<tr><td class="nw">{}</td><td class="nw">{}</td><td class="p">{}{}</td><td class="nw">{}</td>{}{}{}{}{}</tr>'.format(
            esc(when(r["start"], target)), esc(r["project"]), esc(r["prompt"]), badge,
            esc(", ".join(m.replace("claude-", "") for m in r["models"])),
            _td(money(r["main"])), _td(money(r["sub"])), _td(num(r["subagents"])),
            _td("<b>{}</b>".format(money(r["total"]))), _td(nl)))
    if not s["sessions"]:
        p.append('<tr><td colspan="9" class="dim">No activity on this day.</td></tr>')
    p.append('</table></div></section>')

    # subagents
    p.append('<section><h2>Biggest subagents</h2><p class="lead">The {} costliest subagents this day, across all sessions, '
             'priced by the model that actually answered.</p>'.format(TOP_SUBAGENTS))
    p.append('<div class="scroll"><table><tr><th>Name</th><th>Model used</th><th>Session</th><th class="r">Cost</th>'
             '<th class="r">Tokens</th><th class="r">Responses</th><th>Time</th><th class="r">Took</th><th>Status</th></tr>')
    for r in s["subagents"][:TOP_SUBAGENTS]:
        tk = r["tokens"]
        tip = "output {}, cache reads {}, cache writes {}, input {}".format(
            num(tk["output"]), num(tk["cache_read"]), num(cache_writes(tk)), num(tk["input"]))
        req = r["requested_model"]
        asked = (' <span class="dim">(asked for {})</span>'.format(esc(req))
                 if isinstance(req, str) and req not in ("inherit", "") and not any(req in m for m in r["models"]) else "")
        name = esc(r["name"]) if r["named"] else '<span class="dim">{}</span>'.format(esc(r["name"]))
        p.append('<tr><td class="p">{}</td><td class="nw">{}{}</td><td class="p dim">{}</td>{}<td class="r" title="{}">{}</td>{}'
                 '<td class="nw">{}-{}</td>{}<td>{}</td></tr>'.format(
                     name, esc(", ".join(m.replace("claude-", "") for m in r["models"])), asked,
                     esc(cut(r["session_prompt"] or r["session"], 48)), _td(money(r["cost"])),
                     esc(tip), num(sum(tk[k] for k in PRICE_KEYS)), _td(num(r["responses"])),
                     hhmm(r["start"]), hhmm(r["end"]), _td(duration(r["duration_s"])),
                     '<span class="tag run">running</span>' if r["status"] == "running" else esc(r["status"])))
    if not s["subagents"]:
        p.append('<tr><td colspan="9" class="dim">No subagents on this day.</td></tr>')
    p.append('</table></div></section>')

    # projects
    p.append('<section><h2>By project</h2><p class="lead">Spend by project folder this day.</p>')
    p.append('<div class="scroll"><table><tr><th>Project folder</th><th class="r">Sessions</th><th class="r">Subagents</th><th class="r">Cost</th></tr>')
    for r in s["projects"]:
        p.append('<tr><td class="nw">{}</td>{}{}{}</tr>'.format(esc(r["project"]), _td(num(r["sessions"])), _td(num(r["subagents"])), _td(money(r["cost"]))))
    if not s["projects"]:
        p.append('<tr><td colspan="4" class="dim">No activity on this day.</td></tr>')
    p.append('</table></div></section>')

    # hours
    hstacks = [_stack(h["by_model"], series) for h in s["hours"]]
    has_other = any(c == "s4" for st in hstacks for c, _, _ in st)
    p.append('<section><h2>By hour</h2><p class="lead">Local time. The busiest hours are labelled; hover a bar for its models.</p>{}<div class="scroll chart">{}</div></section>'.format(
        legend(series, has_other),
        stacked_chart(["{:02d}".format(h["hour"]) for h in s["hours"]], hstacks, [h["total"] for h in s["hours"]],
                      label_every=1, show_totals=False)))

    # days
    dstacks = [_stack(dd["by_model"], series) for dd in s["days"]]
    has_other = any(c == "s4" for st in dstacks for c, _, _ in st)
    gaps = [dd["not_logged"] for dd in s["days"]]
    n = len(s["days"])
    labels = [datetime.strptime(dd["date"], "%Y-%m-%d").strftime("%a %d" if n <= 14 else "%m-%d") for dd in s["days"]]
    p.append('<section><h2>Last {} days</h2><p class="lead">Each bar is labelled with its logged total.</p>{}<div class="scroll{}">{}</div>'.format(
        n, legend(series, has_other, any(g > 0 for g in gaps)), "" if n <= 14 else " chart",
        stacked_chart(labels, dstacks, [dd["total"] for dd in s["days"]], extra=gaps,
                      label_every=1 if n <= 14 else max(1, n // 10), show_totals=n <= 14)))
    p.append('<div class="scroll"><table class="narrow"><tr><th>Day</th><th class="r">Logged</th><th class="r">Not in transcripts</th>'
             '<th class="r">Open sessions</th></tr>')
    for dd, lab in zip(reversed(s["days"]), reversed(labels)):
        p.append('<tr><td class="nw">{}</td>{}{}{}</tr>'.format(esc(lab), _td(money(dd["total"])),
                                                    _td(money(dd["not_logged"]) if dd["not_logged"] else ""),
                                                    _td(num(dd["open_sessions"]) if dd["open_sessions"] else "")))
    p.append('</table></div></section>')

    # data quality
    p.append('<section><h2>Data quality</h2><p class="lead">What was read and what could not be counted, for this day.</p><dl>')
    rows = [
        ("Files read", num(q["files_read"])),
        ("Assistant lines read", num(q["assistant_lines"])),
        ("Responses after dedupe", num(q["responses"])),
        ("Zero-usage lines skipped", num(q["zero_usage_skipped"])),
        ("Requests with no usage logged", "{}{}".format(num(q["requests_no_usage"]),
                                                       " of {} ({})".format(num(q["requests_logged"]), why_text(q["requests_no_usage_why"]))
                                                       if q["requests_no_usage"] else " of {}".format(num(q["requests_logged"])))),
        ("Compactions", "{} ({} tokens summarized; usage not written to transcripts)".format(num(q["compactions"]), num(q["compacted_tokens"]))),
        ("Not in transcripts", "{} from Claude Code's own tally for closed sessions{}".format(
            money(q["not_logged"]), "; {} session(s) still open, not known yet".format(q["open_sessions"]) if q["open_sessions"] else "")),
        ("Unpriced models", ", ".join(q["unpriced_models"]) or "none"),
        ("Prices", "read {} ({} days ago) from {}".format(q["prices_date"], q["prices_age_days"], q["prices_source"])),
    ]
    if q["dupe_conflicts"]:
        rows.insert(3, ("Written twice, usage differed", "{} (last kept)".format(num(q["dupe_conflicts"]))))
    if q["unreadable_lines"]:
        rows.insert(1, ("Unreadable lines skipped", num(q["unreadable_lines"])))
    for k, v in rows:
        p.append("<dt>{}</dt><dd>{}</dd>".format(esc(k), esc(v)))
    p.append('</dl><p class="note">Nothing on this page leaves this computer unless you publish it. {}</p></section>'.format(esc(PRIVACY_LINE)))
    p.append("</main></body></html>")
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
    data = scan(root, first, target, prices)
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
