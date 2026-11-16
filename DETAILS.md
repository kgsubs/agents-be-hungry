# claude-spend: technical details

The simple guide is in [README.md](README.md). This page is for people who want the commands and the math.

## Install from a terminal

macOS and Linux:

```
git clone https://github.com/kgsubs/claude-spend ~/.claude/skills/claude-spend
```

Windows (Command Prompt):

```
git clone https://github.com/kgsubs/claude-spend %USERPROFILE%\.claude\skills\claude-spend
```

## Requirements

- Python 3. Check with `python3 --version` (on Windows, `python --version`). On macOS, running it may prompt you to install the developer tools; accept. On Windows, install Python from python.org or the Microsoft Store.
- Claude Code running on your own computer. Claude Code on the web (claude.ai/code) is not covered: those sessions run in the cloud, so their transcripts are not on this computer. If none are found, the script says where it looked.
- Nothing to install beyond Python: the script uses the standard library only.

## Commands

Run from the skill folder:

    python3 spend.py                      today's spend: totals, models, sessions, subagents
    python3 spend.py --html               write dashboard.html and open it
    python3 spend.py --artifact           write artifact.html, the page to publish as an artifact
    python3 spend.py --json               print everything as JSON for other tools
    python3 spend.py --watch              rewrite the dashboard every 60 seconds
    python3 spend.py --days 14            change the history from 7 days
    python3 spend.py --date 2026-11-10    show a past day
    python3 spend.py --out PATH           write the dashboard to PATH
    python3 spend.py --no-open            do not open a browser

`page.css` and `page.js` are the page's shared styles and navigation script; keep them next to `spend.py`, which writes them into each page. The terminal output and the page show the same sections: the header (total, change from the day before, the average over the history, last update), where the money went (output, cache reads, 5-minute and 1-hour cache writes, uncached input, web searches), every model seen in the history with this day's tokens by type, the 10 biggest sessions, the 15 biggest subagents, spend by project, by hour, the last N days, and a data quality panel.

On Windows use `python` or `py` in place of `python3`. The file path of the dashboard is always printed; if no browser can be opened, the script carries on. Exit code is 0 on success and non-zero with a message on bad flags. If `CLAUDE_CONFIG_DIR` is set, transcripts are read from there instead of `~/.claude`.

## The artifact link

When Claude can make artifacts (the Artifact tool is available in the session), the skill publishes the page to your Claude account as a private artifact and keeps one link that updates:

1. `python3 spend.py --artifact` writes `artifact.html` in the skill folder: one self-contained page (the styles in `page.css` and the small navigation script in `page.js` are written into it), no outside files or fonts, no auto-refresh, light mode only. It has a sidebar with a link to each section (a Sections menu on phones) and shows the same numbers as the terminal.
2. The first time, Claude publishes it as a new artifact and saves its URL to `.artifact-url` in the skill folder.
3. Each later time you ask, Claude rewrites `artifact.html` and republishes it to the saved URL, so the same link shows the latest numbers.
4. If that artifact was deleted or the publish is refused, Claude publishes a new one and saves the new URL.

The artifact is private to your Claude account until you share it. `artifact.html` and `.artifact-url` are in `.gitignore`. Without the Artifact tool, the skill falls back to `--html` and the file path.

## On a VPS or server

It runs the same on a Linux server where Claude Code runs: same install, same commands. `--html` writes the page and prints its path, but there is no browser on the server to open it. Two ways to view it:

1. Copy it to your computer, then open it:

```
scp you@your-server:~/.claude/skills/claude-spend/dashboard.html .
```

2. Or serve it over an SSH tunnel, so it keeps updating with `--watch`. On the server:

```
cd ~/.claude/skills/claude-spend && python3 spend.py --watch --no-open &
python3 -m http.server 8765 --bind 127.0.0.1
```

On your computer:

```
ssh -L 8765:127.0.0.1:8765 you@your-server
```

Then open http://127.0.0.1:8765/dashboard.html. Binding to 127.0.0.1 keeps the page off the public internet; it contains your prompts.

## How cost is worked out

Claude Code writes each session to `~/.claude/projects/<project-folder>/<session-id>.jsonl`, and subagents to `.../<session-id>/subagents/agent-*.jsonl` (also in folders below `subagents`, such as workflow runs), which are counted under their parent session. Each subagent has an `agent-<id>.meta.json` beside it; its `description` is the name shown on the page, and its `model` is the model that was asked for. Cost is always priced by the `message.model` that actually answered. A subagent with no meta file is named by the first line of its first prompt, or `agent-<id>`. Its status is finished (the meta file says so), running (written to in the last 5 minutes) or stopped. For each response:

    cost = input x input price
         + output x output price
         + 5-minute cache write tokens x 5-minute write price
         + 1-hour cache write tokens x 1-hour write price
         + cache read tokens x cache read price
         + web searches x search price

