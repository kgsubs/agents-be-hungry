#!/usr/bin/env python3
"""claude-spend: estimate Claude Code spend from local session transcripts.

Reads ~/.claude/projects/**.jsonl (or $CLAUDE_CONFIG_DIR/projects), read only.
Makes no network calls. Python 3 standard library only.
Works on macOS, Linux and Windows.

  python3 spend.py                 today's total, by model, top 5 sessions
  python3 spend.py --html          write dashboard.html and open it
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
RUNNING_SECONDS = 300
STALE_DAYS = 60
PRICE_KEYS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


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
    return x if isinstance(x, (int, float)) else 0


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


def cost_of(prices, price, tok, usage):
    """Dollar cost of one response, or None if the model has no price."""
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
    c = sum(tok[k] * p[k] for k in PRICE_KEYS) / 1e6 * mult
    c += tok["searches"] * prices.get("web_search_per_request", 0.0)
    return c


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


def _listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def session_files(root, min_mtime):
    """{(project, session_id): {'main': path or None, 'subs': [paths]}} for sessions
    that have at least one file modified at or after min_mtime. Uses os.listdir, not
    glob, so folder names with [ ] * ? characters work."""
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
                sub = os.path.join(full, "subagents")
                for sname in _listdir(sub):
                    if sname.startswith("agent-") and sname.endswith(".jsonl"):
                        sessions.setdefault((pname, name), {"main": None, "subs": []})["subs"].append(
                            os.path.join(sub, sname))
    keep = {}
    for k, v in sessions.items():
        files = ([v["main"]] if v["main"] else []) + v["subs"]
        try:
            if max(os.path.getmtime(f) for f in files) >= min_mtime:
                keep[k] = v
        except OSError:
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


def scan(root, first_day, last_day, prices):
    """Read transcripts. Returns dict with deduplicated records and counts.

    Record: dict(day, hour, model, session, is_sub, cost or None, tok)
    """
    start_local = datetime.combine(first_day, datetime.min.time()).astimezone()
    sessions = session_files(root, start_local.timestamp() - 86400)
    seen = set()
    records = []
    meta = {}  # (project, sid) -> dict(start, prompt, mtime)
    lines_by_day = defaultdict(int)
    for key in sorted(sessions):
        v = sessions[key]
        files = ([v["main"]] if v["main"] else []) + sorted(v["subs"])
        info = meta.setdefault(key, {"start": None, "prompt": None, "mtime": 0.0})
        for f in files:
            is_sub = f != v["main"]
            try:
                info["mtime"] = max(info["mtime"], os.path.getmtime(f))
                fh = open(f, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in _safe_lines(fh):
                    try:
                        o = json.loads(line)
                    except ValueError:  # half-written or corrupt line: skip
                        continue
                    if not isinstance(o, dict):
                        continue
                    t = o.get("type")
                    ts = parse_ts(o.get("timestamp"))
                    if not is_sub and ts is not None and (info["start"] is None or ts < info["start"]):
                        info["start"] = ts
                    if t == "user" and not is_sub and info["prompt"] is None and not o.get("isMeta"):
                        txt = first_text(o.get("message"))
                        if txt and not txt.lstrip().startswith(("<command-", "<local-command", "<system-reminder")):
                            info["prompt"] = clean_prompt(txt)
                    if t != "assistant" or ts is None:
                        continue
                    msg = o.get("message")
                    if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                        continue
                    d = ts.date()
                    in_range = first_day <= d <= last_day
                    if in_range:
                        lines_by_day[d] += 1
                    mid, rid = msg.get("id"), o.get("requestId")
                    if mid:
                        k2 = (mid, rid)
                        if k2 in seen:
                            continue
                        seen.add(k2)
                    if not in_range:
                        continue
                    usage = msg["usage"]
                    tok = usage_numbers(usage)
                    model = msg.get("model") or "unknown"
                    price = find_price(prices, model)
                    cost = cost_of(prices, price, tok, usage)
                    records.append({"day": d, "hour": ts.hour, "model": model, "session": key,
                                    "is_sub": is_sub, "cost": cost, "tok": tok})
    return {"records": records, "meta": meta, "lines_by_day": lines_by_day}


# ---------------------------------------------------------------- summary

def summarize(data, target):
    recs = data["records"]
    today = [r for r in recs if r["day"] == target]
    s = {"total": 0.0, "models": {}, "sessions": {}, "hours": [0.0] * 24,
         "days": defaultdict(float), "tokens": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
         "searches": 0, "lines_read": data["lines_by_day"].get(target, 0), "unique_all": len(recs), "unique_today": len(today)}
    for r in recs:
        if r["cost"] is not None:
            s["days"][r["day"]] += r["cost"]
    for r in today:
        t = r["tok"]
        m = s["models"].setdefault(r["model"], {"cost": 0.0, "priced": r["cost"] is not None, "n": 0,
                                                "input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
        m["n"] += 1
        m["input"] += t["input"]
        m["output"] += t["output"]
        m["cache_write"] += t["cache_write_5m"] + t["cache_write_1h"]
        m["cache_read"] += t["cache_read"]
        s["tokens"]["input"] += t["input"]
        s["tokens"]["output"] += t["output"]
        s["tokens"]["cache_write"] += t["cache_write_5m"] + t["cache_write_1h"]
        s["tokens"]["cache_read"] += t["cache_read"]
        s["searches"] += t["searches"]
        ses = s["sessions"].setdefault(r["session"], {"main": 0.0, "sub": 0.0, "unpriced": 0})
        if r["cost"] is None:
            if sum(t[k] for k in PRICE_KEYS) > 0:
                ses["unpriced"] += 1
            continue
        m["cost"] += r["cost"]
        s["total"] += r["cost"]
        s["hours"][r["hour"]] += r["cost"]
        ses["sub" if r["is_sub"] else "main"] += r["cost"]
    # drop models that have no tokens and no price (empty synthetic lines)
    for name in list(s["models"]):
        m = s["models"][name]
        if not m["priced"] and (m["input"] + m["output"] + m["cache_write"] + m["cache_read"]) == 0:
            del s["models"][name]
    rows = []
    now = time.time()
    for key, v in s["sessions"].items():
        info = data["meta"].get(key, {})
        total = v["main"] + v["sub"]
        if total == 0 and not v["unpriced"]:
            continue
        rows.append({"project": key[0], "session": key[1], "start": info.get("start"),
                     "prompt": info.get("prompt") or "", "main": v["main"], "sub": v["sub"],
                     "total": total, "unpriced": v["unpriced"],
                     "running": target == date.today() and (now - info.get("mtime", 0)) <= RUNNING_SECONDS})
    rows.sort(key=lambda r: r["total"], reverse=True)
    s["rows"] = rows
    return s


def money(x):
    return "${:,.2f}".format(x)


def num(x):
    return "{:,}".format(int(x))


# ---------------------------------------------------------------- terminal

def render_text(s, target):
    out = []
    out.append("Claude Code spend on {}: {}".format(target.isoformat(), money(s["total"])))
    out.append(ESTIMATE_LINE)
    out.append("")
    out.append("By model")
    for name, m in sorted(s["models"].items(), key=lambda kv: kv[1]["cost"], reverse=True):
        if m["priced"]:
            out.append("  {:<22} {:>10}   {} responses".format(name, money(m["cost"]), num(m["n"])))
        else:
            out.append("  {:<22} {:>10}   {} responses; tokens in {} out {} cache write {} cache read {}".format(
                name, "unpriced", num(m["n"]), num(m["input"]), num(m["output"]),
                num(m["cache_write"]), num(m["cache_read"])))
    out.append("")
    out.append("Top 5 sessions")
    for r in s["rows"][:5]:
        st = r["start"].strftime("%m-%d %H:%M") if r["start"] else "--:--"
        out.append("  {:>9}  main {:>9}  subagents {:>9}  {}  {}  {}{}".format(
            money(r["total"]), money(r["main"]), money(r["sub"]), st, r["project"],
            r["prompt"], "  [running]" if r["running"] else ""))
    out.append("")
    t = s["tokens"]
    out.append("Tokens: input {}  output {}  cache writes {}  cache reads {}".format(
        num(t["input"]), num(t["output"]), num(t["cache_write"]), num(t["cache_read"])))
    out.append("Assistant lines read on that day: {}  after dedupe: {}".format(
        num(s["lines_read"]), num(s["unique_today"])))
    return "\n".join(out)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mute:#667085;--line:#e4e7ec;--bar:#4f6bed;--bar2:#98a8f5;--ok:#1a7f4b;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--ink:#e8ecf3;--mute:#9aa4b5;--line:#2b3240;--bar:#7b93ff;--bar2:#4a5aa8;--ok:#4ade80;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px}
h1{margin:0;font-size:14px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
h2{margin:0 0 12px;font-size:14px;font-weight:600;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.big{font-size:64px;font-weight:700;line-height:1.1;margin:6px 0}
.mute{color:var(--mute)}.small{font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{border:1px solid var(--line);border-radius:10px;padding:12px}
.tile b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--ok);color:#fff;font-size:12px;margin-left:6px}
@media (prefers-color-scheme:dark){.badge{color:#062b14}}
.unp{color:var(--warn)}
svg{width:100%;height:auto;display:block}
svg text{fill:var(--mute);font-size:11px}
svg .bar{fill:var(--bar)}svg .bar2{fill:var(--bar2)}
@media (max-width:600px){.big{font-size:44px}}
"""


