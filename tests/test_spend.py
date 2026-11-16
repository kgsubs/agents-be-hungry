"""Tests for spend.py. Fixtures are small made-up transcripts, no real data."""
import contextlib
import html
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import spend  # noqa: E402

PRICES = spend.load_prices()


def iso(local_dt):
    """Local naive datetime -> UTC ISO string like the transcripts use."""
    return local_dt.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def today_at(h, m=0):
    return datetime.combine(date.today(), datetime.min.time()).replace(hour=h, minute=m)


def line(mid, rid, model="claude-sonnet-5-5", ts=None, inp=0, out=0, cr=0, w5=0, w1=0, ref=None,
         iterations=None, top=None, **extra):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": w5 + w1,
             "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}
    if iterations is not None:
        usage["iterations"] = iterations
    usage.update(extra)
    o = {"type": "assistant", "timestamp": iso(ts or today_at(12)), "requestId": rid,
         "message": {"id": mid, "model": model, "usage": usage}}
    if ref:
        o["requestRef"] = ref
    o.update(top or {})
    return json.dumps(o)


def user(text, ts=None, **extra):
    o = {"type": "user", "timestamp": iso(ts or today_at(11)), "message": {"role": "user", "content": text}}
    o.update(extra)
    return json.dumps(o)


def request(rid, ts=None):
    return json.dumps({"type": "api-request", "id": rid, "timestamp": iso(ts or today_at(12)),
                       "querySource": "repl_main_thread"})


def cost_state(total, models, ts=None):
    return json.dumps({"type": "cost-state", "totalCostUSD": total,
                       "modelUsage": {m: {"costUSD": c} for m, c in models.items()}})


def system(ts=None, **extra):
    o = {"type": "system", "timestamp": iso(ts or today_at(12))}
    o.update(extra)
    return json.dumps(o)


class Fixture:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def close(self):
        self._tmp.cleanup()

    def write(self, rel, lines, trailing_newline=True):
        p = Path(self.root, "projects", *rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + ("\n" if trailing_newline else ""), encoding="utf-8")
        return p

    def meta(self, rel, **fields):
        p = Path(self.root, "projects", *rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(fields), encoding="utf-8")


def run_summary(root, day=None, ndays=7):
    day = day or date.today()
    first = day - timedelta(days=ndays - 1)
    data = spend.scan(root, first, day, PRICES)
    return data, spend.summarize(data, day, ndays)


def model(s, name):
    return next(m for m in s["models"] if m["model"] == name)