Prices are per million tokens and come from `prices.json`. Models are matched by name, ignoring an optional trailing `-YYYYMMDD` date. A model with no price is marked "no price" in its own row with its tokens and is not added to the total. Responses with no tokens (`<synthetic>` placeholders) are ignored. Three modifiers from the pricing page apply when the transcript says so: Claude Haiku 5.5 prompts over 100,000 tokens use a higher price set; fast mode uses its 2x multiplier on models that have one; US-only inference uses 1.1x.

`usage.iterations` lists every attempt the API made for a request. When it has one entry, it equals the top-level usage, so the top-level usage is priced. An `advisor_message` entry is an advisor consultation, billed at the advisor model's rates on top of the top-level usage, so it is priced as its own piece at that model. When there is more than one non-advisor entry (a fallback), each attempt is billed, so each is priced. `fallback_credit` was empty on every response checked, so it changes nothing here.

The dedupe rule: Claude Code writes the same response on several lines. Each `message.id` + `requestId` pair is counted once across all files, or totals come out far too high. Copies have always had identical usage; if a later copy ever has different usage, the last one written is kept and the data quality panel counts it.

Days and hours are in the computer's local time zone: a response at 04:58 UTC on the 11th counts on the 10th in New York (UTC-5).

## What the transcripts do not show

Claude Code also makes calls it never writes to the transcript: conversation compaction, session titles, background-agent progress summaries, tool-use summaries, safety and permission checks, memory extraction, web page summaries and similar. When a session closes, Claude Code writes its own running tally to the session file (a `cost-state` line with the dollar cost per model, priced by Claude Code itself). That tally includes those calls.

The difference is "Hidden background calls" in the terminal, "Background calls" on the page: for each pair of tally lines, the tally's increase minus the cost logged in the transcripts between them, per model, spread over the days in proportion to the logged cost. It is included in every total: the big number, the daily average, the change from the day before, each day's bar and label, each session's total, each model's cost and history cost, and the project totals. The cost breakdown ("Where the money went" in the terminal, "Cost by type" on the page) has a row for it so its rows add up to the total, and the background-call columns and the striped part of each bar show the breakdown. `--json` totals include it, and `logged_total`, `not_logged` and the per-row `not_logged` fields keep the parts. A session that is still open has no tally yet, so nothing is added for it and the page says so ("1 session is still open. Its background calls get added when it closes."); its hidden cost is never estimated. On one machine, over 80 closed sessions, Claude Code's tally was 31% above the logged total. The tally had more of every token type than the transcripts, uncached input most of all, plus Haiku calls that never appear in a transcript. Pricing the tally's own per-model token counts with `prices.json` gave Claude Code's dollar figure within 1% in 195 of 206 cases; the other 11 cannot be checked from totals alone, because price tiers apply per request.

The "How this was counted" panel ("How it's counted" on the page) also lists:

- requests written to the transcript (`api-request` lines) that never got a response line, with the reason: interrupted by you, cut off when the session closed, or no response written;
- compactions that day and how many tokens they summarized;
- lines with zero usage (`<synthetic>` placeholders), which are skipped;
- unpriced models, and the date and age of `prices.json`.

Transcripts are read one line at a time, read only. Lines that cannot be parsed, such as a half-written last line, are skipped.

## Updating prices

`prices.json` records the pricing page address and the date it was read. The script warns, in the terminal and on the page, when it is more than 60 days old. To update it, ask Claude to refresh `prices.json` from https://docs.claude.com/en/docs/about-claude/pricing, or edit the per-million-token values yourself, add new models by their model name, and change `date_read`.

## Privacy

`spend.py` makes no network calls and only reads the transcripts. Nothing leaves your computer unless Claude publishes the page as an artifact, which is private to your Claude account. `dashboard.html` and `artifact.html` contain your prompts, agent names and project folder names, so do not share them as-is. Both are listed in `.gitignore`, with `.artifact-url`.

## Build your own instead

[PROMPT.md](PROMPT.md) is a prompt you can paste into Claude Code to have it build its own version of this skill from scratch.

## Update and uninstall

Update: run `git pull` inside the skill folder. Uninstall: delete the skill folder (`~/.claude/skills/claude-spend`, or `%USERPROFILE%\.claude\skills\claude-spend` on Windows).