def bar_chart(labels, values, highlight=None, fmt=money):
    w, h, pad_b, pad_t = 900, 220, 26, 20
    n = len(values)
    mx = max(values) if values and max(values) > 0 else 1.0
    slot = w / max(n, 1)
    bw = slot * 0.7
    parts = ['<svg viewBox="0 0 {} {}" role="img" aria-label="bar chart">'.format(w, h)]
    parts.append('<line x1="0" y1="{0}" x2="{1}" y2="{0}" stroke="currentColor" opacity=".2"/>'.format(h - pad_b, w))
    for i, (lab, v) in enumerate(zip(labels, values)):
        bh = (h - pad_b - pad_t) * (v / mx)
        x = i * slot + (slot - bw) / 2
        y = h - pad_b - bh
        cls = "bar" if (highlight is None or i == highlight) else "bar2"
        parts.append('<rect class="{}" x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="3"><title>{}: {}</title></rect>'.format(
            cls, x, y, bw, max(bh, 0), html.escape(str(lab)), fmt(v)))
        if v > 0:
            parts.append('<text x="{:.1f}" y="{:.1f}" text-anchor="middle">{}</text>'.format(
                x + bw / 2, y - 4, fmt(v)))
        if n <= 12 or i % 3 == 0:
            parts.append('<text x="{:.1f}" y="{}" text-anchor="middle">{}</text>'.format(
                x + bw / 2, h - 8, html.escape(str(lab))))
    parts.append("</svg>")
    return "".join(parts)