def day(s, d):
    return next(x for x in s["days"] if x["date"] == d.isoformat())


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_dedupe_counts_each_response_once(self):
        l = line("m1", "r1", inp=1_000_000, out=100_000)
        self.fx.write("proj/sess1.jsonl", [user("hello"), l, l, l, line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertEqual(s["quality"]["assistant_lines"], 4)
        self.assertEqual(s["quality"]["responses"], 2)
        self.assertAlmostEqual(s["total"], 3.00 + 2.00, places=6)

    def test_dedupe_across_files(self):
        l = line("m1", "r1", inp=1_000_000)
        self.fx.write("proj/sess1.jsonl", [l])
        self.fx.write("proj/sess1/subagents/agent-a.jsonl", [l])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_duplicate_with_different_usage_keeps_last(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1_000_000, out=10),
                                       line("m1", "r1", inp=1_000_000, out=100_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00 + 1.00, places=6)
        self.assertEqual(s["quality"]["responses"], 1)
        self.assertEqual(s["quality"]["dupe_conflicts"], 1)

    def test_subagent_attributed_to_parent_session(self):
        self.fx.write("proj/sessA.jsonl", [user("do the thing"), line("m1", "r1", inp=1_000_000)])
        self.fx.write("proj/sessA/subagents/agent-1.jsonl", [line("m2", "r2", inp=500_000)])
        self.fx.write("proj/sessA/subagents/agent-2.jsonl", [line("m3", "r3", inp=500_000)])
        _, s = run_summary(self.fx.root)
        self.assertEqual(len(s["sessions"]), 1)
        row = s["sessions"][0]
        self.assertEqual(row["session"], "sessA")
        self.assertAlmostEqual(row["main"], 2.00, places=6)
        self.assertAlmostEqual(row["sub"], 2.00, places=6)
        self.assertAlmostEqual(row["total"], 4.00, places=6)
        self.assertEqual(row["subagents"], 2)
        self.assertEqual(row["prompt"], "do the thing")

    def test_subagent_named_from_meta(self):
        self.fx.write("proj/s.jsonl", [user("parent prompt"), line("m1", "r1", inp=1000)])
        self.fx.write("proj/s/subagents/agent-abc.jsonl", [
            user("brief for the agent", ts=today_at(9)),
            line("m2", "r2", model="claude-opus-5-5", ts=today_at(9, 5), inp=1_000_000),
            line("m3", "r3", model="claude-opus-5-5", ts=today_at(9, 35), out=1000)])
        self.fx.meta("proj/s/subagents/agent-abc.meta.json", description="PRICE CHECK [sonnet]",
                     agentType="general-purpose", model="sonnet", finished=True)
        _, s = run_summary(self.fx.root)
        a = s["subagents"][0]
        self.assertEqual(a["name"], "PRICE CHECK [sonnet]")
        self.assertTrue(a["named"])
        self.assertEqual(a["requested_model"], "sonnet")
        self.assertEqual(a["models"], ["claude-opus-5-5"])      # priced by the model that answered
        self.assertAlmostEqual(a["cost"], 4.00 + 0.02, places=6)
        self.assertEqual(a["responses"], 2)
        self.assertEqual(a["status"], "finished")
        self.assertEqual(a["duration_s"], 35 * 60)
        self.assertEqual(a["session_prompt"], "parent prompt")
        self.assertIn("PRICE CHECK [sonnet]", spend.render_html(s))
        self.assertIn("PRICE CHECK [sonnet]", spend.render_text(s))

    def test_subagent_without_meta(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1000)])
        self.fx.write("proj/s/subagents/agent-zz9.jsonl", [user("look up the weather"), line("m2", "r2", inp=1000)])
        self.fx.write("proj/s/subagents/agent-yy8.jsonl", [line("m3", "r3", inp=1000)])
        _, s = run_summary(self.fx.root)
        names = {a["id"]: a for a in s["subagents"]}
        self.assertEqual(names["zz9"]["name"], "look up the weather")
        self.assertFalse(names["zz9"]["named"])
        self.assertEqual(names["yy8"]["name"], "agent-yy8")
        self.assertFalse(names["yy8"]["has_meta"])
        self.assertIn(names["yy8"]["status"], ("running", "stopped"))

    def test_nested_subagent_folder_counted(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1000)])
        self.fx.write("proj/s/subagents/workflows/wf_1/agent-n1.jsonl", [line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["sessions"][0]["sub"], 2.00, places=6)

    def test_token_type_split(self):
        self.fx.write("proj/s.jsonl", [
            line("m1", "r1", model="claude-opus-5-5", inp=1_000_000, out=1_000_000, cr=1_000_000,
                 w5=1_000_000, w1=1_000_000, server_tool_use={"web_search_requests": 3})])
        _, s = run_summary(self.fx.root)
        split = {t["key"]: t for t in s["token_types"]}
        self.assertEqual([t["key"] for t in s["token_types"]],
                         ["output", "cache_read", "cache_write_5m", "cache_write_1h", "input", "searches", "hidden"])
        self.assertAlmostEqual(split["output"]["cost"], 20.0)
        self.assertAlmostEqual(split["cache_read"]["cost"], 0.2)
        self.assertAlmostEqual(split["cache_write_5m"]["cost"], 5.0)
        self.assertAlmostEqual(split["cache_write_1h"]["cost"], 8.0)
        self.assertAlmostEqual(split["input"]["cost"], 4.0)
        self.assertAlmostEqual(split["searches"]["cost"], 0.03)
        self.assertEqual(split["searches"]["tokens"], 3)
        self.assertAlmostEqual(sum(t["cost"] for t in s["token_types"]), s["total"])
        self.assertAlmostEqual(sum(t["percent"] for t in s["token_types"]), 100.0)
        self.assertAlmostEqual(split["output"]["percent"], 100 * 20 / 37.23)

    def test_all_models_listed_including_zero_today(self):
        yday = today_at(12) - timedelta(days=1)
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="claude-haiku-5-5", ts=yday, inp=50_000),
                                       line("m2", "r2", model="claude-opus-5-5", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        names = [m["model"] for m in s["models"]]
        self.assertEqual(names, ["claude-opus-5-5", "claude-haiku-5-5"])
        h = model(s, "claude-haiku-5-5")
        self.assertEqual(h["cost"], 0.0)
        self.assertEqual(h["responses"], 0)
        self.assertAlmostEqual(h["window_cost"], 0.005, places=6)
        self.assertIn("Haiku 5.5", spend.render_text(s))
        page = spend.render_html(s)
        self.assertIn("Haiku 5.5", page)
        self.assertIn('title="claude-haiku-5-5"', page)       # full id kept as a tooltip

    def test_unknown_model_goes_to_unpriced(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="claude-future-9", inp=1000, out=500),
                                       line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        m = model(s, "claude-future-9")
        self.assertFalse(m["priced"])
        self.assertEqual(m["tokens"]["input"], 1000)
        self.assertAlmostEqual(s["total"], 2.00, places=6)
        self.assertIn("claude-future-9", s["quality"]["unpriced_models"])
        text = spend.render_text(s)
        self.assertIn("claude-future-9", text)
        self.assertIn("no price", text)
        self.assertIn("no price", spend.render_html(s))

    def test_synthetic_lines_ignored(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="<synthetic>"),
                                       line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertNotIn("<synthetic>", [m["model"] for m in s["models"]])
        self.assertEqual(s["quality"]["zero_usage_skipped"], 1)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_date_suffix_model_is_priced(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="claude-sonnet-5-5-20261101", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_haiku_long_context_tier(self):
        self.fx.write("proj/s.jsonl", [
            line("m1", "r1", model="claude-haiku-5-5", inp=150_000),   # over 100k: 0.5 per M
            line("m2", "r2", model="claude-haiku-5-5", inp=50_000)])   # under: 0.1 per M
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 0.075 + 0.005, places=6)

    def test_cache_prices_and_fast_and_us(self):
        self.fx.write("proj/s.jsonl", [
            line("m1", "r1", model="claude-opus-5-5", w5=1_000_000, w1=1_000_000, cr=1_000_000),
            line("m2", "r2", model="claude-opus-5-5", inp=1_000_000, speed="fast"),
            line("m3", "r3", model="claude-sonnet-5-5", inp=1_000_000, inference_geo="us")])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], (5 + 8 + 0.2) + 4 * 2 + 2 * 1.1, places=6)

    def test_single_iteration_matches_top_level(self):
        it = [{"type": "message", "input_tokens": 1_000_000, "output_tokens": 0}]
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1_000_000, iterations=it)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_advisor_iteration_priced_at_advisor_model(self):
        it = [{"type": "message", "input_tokens": 1_000_000, "output_tokens": 0},
              {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0}]
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1_000_000, iterations=it)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00 + 4.00, places=6)
        self.assertAlmostEqual(model(s, "claude-opus-5-5")["cost"], 4.00, places=6)
        self.assertEqual(model(s, "claude-opus-5-5")["responses"], 0)
        self.assertEqual(model(s, "claude-sonnet-5-5")["responses"], 1)

    def test_half_written_last_line_skipped(self):
        good = line("m1", "r1", inp=1_000_000)
        half = line("m2", "r2", inp=1_000_000)[:-25]
        self.fx.write("proj/s.jsonl", [good, half], trailing_newline=False)
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_garbage_lines_skipped(self):
        self.fx.write("proj/s.jsonl", ["not json", "[1,2]", "", line("m1", "r1", inp=1_000_000), "{"])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_date_filter_across_local_midnight(self):
        yesterday_late = today_at(23, 30) - timedelta(days=1)
        today_early = today_at(0, 30)
        self.fx.write("proj/s.jsonl", [
            line("m1", "r1", ts=yesterday_late, inp=1_000_000),
            line("m2", "r2", ts=today_early, inp=500_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 1.00, places=6)              # only after local midnight
        self.assertAlmostEqual(day(s, date.today())["total"], 1.00, places=6)
        self.assertAlmostEqual(day(s, date.today() - timedelta(days=1))["total"], 2.00, places=6)
        self.assertAlmostEqual(s["yesterday"], 2.00, places=6)
        self.assertAlmostEqual(s["change"], -1.00, places=6)
        self.assertAlmostEqual(s["average"], 3.00 / 7, places=6)
        self.assertEqual(s["hours"][0]["total"], 1.00)
        _, y = run_summary(self.fx.root, day=date.today() - timedelta(days=1))
        self.assertAlmostEqual(y["total"], 2.00, places=6)

    def test_requests_with_no_usage_counted(self):
        self.fx.write("proj/s.jsonl", [
            request("q1"), line("m1", "r1", inp=1000, ref="q1"),
            request("q2"), user("[Request interrupted by user]"),
            request("q3"), line("m3", "r3", inp=1000, ref="q3")] + [user("filler")] * 20 + [request("q4")])
        _, s = run_summary(self.fx.root)
        q = s["quality"]
        self.assertEqual(q["requests_logged"], 4)
        self.assertEqual(q["requests_no_usage"], 2)
        self.assertEqual(q["requests_no_usage_why"], {"interrupted": 1, "end of file": 1})
        self.assertEqual(q["requests_no_usage_by_day"][date.today().isoformat()], 2)
        self.assertIn("Calls with no cost saved", spend.render_text(s))
        self.assertIn("Calls with no token count", spend.render_html(s))

    def test_compactions_counted(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1000),
                                       system(subtype="compact_boundary", compactMetadata={"preTokens": 150000})])
        _, s = run_summary(self.fx.root)
        self.assertEqual(s["quality"]["compactions"], 1)
        self.assertEqual(s["quality"]["compacted_tokens"], 150000)

    def test_tally_gap_from_cost_state(self):
        # Claude Code's own tally says $3.00 for sonnet and $0.50 for haiku; transcripts logged $2.00.
        self.fx.write("proj/closed.jsonl", [line("m1", "r1", inp=1_000_000, ts=today_at(10)),
                                            cost_state(3.50, {"claude-sonnet-5-5": 3.00, "claude-haiku-5-5": 0.50})])
        self.fx.write("proj/open.jsonl", [line("m2", "r2", inp=1_000_000, ts=today_at(11))])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["logged_total"], 4.00, places=6)
        self.assertAlmostEqual(s["total"], 5.50, places=6)                  # logged 4.00 + hidden 1.50
        self.assertAlmostEqual(s["quality"]["not_logged"], 1.50, places=6)
        self.assertEqual(s["quality"]["open_sessions"], 1)
        self.assertAlmostEqual(model(s, "claude-haiku-5-5")["not_logged"], 0.50, places=6)
        rows = {r["session"]: r for r in s["sessions"]}
        self.assertAlmostEqual(rows["closed"]["not_logged"], 1.50, places=6)
        self.assertIsNone(rows["open"]["not_logged"])

    def test_total_is_logged_plus_hidden_for_closed_sessions(self):
        self.fx.write("proj/closed.jsonl", [line("m1", "r1", inp=1_000_000, ts=today_at(10)),
                                            cost_state(3.50, {"claude-sonnet-5-5": 3.00, "claude-haiku-5-5": 0.50})])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["logged_total"], 2.00, places=6)
        self.assertAlmostEqual(s["not_logged"], 1.50, places=6)
        self.assertAlmostEqual(s["total"], 3.50, places=6)                  # equals Claude Code's own tally
        self.assertAlmostEqual(s["total"], s["logged_total"] + s["not_logged"], places=6)
        self.assertAlmostEqual(day(s, date.today())["total"], 3.50, places=6)
        self.assertAlmostEqual(s["average"], 3.50 / 7, places=6)
        self.assertAlmostEqual(s["sessions"][0]["total"], 3.50, places=6)
        self.assertAlmostEqual(s["sessions"][0]["logged"], 2.00, places=6)
        self.assertAlmostEqual(s["projects"][0]["cost"], 3.50, places=6)
        self.assertAlmostEqual(model(s, "claude-sonnet-5-5")["cost"], 3.00, places=6)
        self.assertAlmostEqual(model(s, "claude-haiku-5-5")["cost"], 0.50, places=6)
        self.assertAlmostEqual(model(s, "claude-haiku-5-5")["window_cost"], 0.50, places=6)
        # the rows of "Where the money went" add up to the total, hidden row included
        self.assertAlmostEqual(sum(t["cost"] for t in s["token_types"]), s["total"], places=6)
        self.assertAlmostEqual(sum(t["percent"] for t in s["token_types"]), 100.0, places=6)
        hidden = next(t for t in s["token_types"] if t["key"] == "hidden")
        self.assertEqual(hidden["label"], "Hidden background calls")
        self.assertAlmostEqual(hidden["cost"], 1.50, places=6)
        self.assertIn("Background calls", spend.render_html(s))
        text = spend.render_text(s)
        self.assertTrue(text.splitlines()[0].endswith("$3.50"))
        self.assertIn("Includes $1.50 of hidden background calls (logged $2.00)", text)
        self.assertIn("Includes $1.50 in background calls. The logs show $2.00.", spend.render_html(s))
        self.assertNotIn("still open", text)

    def test_yesterday_and_change_include_hidden(self):
        yday = today_at(12) - timedelta(days=1)
        self.fx.write("proj/old.jsonl", [line("m1", "r1", inp=1_000_000, ts=yday),
                                         cost_state(5.00, {"claude-sonnet-5-5": 5.00})])
        self.fx.write("proj/new.jsonl", [line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["yesterday"], 5.00, places=6)
        self.assertAlmostEqual(s["change"], 2.00 - 5.00, places=6)

    def test_open_session_adds_nothing(self):
        self.fx.write("proj/open.jsonl", [line("m1", "r1", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)
        self.assertAlmostEqual(s["not_logged"], 0.0, places=6)
        self.assertEqual(s["open_sessions"], 1)
        self.assertIsNone(s["sessions"][0]["not_logged"])
        self.assertAlmostEqual(s["sessions"][0]["total"], 2.00, places=6)
        self.assertIn("1 session is still open; its background calls get added when it closes.", spend.render_text(s))
        self.assertIn("1 session is still open. Its background calls get added when it closes.", spend.render_html(s))
        self.assertNotIn("Includes", spend.render_text(s))

    def test_chart_bar_label_is_full_total(self):
        self.fx.write("proj/closed.jsonl", [line("m1", "r1", inp=1_000_000, ts=today_at(10)),
                                            cost_state(3.50, {"claude-sonnet-5-5": 3.50})])
        _, s = run_summary(self.fx.root)
        page = spend.render_html(s)
        self.assertIn('class="total" x=', page)
        self.assertRegex(page, r'class="total"[^>]*>\$3\.50</text>')
        self.assertIn('class="gapfill"', page)                       # striped segment stays inside the bar

    def test_json_shape(self):
        self.fx.write("proj/s.jsonl", [user("hi"), line("m1", "r1", inp=1000)])
        self.fx.write("proj/s/subagents/agent-a1.jsonl", [line("m2", "r2", inp=1000)])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = spend.main(["--json"])
        self.assertEqual(code, 0)
        j = json.loads(buf.getvalue())
        for k in ("date", "generated", "estimate_line", "total", "yesterday", "change", "average", "token_types",
                  "models", "sessions", "subagents", "projects", "hours", "days", "quality", "prices_warning", "tldr",
                  "logged_total", "not_logged", "open_sessions"):
            self.assertIn(k, j)
        self.assertNotIn(spend.PAGE_TLDR_KEY, j)                    # the page's wording stays off --json
        self.assertEqual(len(j["hours"]), 24)
        self.assertEqual(len(j["days"]), 7)
        self.assertEqual(j["subagents"][0]["id"], "a1")
        for k in ("files_read", "assistant_lines", "responses", "zero_usage_skipped", "requests_no_usage",
                  "unpriced_models", "prices_date", "prices_age_days", "not_logged"):
            self.assertIn(k, j["quality"])

    def test_empty_config_dir_message(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(empty))
        for root in (empty, os.path.join(empty, "nope")):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root}):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = spend.main([])
            out = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("No Claude Code logs found", out)
            self.assertIn(os.path.join(root, "projects"), out)
            self.assertIn("claude.ai/code", out)
            self.assertIn("cloud", out)

    def test_stale_price_warning(self):
        old = dict(PRICES, date_read=(date.today() - timedelta(days=61)).isoformat())
        fresh = dict(PRICES, date_read=(date.today() - timedelta(days=60)).isoformat())
        self.assertIn("61 days", spend.stale_warning(old))
        self.assertIsNone(spend.stale_warning(fresh))
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1000)])
        _, s = run_summary(self.fx.root)
        self.assertIn("Warning", spend.render_html(s, False, spend.stale_warning(old)))

    def test_page_has_privacy_note_and_sections(self):
        self.fx.write("proj/s.jsonl", [user("secret prompt"), line("m1", "r1", inp=1000)])
        _, s = run_summary(self.fx.root)
        page = spend.render_html(s)
        for needle in ("Nothing here leaves this computer unless you publish it.", "Cost by type", "Tokens by type", "Cost by model",
                       "Biggest sessions", "Biggest subagents", "Cost by day", "The last 7 days", "Cost by hour",
                       "The math be mathing.", "This is an estimate at API prices", "secret prompt"):
            self.assertIn(needle, page)
        self.assertNotIn("Cost by project", page)
        self.assertEqual(page.count("serif"), page.count("sans-serif"))   # no serif font anywhere

    def test_artifact_page(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1_000_000)])
        art = os.path.join(self.fx.root, "artifact.html")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}), \
                mock.patch.object(spend, "artifact_path", return_value=art):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = spend.main(["--artifact"])
        self.assertEqual(code, 0)
        self.assertIn(art, buf.getvalue())
        page = Path(art).read_text(encoding="utf-8")
        self.assertIn("<title>Claude Code Spend</title>", page)
        self.assertIn(":root{", page)
        self.assertIn("--green:#267e5c", page)
        self.assertIn("color-scheme:light", page)
        self.assertNotIn("prefers-color-scheme", page)
        self.assertNotIn('data-theme', page)
        self.assertIn("font:16px/1.55 Arial,sans-serif", page)
        self.assertIn("body{margin:0;background:var(--white)", page)
        self.assertNotIn("http-equiv", page)
        self.assertEqual(page.count("<script"), 1)                  # the inlined navigation script
        self.assertNotIn("<script src", page)

    # ---- TLDR rules

    def tl(self):
        _, s = run_summary(self.fx.root)
        return s, s["tldr"]

    def test_tldr_empty_state(self):
        self.fx.write("proj/s.jsonl", [user("tiny"), line("m1", "r1", inp=1000)])
        s, t = self.tl()
        self.assertTrue(t["empty"])
        self.assertEqual(t["recommendations"], [])
        self.assertIn("Not enough spend for a TLDR", spend.render_html(s))
        self.assertIn("Too little spend", spend.render_text(s))

    def test_tldr_findings_and_meaning(self):
        self.fx.write("proj/s.jsonl", [user("big"), line("m1", "r1", cr=100_000_000, inp=5_000_000)])
        s, t = self.tl()
        self.assertFalse(t["empty"])
        self.assertTrue(2 <= len(t["findings"]) <= 3)
        self.assertEqual(t["findings"][0], "50% of today's cost was Claude re-reading the chat.")
        self.assertEqual(t["findings"][1], "Sonnet 5.5 was 100% of the cost.")
        self.assertEqual(t["findings"][2], "Your biggest session cost $20.00, 100% of the day.")
        self.assertIn("re-reading", t["meaning"])

    def test_tldr_rule_a_reread(self):
        self.fx.write("proj/s.jsonl", [user("long"), line("m1", "r1", cr=100_000_000, inp=5_000_000)])
        _, t = self.tl()
        r = t["recommendations"][0]
        self.assertIn("new session", r["action"])
        self.assertIn("Your longest session re-reads", r["action"])            # singular reads naturally
        self.assertAlmostEqual(r["saving_usd"], 5.00, places=6)     # half of $10 of cache reads
        self.assertIn("$10.00 x 0.50", r["basis"])

    def test_tldr_rule_a_needs_large_share(self):
        self.fx.write("proj/s.jsonl", [user("x"), line("m1", "r1", cr=1_000_000, inp=5_000_000)])
        _, t = self.tl()
        self.assertFalse(any("new session" in r["action"] for r in t["recommendations"]))

    def test_tldr_rule_b_opus_subagents(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", inp=1_000_000)])
        self.fx.write("proj/s/subagents/agent-1.jsonl", [line("m2", "r2", model="claude-opus-5-5", inp=1_000_000)])
        self.fx.meta("proj/s/subagents/agent-1.meta.json", description="BIG JOB", finished=True)
        _, t = self.tl()
        r = next(r for r in t["recommendations"] if "Sonnet" in r["action"])
        self.assertAlmostEqual(r["saving_usd"], 2.00, places=6)     # $4.00 on Opus, $2.00 at Sonnet prices
        self.assertIn("BIG JOB ($4.00)", r["action"])
        self.assertIn("1 helper ran on Opus", r["action"])
        self.assertIn("$4.00 - $2.00 = $2.00", r["basis"])

    def test_tldr_rule_c_one_hour_writes(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", w1=1_000_000)])
        _, t = self.tl()
        r = next(r for r in t["recommendations"] if "5 minutes" in r["action"])
        self.assertAlmostEqual(r["saving_usd"], 1.50, places=6)     # $4.00 at the 1h rate, $2.50 at 5m
        self.assertIn("$4.00 - $2.50 = $1.50", r["basis"])

    def test_tldr_rule_d_top_subagents(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", inp=50_000)])
        for i in (1, 2, 3):
            self.fx.write("proj/s/subagents/agent-{}.jsonl".format(i), [line("a{}".format(i), "q{}".format(i), inp=1_000_000)])
            self.fx.meta("proj/s/subagents/agent-{}.meta.json".format(i), description="AGENT {}".format(i), finished=True)
        _, t = self.tl()
        r = next(r for r in t["recommendations"] if "Check if these helper agents" in r["action"])
        self.assertEqual(r["saving_usd"], 0.0)
        self.assertIn("AGENT 1", r["action"])

    def test_tldr_rule_e_background_calls(self):
        self.fx.write("proj/closed.jsonl", [line("m1", "r1", inp=1_000_000),
                                            cost_state(3.00, {"claude-sonnet-5-5": 3.00})])
        _, t = self.tl()
        r = next(r for r in t["recommendations"] if "background" in r["action"])
        self.assertEqual(r["saving_usd"], 0.0)
        self.assertIn("$1.00", r["basis"])

    def test_tldr_ranked_by_saving_and_capped_at_three(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", cr=100_000_000, inp=5_000_000, w1=1_000_000)])
        self.fx.write("proj/s/subagents/agent-1.jsonl", [line("m2", "r2", model="claude-opus-5-5", inp=1_000_000)])
        _, t = self.tl()
        rec = t["recommendations"]
        self.assertEqual(len(rec), 3)
        self.assertEqual([r["saving_usd"] for r in rec], sorted((r["saving_usd"] for r in rec), reverse=True))
        self.assertAlmostEqual(rec[0]["saving_usd"], 5.00, places=6)
        self.assertIn("about $", t["meaning"])

    def test_tldr_in_json_text_and_page(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", cr=100_000_000, inp=5_000_000)])
        s, t = self.tl()
        for k in ("findings", "meaning", "recommendations"):
            self.assertIn(k, t)
        for k in ("action", "saving_usd", "basis"):
            self.assertIn(k, t["recommendations"][0])
        self.assertEqual(json.loads(json.dumps(s))["tldr"], t)
        text = spend.render_text(s)
        self.assertLess(text.index("TLDR"), text.index("This is an estimate at API prices"))
        self.assertIn("Best way to spend less", text)                 # one tip: singular heading
        page = spend.render_html(s)
        self.assertIn("Best tip to reduce spend", page)
        self.assertIn('title="Re-reading in your longest session cost $10.00', page)
        self.assertLess(page.index("TLDR"), page.index("Cost by type"))

    def test_tldr_page_words_same_numbers(self):
        self.fx.write("proj/s.jsonl", [user("p"), line("m1", "r1", cr=200_000_000, inp=1_000_000, w1=1_000_000)])
        self.fx.write("proj/s/subagents/agent-1.jsonl", [line("m2", "r2", model="claude-opus-5-5", inp=1_000_000)])
        s, t = self.tl()
        pt = s[spend.PAGE_TLDR_KEY]
        self.assertEqual(t["findings"][0], "{:.0f}% of today's cost was Claude re-reading the chat.".format(
            s["token_types"][1]["percent"]))                          # the terminal keeps its words
        self.assertEqual([r["saving_usd"] for r in pt["recommendations"]], [r["saving_usd"] for r in t["recommendations"]])
        self.assertEqual(len(pt["findings"]), len(t["findings"]))
        self.assertIn("Claude re-reading the session", pt["findings"][0])
        self.assertIn("subagent", " ".join(pt["findings"]))
        self.assertIn("1 subagent ran on Opus", " ".join(r["action"] for r in pt["recommendations"]))
        page = spend.render_html(s)
        for f in pt["findings"]:
            self.assertIn(html.escape(f), page)
        for old in ("helper agent", "re-reading the chat", "memory", "Hidden"):
            self.assertNotIn(old, page)

    def test_plain_helpers(self):
        self.assertEqual(spend.plural(1, "session"), "1 session")
        self.assertEqual(spend.plural(2, "session"), "2 sessions")
        self.assertEqual(spend.plural(3, "search", "searches"), "3 searches")
        self.assertEqual(spend.model_name("claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(spend.model_name("claude-sonnet-5-5-20261101"), "Sonnet 5.5")
        self.assertEqual(spend.model_name("claude-3-5-sonnet-20241022"), "3.5 Sonnet")
        self.assertEqual(spend.time_range("2026-01-01T23:30:00", "2026-01-01T23:53:00"), "11:30-11:53 pm")
        self.assertEqual(spend.time_range("2026-01-01T11:50:00", "2026-01-01T12:10:00"), "11:50 am-12:10 pm")
        self.assertEqual(spend.day_name(date.today().isoformat()).split(",")[0], "Today")
        self.assertEqual(spend.change_text(-1.5, "yesterday"), "$1.50 less than yesterday")

    def test_html_out_and_no_browser(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1_000_000)])
        out = os.path.join(self.fx.root, "sub dir", "d.html")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}), \
                mock.patch.object(spend, "try_open", return_value=False):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = spend.main(["--html", "--out", out])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(out))
        self.assertIn(out, buf.getvalue())

    def test_bad_flags_exit_nonzero(self):
        for argv in (["--date", "nope"], ["--days", "0"], ["--bogus"]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                spend.main(argv)
            self.assertNotEqual(cm.exception.code, 0)

    # ---- 30-day average on the page

    def days_ago(self, n, h=12):
        return today_at(h) - timedelta(days=n)

    def write_days(self, offsets, name="s", old=False):
        """One session with a $2.00 response (1M sonnet input) on each day offset."""
        lines = [line("{}m{}".format(name, n), "{}r{}".format(name, n), ts=self.days_ago(n), inp=1_000_000)
                 for n in offsets]
        p = self.fx.write("proj/{}.jsonl".format(name), lines)
        if old:
            t = (datetime.now() - timedelta(days=min(offsets))).timestamp()
            os.utime(p, (t, t))
        return p

    def avg(self, ndays=7, history=31, now=None, day_=None):
        day_ = day_ or date.today()
        data = spend.scan(self.fx.root, day_ - timedelta(days=ndays - 1), day_, PRICES, history_days=history)
        return data, spend.summarize(data, day_, ndays, now=now)

    def hero_stats(self, s):
        hero = re.search(r"<header.*?</header>", spend.render_html(s), re.S).group(0)
        return [tuple(html.unescape(x) for x in t) for t in
                re.findall(r'<div class="stat" title="([^"]*)"><span>([^<]*)</span><b>([^<]*)</b>', hero)]

    def test_average_fewer_than_30_days_is_not_yet(self):
        self.write_days([0, 1, 12, 20])
        _, s = self.avg()
        a = s[spend.PAGE_AVG_KEY]
        self.assertIsNone(a["average"])
        self.assertIsNone(a["by_now"])
        self.assertEqual(a["first_day"], (date.today() - timedelta(days=20)).isoformat())
        first = date.today() - timedelta(days=20)
        tip = "Not enough days of logs yet: they go back to {} {}.".format(first.strftime("%b"), first.day)
        # the window starts 30 days back, so logs from 20 days back are 10 days short
        self.assertEqual(self.hero_stats(s), [(tip, "vs. 30-day average by now", "10 more days of data required"),
                                              (tip, "30-day daily average", "10 more days of data required")])

    def test_average_exactly_30_days_back_counts_gap_days_as_zero(self):
        self.write_days([0, 2, 30])                       # first day is the first day of the window
        _, s = self.avg()
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 4.00 / 30, places=6)
        stat = self.hero_stats(s)[1]
        self.assertEqual(stat[1:], ("30-day daily average", "$0.13"))
        self.assertEqual(stat[0], "Average cost per day over the 30 days before {}.".format(spend.plain_md(date.today().isoformat())))

    def test_window_excludes_the_shown_day(self):
        self.write_days([0, 30])                          # today's $2 is not in the average
        _, s = self.avg()
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 2.00 / 30, places=6)
        self.write_days([1], name="t")
        _, s = self.avg()
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 4.00 / 30, places=6)   # yesterday is in

    def test_average_one_day_short_says_1_day(self):
        self.write_days([0, 29])                          # window starts 30 days back
        _, s = self.avg()
        self.assertIsNone(s[spend.PAGE_AVG_KEY]["average"])
        self.assertEqual(s[spend.PAGE_AVG_KEY]["needed"], 1)
        for st in self.hero_stats(s):
            self.assertEqual(st[2], "1 more day of data required")

    def test_average_many_days_short_says_days(self):
        self.write_days([0, 1])
        _, s = self.avg()
        self.assertEqual(s[spend.PAGE_AVG_KEY]["needed"], 29)
        self.assertEqual([st[2] for st in self.hero_stats(s)], ["29 more days of data required"] * 2)

    def test_average_uses_only_the_30_days_before(self):
        self.write_days([0, 1, 45, 31])                   # the two old days are outside the window
        _, s = self.avg()
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 2.00 / 30, places=6)
        self.assertEqual(s[spend.PAGE_AVG_KEY]["first_day"], (date.today() - timedelta(days=45)).isoformat())

    def test_average_includes_background_calls(self):
        far = self.days_ago(30)
        self.fx.write("proj/c.jsonl", [line("m1", "r1", ts=far, inp=1_000_000),
                                       cost_state(5.00, {"claude-sonnet-5-5": 5.00})])
        self.write_days([0], name="o")
        _, s = self.avg()
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 5.00 / 30, places=6)

    def test_average_unaffected_by_7_day_window(self):
        self.write_days([0, 30])
        for nd in (1, 7, 14):
            _, s = self.avg(ndays=nd)
            self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["average"], 2.00 / 30, places=6)

    def test_average_with_no_usage(self):
        self.fx.write("proj/s.jsonl", [user("hi")])
        _, s = self.avg()
        self.assertIsNone(s[spend.PAGE_AVG_KEY]["average"])
        self.assertEqual(self.hero_stats(s)[1][2], "30 more days of data required")
        self.assertEqual(self.hero_stats(s)[1][0], "Not enough days of logs yet.")

    def by_now_fixture(self):
        """Window days 1..30 back; a response at 09:00 ($2), 14:00 ($4) and 14:01 ($8) on day 1 and
        on day 30 only a 09:00 one; today has $2 at 10:00 plus $2 of background calls."""
        lines = [line("a1", "ra1", ts=self.days_ago(1, 9), inp=1_000_000),
                 line("a2", "ra2", ts=self.days_ago(1, 14), inp=2_000_000),
                 line("a3", "ra3", ts=today_at(14, 1) - timedelta(days=1), inp=4_000_000),
                 line("a4", "ra4", ts=self.days_ago(30, 9), inp=1_000_000),
                 line("a5", "ra5", ts=today_at(10), inp=1_000_000),
                 cost_state(4.00, {"claude-sonnet-5-5": 4.00})]
        self.fx.write("proj/n.jsonl", lines)

    def test_by_now_average_cuts_each_day_at_the_same_time_of_day(self):
        self.by_now_fixture()
        _, s = self.avg(now=today_at(14, 0).timestamp())
        a = s[spend.PAGE_AVG_KEY]
        # day 1: 2 + 4 (14:00 counts, 14:01 does not); day 30: 2
        self.assertAlmostEqual(a["by_now"], (2.00 + 4.00 + 2.00) / 30, places=6)
        self.assertEqual(a["by_time"], "2:00 pm")
        _, s = self.avg(now=today_at(14, 1).timestamp())
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["by_now"], (2.00 + 4.00 + 8.00 + 2.00) / 30, places=6)
        _, s = self.avg(now=today_at(8, 59).timestamp())
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["by_now"], 0.0, places=6)

    def test_by_now_leaves_out_background_calls_and_full_average_keeps_them(self):
        self.fx.write("proj/n.jsonl", [line("a1", "ra1", ts=self.days_ago(1, 9), inp=1_000_000),
                                       cost_state(7.00, {"claude-sonnet-5-5": 7.00}),
                                       line("a2", "ra2", ts=self.days_ago(30, 9), inp=1_000_000),
                                       line("a5", "ra5", ts=today_at(10), inp=1_000_000)])
        _, s = self.avg(now=today_at(12).timestamp())
        a = s[spend.PAGE_AVG_KEY]
        self.assertAlmostEqual(a["by_now"], 4.00 / 30, places=6)        # logged only
        self.assertAlmostEqual(a["average"], (4.00 + 3.00) / 30, places=6)  # the tally is $7 against $4 logged: a $3 gap
        self.assertAlmostEqual(a["target_logged"], 2.00, places=6)

    def test_today_stat_wording_and_tooltip(self):
        self.by_now_fixture()
        _, s = self.avg(now=today_at(14, 45).timestamp())
        a = s[spend.PAGE_AVG_KEY]
        s1, s2 = self.hero_stats(s)
        self.assertEqual(s1[1], "vs. 30-day average by now")
        by = (2.00 + 4.00 + 8.00 + 2.00) / 30
        self.assertEqual(s1[2], "{} less".format(spend.money(by - 2.00)) if by > 2.00 else "{} more".format(spend.money(2.00 - by)))
        self.assertEqual(s1[0], "Today so far: $2.00. Average by 2:45 pm over the last 30 days: {}. Background calls are left out "
                                "of both, because they're only known when a session closes.".format(spend.money(by)))
        self.assertEqual(s2[1:], ("30-day daily average", spend.money(a["average"])))
        self.assertEqual(s2[0], "Average cost per day over the 30 days before {}.".format(
            spend.plain_md(date.today().isoformat())))

    def test_today_stat_more_and_same(self):
        self.write_days([30])
        self.fx.write("proj/t.jsonl", [line("t1", "rt1", ts=today_at(0, 0), inp=5_000_000)])
        _, s = self.avg(now=today_at(1).timestamp())
        self.assertEqual(self.hero_stats(s)[0][2], "$10.00 more")         # $10 so far against $0 by now

    def test_today_stat_same(self):
        self.write_days([30])
        self.fx.write("proj/t.jsonl", [line("t1", "rt1", ts=today_at(0, 0), inp=1_000_000)])
        self.fx.write("proj/y.jsonl", [line("y1", "ry1", ts=today_at(0, 0) - timedelta(days=1), inp=30_000_000)])
        _, s = self.avg(now=today_at(1).timestamp())
        self.assertAlmostEqual(s[spend.PAGE_AVG_KEY]["by_now"], 2.00, places=6)   # $60 over 30 days
        self.assertEqual(self.hero_stats(s)[0][2], "Same")

    def test_past_day_mode(self):
        self.fx.write("proj/p.jsonl", [line("p1", "rp1", ts=self.days_ago(2, 10), inp=3_000_000),
                                       cost_state(8.00, {"claude-sonnet-5-5": 8.00})])
        self.fx.write("proj/q.jsonl", [line("p2", "rp2", ts=self.days_ago(33, 10), inp=1_000_000),
                                       line("p3", "rp3", ts=self.days_ago(3, 10), inp=1_000_000)])
        day_ = date.today() - timedelta(days=2)
        _, s = self.avg(day_=day_)
        a = s[spend.PAGE_AVG_KEY]
        # window: the 30 days before day_ (days 3..32 back); day_ itself and later are not in it
        self.assertAlmostEqual(a["average"], 2.00 / 30, places=6)
        s1, s2 = self.hero_stats(s)
        self.assertEqual(s1[1], "vs. 30-day daily average")
        self.assertEqual(s1[2], "{} more".format(spend.money(s["total"] - a["average"])))
        self.assertAlmostEqual(s["total"], 8.00, places=6)                # background calls included in the day
        self.assertEqual(s1[0], "This day's total: $8.00. Average cost per day over the 30 days before {}: {}. "
                                "Background calls are included in both.".format(
                                    spend.plain_md(day_.isoformat()), spend.money(a["average"])))
        self.assertEqual(s2[1:], ("30-day daily average", spend.money(a["average"])))
        self.assertEqual(s2[0], "Average cost per day over the 30 days before {}.".format(spend.plain_md(day_.isoformat())))

    def test_history_read_matches_a_full_read(self):
        """Sessions only the extra days need are read cheaply; every daily total comes out the same."""
        self.write_days([0, 1], name="new")
        self.write_days([14, 20, 29], name="old", old=True)
        self.fx.write("proj/cl.jsonl", [line("c1", "q1", ts=self.days_ago(22), inp=1_000_000),
                                        cost_state(7.00, {"claude-sonnet-5-5": 7.00}),
                                        line("c2", "q2", ts=self.days_ago(21), inp=500_000)])
        t = (datetime.now() - timedelta(days=21)).timestamp()
        os.utime(Path(self.fx.root, "projects", "proj", "cl.jsonl"), (t, t))
        day_ = date.today()
        light = spend.scan(self.fx.root, day_ - timedelta(days=6), day_, PRICES, history_days=31)
        self.assertTrue(light["history"]["records"])          # the old sessions came through the cheap read
        full = spend.scan(self.fx.root, day_ - timedelta(days=30), day_, PRICES)
        for days in (30, 25, 8):
            a = spend.window_average(light, spend.tally_gaps(light), day_, days)
            b = spend.window_average(full, spend.tally_gaps(full), day_, days)
            self.assertEqual(a["first_day"], b["first_day"])
            self.assertAlmostEqual(a["average"] or 0.0, b["average"] or 0.0, places=9)
        a = spend.window_average(light, spend.tally_gaps(light), day_, 25)
        self.assertGreater(a["average"], 0)

    def test_history_read_counts_a_response_once(self):
        l = line("dup", "r", ts=self.days_ago(12), inp=1_000_000)
        self.fx.write("proj/a.jsonl", [line("m0", "r0", inp=1_000_000), l])
        p = self.fx.write("proj/b.jsonl", [l])
        t = (datetime.now() - timedelta(days=12)).timestamp()
        os.utime(p, (t, t))
        data, _ = self.avg()
        a = spend.window_average(data, spend.tally_gaps(data), date.today(), 12)
        self.assertAlmostEqual(a["average"], 2.00 / 12, places=6)

    def test_history_read_off_for_terminal_and_json(self):
        self.write_days([0, 3])
        self.write_days([15], name="old", old=True)
        for argv in ([], ["--json"]):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}), \
                    mock.patch.object(spend, "scan", wraps=spend.scan) as sc:
                with contextlib.redirect_stdout(io.StringIO()):
                    spend.main(argv)
            self.assertEqual(sc.call_args.kwargs.get("history_days", 0), 0)
        art = os.path.join(self.fx.root, "a.html")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}), \
                mock.patch.object(spend, "artifact_path", return_value=art), \
                mock.patch.object(spend, "scan", wraps=spend.scan) as sc:
            with contextlib.redirect_stdout(io.StringIO()):
                spend.main(["--artifact"])
        self.assertEqual(sc.call_args.kwargs["history_days"], 31)

    def test_terminal_keeps_its_daily_average(self):
        self.write_days([0, 3])
        _, s = self.avg()
        _, plain = run_summary(self.fx.root)
        self.assertEqual(spend.render_text(s), spend.render_text(plain))
        self.assertIn("Daily average (last 7 days) $0.57", spend.render_text(s))
        self.assertEqual(spend.render_text(s).count("30-day"), 0)

    def test_hero_stats_and_updated_in_header(self):
        self.write_days([0, 1])
        _, s = self.avg()
        page = spend.render_html(s)
        stats = self.hero_stats(s)
        self.assertEqual([x[1] for x in stats], ["vs. 30-day average by now", "30-day daily average"])
        self.assertNotIn("<span>Updated</span>", page)
        stamp = "Updated " + spend.when(s["generated"], "")
        self.assertIn('Claude Code Spend<span title="When this page was made.">{}</span><span>{}</span></a>'.format(stamp, spend.LOCAL_LINE), page)
        self.assertIn('<span class="mobile-updated" title="When this page was made.">{}</span>'.format(stamp), page)
        self.assertEqual(page.count(stamp), 2)                # sidebar and top bar, not the stats
        css = spend._asset("page.css")
        narrow = css[css.index("@media(max-width:900px)"):css.index("@media(max-width:620px)")]
        self.assertIn(".brand span{display:none}", narrow)           # the sidebar line hides here ...
        self.assertIn(".mobile-updated{display:block", narrow)       # ... and the top bar line shows
        self.assertRegex(css, r"\.mobile-updated\{display:block")
        self.assertIn("repeat(auto-fit,minmax(150px,1fr))", css)

    def test_json_leaves_out_the_page_average(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", inp=1000)])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.fx.root}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                spend.main(["--json"])
        self.assertNotIn(spend.PAGE_AVG_KEY, json.loads(buf.getvalue()))

    def test_bracket_in_folder_name(self):
        self.fx.write("my [proj]/s.jsonl", [line("m1", "r1", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)


EVIL_AGENT = 'EVIL "NAME" <img src=x onerror=alert(1)> [opus]'
EVIL_PROMPT = "<script>alert(1)</script> & more"
EVIL_PROJECT = "-home-<x>"
# Built from reversed pieces so this file does not contain the words it checks for.
FORBIDDEN = re.compile(r"\b" + "adwog"[::-1] + r"\b", re.I)
REPO = Path(__file__).resolve().parent.parent


class PageTests(unittest.TestCase):
    """The HTML page: one document, the shared page system, safe text, no stray totals."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.fx.write(EVIL_PROJECT + "/s1.jsonl", [
            user(EVIL_PROMPT, ts=today_at(9)),
            line("m1", "r1", model="claude-opus-5-5", ts=today_at(9, 5), inp=2000, out=30000, cr=9_000_000, w5=400_000, w1=200_000),
            line("m2", "r2", model="claude-future-9", ts=today_at(15), inp=1000, out=500),
            cost_state(9.0, {"claude-opus-5-5": 8.0, "claude-sonnet-5-5": 1.0})])
        self.fx.write(EVIL_PROJECT + "/s1/subagents/agent-a1.jsonl", [
            user(EVIL_PROMPT, ts=today_at(10)),
            line("a1", "q1", model="claude-opus-5-5", ts=today_at(10, 5), inp=10000, out=20000, cr=2_000_000)])
        self.fx.meta(EVIL_PROJECT + "/s1/subagents/agent-a1.meta.json", description=EVIL_AGENT, finished=True)
        self.fx.write("other/s2.jsonl", [user("second", ts=today_at(8)), line("n1", "nr1", ts=today_at(8, 5), inp=300_000)])
        _, self.s = run_summary(self.fx.root)
        self.page = spend.render_html(self.s)

    def markup(self):
        """The page without its inlined CSS and script, which hold strings that only look like markup."""
        return re.sub(r"<(style|script)>.*?</\1>", "", self.page, flags=re.S)

    def ids(self, page=None):
        return re.findall(r'\sid="([^"]*)"', page or self.page)

    def test_one_document(self):
        for page in (self.page, spend.render_html(self.s, True, "old prices")):
            self.assertEqual(len(re.findall(r"<!doctype", page, re.I)), 1)
            self.assertEqual(page.count("<html"), 1)
            self.assertEqual(page.count("</html>"), 1)
            self.assertEqual(page.count("<body"), 1)
            self.assertEqual(page.count("<head>"), 1)
            self.assertTrue(page.rstrip().endswith("</html>"))
            self.assertNotIn("<link", page)                          # CSS and script are inlined
            self.assertNotIn("<script src", page)
            self.assertEqual(page.count("<script"), 1)
            self.assertEqual(page.count("<style"), 1)

    def test_shared_files_are_inlined(self):
        self.assertIn(spend._asset("page.css"), self.page)
        self.assertIn(spend._asset("page.js"), self.page)
        self.assertNotIn("prefers-color-scheme", self.page)
        self.assertNotIn("data-theme", self.page)

    def test_no_forbidden_names_in_page_or_repo(self):
        self.assertIsNone(FORBIDDEN.search(self.page))
        files = [p for p in REPO.rglob("*") if p.is_file() and ".git" not in p.relative_to(REPO).parts
                 and "__pycache__" not in p.relative_to(REPO).parts]
        self.assertTrue(files)
        for p in files:
            self.assertIsNone(FORBIDDEN.search(p.name), p)
            self.assertIsNone(FORBIDDEN.search(p.read_text(encoding="utf-8", errors="replace")), p)

    def test_sidebar_brand(self):
        self.assertIn('<a class="brand" href="#overview">Claude Code Spend<span title="When this page was made.">Updated ', self.page)

    def test_no_overview_nav_and_icon_only_menu_button(self):
        nav = re.search(r'<nav class="journey-nav".*?</nav>', self.page, re.S).group(0)
        self.assertNotIn("Overview", nav)
        self.assertNotIn('href="#overview"', nav)
        self.assertNotIn('class="mobile-location"', self.page)
        self.assertIn('<button class="menu-toggle" type="button" aria-label="Open menu" aria-expanded="false" '
                      'aria-controls="journey-nav"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true">'
                      '<use href="#i-menu"/></svg></button>', self.page)
        text = re.sub(r"<(style|script)\b.*?</\1>", "", self.page, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        self.assertNotIn("Overview", text)
        self.assertNotIn("Sections", text)

    def test_footer_credit_and_no_sharing_note(self):
        self.assertIn('<footer class="page-footer"><span>Made with &#x2764;&#xFE0F; by '
                      '<a href="https://keigowda.com" target="_blank" rel="noopener">Kei</a></span>'
                      '<a href="#overview">Back to top</a></footer>', self.page)
        self.assertNotIn('class="privacy"', self.page)

    def test_journey_links_point_at_unique_sections(self):
        nav = re.search(r'<nav class="journey-nav".*?</nav>', self.page, re.S).group(0)
        targets = re.findall(r'<a class="journey-link" href="#([^"]+)"', nav)
        self.assertEqual(targets, [sid for sid, _, _ in spend.SECTIONS])
        self.assertEqual(len(set(targets)), len(targets))
        ids = self.ids()
        for t in targets:
            self.assertEqual(ids.count(t), 1, t)
            self.assertRegex(self.page, r'<(section|header)[^>]* id="{}"|<(section|header) id="{}"'.format(t, t))
        for ref in re.findall(r'href="#([^"]+)"', self.markup()):
            self.assertEqual(ids.count(ref), 1, ref)
        self.assertEqual(ids.count("main"), 1)
        self.assertIn('aria-controls="journey-nav"', self.page)
        self.assertEqual(ids.count("journey-nav"), 1)

    def test_sidebar_labels_match_section_headings(self):
        """Each sidebar label's words appear, in order, in the heading of the section it links to."""
        nav = re.search(r'<nav class="journey-nav".*?</nav>', self.page, re.S).group(0)
        links = re.findall(r'<a class="journey-link" href="#([^"]+)">.*?<span class="nav-label">([^<]+)</span>', nav, re.S)
        self.assertEqual(len(links), len(spend.SECTIONS))
        for sid, label in links:
            hero = re.search(r'<header[^>]* id="{}"[^>]* aria-label="([^"]+)"'.format(re.escape(sid)), self.page)
            if hero:   # the overview panel has no heading; its aria-label names it
                self.assertEqual(html.unescape(hero.group(1)), html.unescape(label))
                continue
            m = re.search(r'<(?:section|header)[^>]* id="{}"[^>]*>.*?<h([12])[^>]*>(.*?)</h\1>'.format(re.escape(sid)),
                          self.page, re.S)
            self.assertIsNotNone(m, sid)
            heading = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
            words = re.findall(r"[\w']+", html.unescape(label).lower())
            hwords = re.findall(r"[\w']+", heading.lower())
            pos = 0
            for wd in words:
                self.assertIn(wd, hwords[pos:], "{!r} not in heading {!r}".format(label, heading))
                pos = hwords.index(wd, pos) + 1

    def test_sections_present_even_when_empty(self):
        empty = Fixture()
        self.addCleanup(empty.close)
        empty.write("p/s.jsonl", [line("m1", "r1", inp=1000)])
        _, s = run_summary(empty.root)
        page = spend.render_html(s)
        for sid, _, _ in spend.SECTIONS:
            self.assertEqual(self.ids(page).count(sid), 1, sid)

    def test_all_ids_and_svg_ids_unique(self):
        ids = self.ids()
        self.assertEqual(len(ids), len(set(ids)))
        patterns = re.findall(r'<pattern id="([^"]+)"', self.page)
        self.assertEqual(len(patterns), 2)
        self.assertEqual(len(set(patterns)), 2)
        for ref in re.findall(r'url\(#([^)]+)\)', self.page):
            self.assertIn(ref, ids)
        for ref in re.findall(r'<use href="#([^"]+)"', self.markup()):
            self.assertIn(ref, ids)

    def test_hidden_calls_are_inside_totals_not_a_total_line(self):
        hero = re.search(r"<header.*?</header>", self.page, re.S).group(0)
        self.assertIn(spend.money(self.s["total"]), hero)
        tiles = re.search(r'<div class="tiles">.*?</div></div>', self.page, re.S).group(0)
        blocks = [hero, tiles] + [b for cls in ("stat", "tile", "hero-metric")
                                  for b in re.findall(r'<div class="{}".*?</div>'.format(cls), self.page, re.S)]
        for block in blocks:
            self.assertNotIn("idden", block)
            self.assertNotIn("ackground", block)
        self.assertNotIn("Logged total", self.page)
        self.assertNotIn("Total with", self.page)
        self.assertGreater(self.s["not_logged"], 0)
        hidden_row = next(t for t in self.s["token_types"] if t["key"] == "hidden")
        self.assertAlmostEqual(sum(t["cost"] for t in self.s["token_types"]), self.s["total"], places=6)
        self.assertAlmostEqual(hidden_row["cost"], self.s["not_logged"], places=6)

    def test_untrusted_text_is_escaped(self):
        for raw in ("<script>alert(1)</script>", "<img src=x", 'EVIL "NAME"', "-home-<x>", "onerror=alert(1)>"):
            self.assertNotIn(raw, self.page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; more", self.page)
        self.assertIn("EVIL &quot;NAME&quot; &lt;img src=x onerror=alert(1)&gt; [opus]", self.page)
        self.assertIn("-home-&lt;x&gt;", self.page)
        self.assertEqual(self.page.count("<script"), 1)              # only the inlined navigation script
        self.assertEqual(len(re.findall(r"<img", self.page)), 0)

    def test_no_update_or_refresh_button(self):
        self.assertEqual(self.page.count("<button"), 1)             # the Sections menu button
        self.assertNotRegex(self.page, r"(?i)<button[^>]*>[^<]*(update|refresh)")
        self.assertNotIn("http-equiv", self.page)                   # --watch alone adds a reload

    def test_watch_page_reloads_itself(self):
        self.assertIn('http-equiv="refresh"', spend.render_html(self.s, True))

    def test_tables_scroll_in_own_region(self):
        tables = self.page.count("<table")
        regions = re.findall(r'<div class="scroll table-scroll"[^>]* tabindex="0" role="region" aria-label="[^"]+"><table', self.page)
        self.assertEqual(tables, len(regions))
        self.assertEqual(tables, 5)
        self.assertEqual(self.page.count('scope="col"'), self.page.count("<th "))
        self.assertEqual(len(re.findall(r'<div class="scroll chart" tabindex="0" role="region"', self.page)), 2)

    def test_product_names_and_stripes(self):
        self.assertIn("Claude Code", self.page)
        self.assertNotIn("Claude Co-work", self.page)
        self.assertIn("gapsw", self.page)                           # striped swatch for hidden calls in the chart key
        self.assertIn('class="gapfill" fill="url(#hatch-week)"', self.page)


if __name__ == "__main__":
    unittest.main()
