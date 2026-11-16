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


def line(mid, rid, model="claude-sonnet-5-5", ts=None, inp=0, out=0, cr=0, w5=0, w1=0, **extra):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
             "cache_creation_input_tokens": w5 + w1,
             "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}
    usage.update(extra)
    return json.dumps({"type": "assistant", "timestamp": iso(ts or today_at(12)), "requestId": rid,
                       "message": {"id": mid, "model": model, "usage": usage}})


def user(text, ts=None):
    return json.dumps({"type": "user", "timestamp": iso(ts or today_at(11)),
                       "message": {"role": "user", "content": text}})


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


def run_summary(root, day=None, ndays=7):
    day = day or date.today()
    first = day - timedelta(days=ndays - 1)
    data = spend.scan(root, first, day, PRICES)
    return data, spend.summarize(data, day)


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_dedupe_counts_each_response_once(self):
        l = line("m1", "r1", inp=1_000_000, out=100_000)
        self.fx.write("proj/sess1.jsonl", [user("hello"), l, l, l, line("m2", "r2", inp=1_000_000)])
        data, s = run_summary(self.fx.root)
        self.assertEqual(s["lines_read"], 4)
        self.assertEqual(s["unique_today"], 2)
        self.assertAlmostEqual(s["total"], 3.00 + 2.00, places=6)

    def test_dedupe_across_files(self):
        l = line("m1", "r1", inp=1_000_000)
        self.fx.write("proj/sess1.jsonl", [l])
        self.fx.write("proj/sess1/subagents/agent-a.jsonl", [l])
        _, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 2.00, places=6)

    def test_subagent_attributed_to_parent_session(self):
        self.fx.write("proj/sessA.jsonl", [user("do the thing"), line("m1", "r1", inp=1_000_000)])
        self.fx.write("proj/sessA/subagents/agent-1.jsonl", [line("m2", "r2", inp=500_000)])
        self.fx.write("proj/sessA/subagents/agent-2.jsonl", [line("m3", "r3", inp=500_000)])
        _, s = run_summary(self.fx.root)
        self.assertEqual(len(s["rows"]), 1)
        row = s["rows"][0]
        self.assertEqual(row["session"], "sessA")
        self.assertAlmostEqual(row["main"], 2.00, places=6)
        self.assertAlmostEqual(row["sub"], 2.00, places=6)
        self.assertAlmostEqual(row["total"], 4.00, places=6)
        self.assertEqual(row["prompt"], "do the thing")

    def test_unknown_model_goes_to_unpriced(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="claude-future-9", inp=1000, out=500),
                                       line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertFalse(s["models"]["claude-future-9"]["priced"])
        self.assertEqual(s["models"]["claude-future-9"]["input"], 1000)
        self.assertAlmostEqual(s["total"], 2.00, places=6)
        text = spend.render_text(s, date.today())
        self.assertIn("claude-future-9", text)
        self.assertIn("unpriced", text)
        page = spend.render_html(s, date.today(), 7, False)
        self.assertIn("Unpriced", page)

    def test_synthetic_lines_ignored(self):
        self.fx.write("proj/s.jsonl", [line("m1", "r1", model="<synthetic>"),
                                       line("m2", "r2", inp=1_000_000)])
        _, s = run_summary(self.fx.root)
        self.assertNotIn("<synthetic>", s["models"])
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
        data, s = run_summary(self.fx.root)
        self.assertAlmostEqual(s["total"], 1.00, places=6)              # only after local midnight
        self.assertAlmostEqual(s["days"][date.today()], 1.00, places=6)
        self.assertAlmostEqual(s["days"][date.today() - timedelta(days=1)], 2.00, places=6)
        self.assertEqual(s["hours"][0], 1.00)
        _, y = run_summary(self.fx.root, day=date.today() - timedelta(days=1))
        self.assertAlmostEqual(y["total"], 2.00, places=6)

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
        self.assertIn("Warning", spend.render_html(s, date.today(), 7, False, spend.stale_warning(old)))

    def test_page_has_privacy_note_and_sections(self):
        self.fx.write("proj/s.jsonl", [user("secret prompt"), line("m1", "r1", inp=1000)])
        _, s = run_summary(self.fx.root)
        page = spend.render_html(s, date.today(), 7, False)
        for needle in ("Do not share it as-is", "By model", "By session", "By hour", "Last 7 days",
                       "Tokens", "Estimate at API list prices"):
            self.assertIn(needle, page)

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