def render_html(s, target, ndays, refresh, warning=None):
    esc = html.escape
    now = datetime.now().astimezone()
    p = []
    p.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    p.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    if refresh:
        p.append("<meta http-equiv=\"refresh\" content=\"60\">")
    p.append("<title>Claude spend</title><style>{}</style></head><body><main>".format(CSS))
    # 1 + 8
    p.append("<div class=\"card\"><h1>Estimated Claude Code spend, {}</h1>".format(target.isoformat()))
    p.append("<div class=\"big\">{}</div>".format(money(s["total"])))
    p.append("<div class=\"mute small\">Updated {}</div>".format(now.strftime("%H:%M:%S %Z")))
    p.append("<p class=\"mute small\">{}</p>".format(esc(ESTIMATE_LINE)))
    if warning:
        p.append("<p class=\"unp small\"><b>Warning:</b> {}</p>".format(esc(warning)))
    p.append("</div>")
    # 6
    t = s["tokens"]
    p.append("<div class=\"card\"><h2>Tokens</h2><div class=\"tiles\">")
    for lab, key in (("Input", "input"), ("Output", "output"), ("Cache writes", "cache_write"), ("Cache reads", "cache_read")):
        p.append("<div class=\"tile\"><span class=\"mute small\">{}</span><b>{}</b></div>".format(lab, num(t[key])))
    p.append("</div></div>")
    # 2
    p.append("<div class=\"card\"><h2>By model</h2><table><tr><th>Model</th><th class=\"r\">Responses</th><th class=\"r\">Cost</th></tr>")
    for name, m in sorted(s["models"].items(), key=lambda kv: kv[1]["cost"], reverse=True):
        if m["priced"]:
            p.append("<tr><td>{}</td><td class=\"r\">{}</td><td class=\"r\">{}</td></tr>".format(esc(name), num(m["n"]), money(m["cost"])))
    p.append("</table>")
    unp = [(n, m) for n, m in s["models"].items() if not m["priced"]]
    if unp:
        p.append("<h2 style=\"margin-top:16px\">Unpriced</h2><table><tr><th>Model</th><th class=\"r\">Input</th><th class=\"r\">Output</th><th class=\"r\">Cache writes</th><th class=\"r\">Cache reads</th></tr>")
        for n, m in unp:
            p.append("<tr><td class=\"unp\">{}</td><td class=\"r\">{}</td><td class=\"r\">{}</td><td class=\"r\">{}</td><td class=\"r\">{}</td></tr>".format(
                esc(n), num(m["input"]), num(m["output"]), num(m["cache_write"]), num(m["cache_read"])))
        p.append("</table><p class=\"mute small\">These models have no entry in prices.json, so they are not in the total.</p>")
    p.append("</div>")
    # 4
    p.append("<div class=\"card\"><h2>By hour (local time)</h2>{}</div>".format(
        bar_chart(["{:02d}".format(h) for h in range(24)], s["hours"])))
    # 5
    days = [target - timedelta(days=i) for i in range(ndays - 1, -1, -1)]
    p.append("<div class=\"card\"><h2>Last {} days</h2>{}</div>".format(
        ndays, bar_chart([d.strftime("%a %d") if ndays <= 14 else d.strftime("%m-%d") for d in days],
                         [s["days"].get(d, 0.0) for d in days], highlight=len(days) - 1)))
    # 3 + 7
    p.append("<div class=\"card\"><h2>By session</h2><table><tr><th>Project</th><th>Started</th><th>First prompt</th><th class=\"r\">Main</th><th class=\"r\">Subagents</th><th class=\"r\">Total</th></tr>")
    for r in s["rows"]:
        st = r["start"].strftime("%Y-%m-%d %H:%M") if r["start"] else ""
        badge = "<span class=\"badge\">running</span>" if r["running"] else ""
        p.append("<tr><td>{}</td><td>{}</td><td>{}{}</td><td class=\"r\">{}</td><td class=\"r\">{}</td><td class=\"r\"><b>{}</b></td></tr>".format(
            esc(r["project"]), esc(st), esc(r["prompt"]), badge, money(r["main"]), money(r["sub"]), money(r["total"])))
    if not s["rows"]:
        p.append("<tr><td colspan=\"6\" class=\"mute\">No activity on this day.</td></tr>")
    p.append("</table></div>")
    p.append("<p class=\"mute small\">Assistant lines read that day: {}. After dedupe: {}. Nothing leaves this computer.</p>".format(
        num(s["lines_read"]), num(s["unique_today"])))
    p.append("<p class=\"unp small\">This page contains your prompts and project folder names. "
             "Do not share it as-is.</p>")
    p.append("</main></body></html>")
    return "".join(p)


