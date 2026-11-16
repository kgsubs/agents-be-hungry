"""Tests for spend.py. Fixtures are small made-up transcripts, no real data."""
import contextlib
import io
import json
import os
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
                         ["output", "cache_read", "cache_write_5m", "cache_write_1h", "input", "searches"])
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
        self.assertIn("claude-haiku-5-5", spend.render_text(s))
        self.assertIn("claude-haiku-5-5", spend.render_html(s))

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
        self.assertIn("unpriced", text)
        self.assertIn("unpriced", spend.render_html(s))

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
        self.assertIn("Requests with no usage logged", spend.render_html(s))

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
        self.assertAlmostEqual(s["total"], 4.00, places=6)                  # headline stays logged usage
        self.assertAlmostEqual(s["quality"]["not_logged"], 1.50, places=6)
        self.assertEqual(s["quality"]["open_sessions"], 1)
        self.assertAlmostEqual(model(s, "claude-haiku-5-5")["not_logged"], 0.50, places=6)
        rows = {r["session"]: r for r in s["sessions"]}
        self.assertAlmostEqual(rows["closed"]["not_logged"], 1.50, places=6)
        self.assertIsNone(rows["open"]["not_logged"])

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
                  "models", "sessions", "subagents", "projects", "hours", "days", "quality", "prices_warning"):
            self.assertIn(k, j)
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
            self.assertIn("No Claude Code transcripts found", out)
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
        for needle in ("Do not share it as-is", "Where the money went", "By model", "Biggest sessions",
                       "Biggest subagents", "By project", "By hour", "Last 7 days", "Data quality",
                       "Estimate at API list prices", "secret prompt"):
            self.assertIn(needle, page)
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
        self.assertIn(":root{--bg:", page)
        self.assertIn('@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){', page)
        self.assertIn(':root[data-theme="dark"]{', page)
        self.assertIn("body{margin:0;background:var(--bg)", page)
        self.assertNotIn("http-equiv", page)
        self.assertNotIn("<script", page)

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

    def test_bracket_in_folder_name(self):
        self.fx.write("my [proj]/s.jsonl", [line("m1", "r1", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)


if __name__ == "__main__":
    unittest.main()
