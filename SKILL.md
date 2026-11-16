---
name: claude-spend
description: Shows what Claude Code usage has cost today, including every subagent, as an estimate at API list prices, with a local dashboard. Use when the user asks what they have spent, their cost today, or their token usage.
---

# claude-spend

Reads the local Claude Code transcripts (read only, no network) and prices them from `prices.json`.

## Run it

From this skill's folder (normally `~/.claude/skills/claude-spend/`, on Windows `%USERPROFILE%\.claude\skills\claude-spend\`):

- Use `python3 spend.py` (on Windows, `python spend.py` or `py spend.py`). It prints today's total, spend by model and the top 5 sessions.
- `--html` writes `dashboard.html` next to `spend.py` (or to the path given with `--out`), opens it, and always prints the file path. `--no-open` skips the browser.
- `--watch` rewrites the dashboard every 60 seconds.
- `--days N` changes the history length (default 7). `--date YYYY-MM-DD` shows a past day.

## How to answer

1. Run `python3 spend.py` first.
2. Lead with today's total in dollars.
3. Then name the biggest session (project, first prompt, main cost, subagent cost, total) and the costliest model.
4. If any model is listed as unpriced, say so and give its tokens; its cost is not in the total.
5. End with this line: "Estimate at API list prices. On a Pro or Max plan this is what the same usage would cost on the API, not what you are billed."
6. If the script says no transcripts were found, relay that message as is: Claude Code on the web (claude.ai/code) runs in the cloud, so those sessions are not on this computer.
7. If the script prints a warning that prices.json is old, pass it on and offer to refresh `prices.json` from https://docs.claude.com/en/docs/about-claude/pricing.
8. The dashboard contains the user's prompts; tell them not to share it as-is.
9. If the user wants the dashboard, run `--html` and give the path to `dashboard.html`.