def write_html(s, target, ndays, refresh, out, warning=None):
    text = render_html(s, target, ndays, refresh, warning)
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out)


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
    return summarize(data, target)


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
    warning = stale_warning(prices)

    if not has_transcripts(root):
        print(no_transcripts_message(root))
        return 0

    def write(s, tgt, refresh):
        try:
            write_html(s, tgt, args.days, refresh, out, warning)
        except OSError as e:
            print("Cannot write {}: {}".format(out, e), file=sys.stderr)
            return False
        return True

    if args.watch:
        opened = False
        try:
            while True:
                tgt = target if args.date else date.today()
                s = run_once(args, prices, tgt, root)
                if write(s, tgt, True):
                    print("{} wrote {} total {}".format(datetime.now().strftime("%H:%M:%S"), out, money(s["total"])), flush=True)
                    if not opened and not args.no_open:
                        try_open(out)
                    opened = True
                time.sleep(60)
        except KeyboardInterrupt:
            return 0
    s = run_once(args, prices, target, root)
    print(render_text(s, target))
    if warning:
        print("\nWarning: " + warning)
    if args.html:
        if not write(s, target, False):
            return 1
        print("\nDashboard: " + out)
        if not args.no_open and not try_open(out):
            print("Could not open a browser here. Open the file above yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
