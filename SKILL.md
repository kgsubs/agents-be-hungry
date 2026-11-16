---
name: claude-spend
description: Shows what Claude Code usage has cost today, including every subagent by name and every model, as an estimate at API list prices, with a dashboard page that can be published as a private artifact. Use when the user asks what they have spent, their cost today, their token usage, or for their spend dashboard.
---

# claude-spend

Reads the local Claude Code transcripts (read only, no network) and prices them from `prices.json`.

## Run it

From this skill's folder (normally `~/.claude/skills/claude-spend/`, on Windows `%USERPROFILE%\.claude\skills\claude-spend\`). Use `python3` (on Windows, `python` or `py`).

- `python3 spend.py` prints today's total, what the money paid for, every model, the biggest sessions and helper agents (subagents), by project, by hour, the last 7 days and how it was counted.
- `--artifact` also writes `artifact.html` in this folder, the page to publish as an artifact, and prints its path.
- `--html` writes `dashboard.html` next to `spend.py` (or to the path given with `--out`), opens it, and prints the path. `--no-open` skips the browser. `--watch` rewrites it every 60 seconds.
- `--json` prints everything as JSON.
- `--days N` changes the history length (default 7). `--date YYYY-MM-DD` shows a past day.

## How to answer

1. When the user asks for their spend or the dashboard, run `python3 spend.py --artifact`.
2. If an Artifact tool is available in this session, publish `artifact.html` as an artifact:
   - First time (no `.artifact-url` file in this folder): publish it with no url and icon "chart", then save the returned artifact URL, alone, to `.artifact-url` in this folder.
   - Every later time: read `.artifact-url` and republish `artifact.html` to that url, so the same link updates.
   - If the saved artifact no longer exists or the publish is refused, publish a new one and overwrite `.artifact-url` with the new URL.
   - Give the user the link first.
   If there is no Artifact tool, run `python3 spend.py --html` instead and give the path to `dashboard.html`.
3. Then answer in a few short, plain lines from the terminal output. Lead with the total. Use the page's everyday words ("helper agents" for subagents, "Claude re-reading the chat" for cache reads, "Opus 5.5" for claude-opus-5-5):
   - The full total in dollars first (it already includes the hidden background calls), and how much more or less than the day before.
   - The biggest sessions: project, first message, main chat cost, helper agent cost, total.
   - The biggest helper agents by name, with the model each actually used and its cost.
   - Every model, with its cost; say which cost $0.00 that day.
   - If "Hidden background calls" shows an amount, say the total includes it: calls Claude Code makes behind the scenes and doesn't save in its logs, from its own running total, known once a session closes. If sessions are still open, say plainly that their background calls get added when they close; never estimate them.
4. If a model is marked "no price", say so and give its tokens; its cost is not in the total.
5. End with this line: "This is an estimate at API prices. On a Pro or Max plan, you don't pay this; it's what the same work would cost on the API."
6. If the script says no Claude Code logs were found, pass that message on as is: Claude Code on the web (claude.ai/code) runs in the cloud, so those sessions aren't on this computer.
7. If the script warns that prices may have changed, pass it on and offer to update `prices.json` from https://docs.claude.com/en/docs/about-claude/pricing.
8. The page shows the user's messages and agent names. The artifact link is private to their Claude account. Tell them not to share the page as is.
