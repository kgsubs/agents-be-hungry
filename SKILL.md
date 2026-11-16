---
name: claude-spend
description: Shows what Claude Code usage has cost today, including every subagent by name and every model, as an estimate at API list prices, with a dashboard page that can be published as a private artifact. Use when the user asks what they have spent, their cost today, their token usage, or for their spend dashboard.
---

# claude-spend

Reads the local Claude Code transcripts (read only, no network) and prices them from `prices.json`.

## Run it

From this skill's folder (normally `~/.claude/skills/claude-spend/`, on Windows `%USERPROFILE%\.claude\skills\claude-spend\`). Use `python3` (on Windows, `python` or `py`).

- `python3 spend.py` prints today's total, what the money paid for, every model, the biggest sessions and subagents, by project, by hour, the last 7 days and a data quality summary.
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
3. Then answer in short text, from the terminal output:
   - Today's total in dollars, and the change from the day before.
   - The biggest sessions (project, first prompt, main cost, subagent cost, total).
   - The biggest subagents by name, with the model each actually used and its cost.
   - Every model listed, with its cost; say which have $0 today.
   - If "Not in transcripts" shows an amount, give it as a separate figure: calls Claude Code made but did not write to its transcripts, from its own tally, known only once a session closes.
4. If any model is listed as unpriced, say so and give its tokens; its cost is not in the total.
5. End with this line: "Estimate at API list prices. On a Pro or Max plan this is what the same usage would cost on the API, not what you are billed."
6. If the script says no transcripts were found, relay that message as is: Claude Code on the web (claude.ai/code) runs in the cloud, so those sessions are not on this computer.
7. If the script prints a warning that prices.json is old, pass it on and offer to refresh `prices.json` from https://docs.claude.com/en/docs/about-claude/pricing.
8. The page contains the user's prompts and agent names; the artifact link is private to their Claude account. Tell them not to share the page as-is.
