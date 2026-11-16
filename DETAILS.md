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

    python3 spend.py                      today's total, by model, top 5 sessions
    python3 spend.py --html               write dashboard.html and open it
    python3 spend.py --watch              rewrite the dashboard every 60 seconds
    python3 spend.py --days 14            change the history from 7 days
    python3 spend.py --date 2026-11-10    show a past day
    python3 spend.py --out PATH           write the dashboard to PATH
    python3 spend.py --no-open            do not open a browser

On Windows use `python` or `py` in place of `python3`. The file path of the dashboard is always printed; if no browser can be opened, the script carries on. Exit code is 0 on success and non-zero with a message on bad flags. If `CLAUDE_CONFIG_DIR` is set, transcripts are read from there instead of `~/.claude`.

## How cost is worked out

Claude Code writes each session to `~/.claude/projects/<project-folder>/<session-id>.jsonl`, and subagents to `.../<session-id>/subagents/agent-*.jsonl`, which are counted under their parent session. For each response:

    cost = input x input price
         + output x output price
         + 5-minute cache write tokens x 5-minute write price
         + 1-hour cache write tokens x 1-hour write price
         + cache read tokens x cache read price
         + web searches x search price

Prices are per million tokens and come from `prices.json`. Models are matched by name, ignoring an optional trailing `-YYYYMMDD` date. A model with no price gets its own "unpriced" row with its tokens and is not added to the total. Responses with no tokens (`<synthetic>` placeholders) are ignored. Three modifiers from the pricing page apply when the transcript says so: Claude Haiku 5.5 prompts over 100,000 tokens use a higher price set; fast mode uses its 2x multiplier on models that have one; US-only inference uses 1.1x.

The dedupe rule: Claude Code writes the same response on several lines with identical usage. Each `message.id` + `requestId` pair is counted once across all files, or totals come out far too high.

Transcripts are read one line at a time, read only. Lines that cannot be parsed, such as a half-written last line, are skipped.

## Updating prices

`prices.json` records the pricing page address and the date it was read. The script warns, in the terminal and on the page, when it is more than 60 days old. To update it, ask Claude to refresh `prices.json` from https://docs.claude.com/en/docs/about-claude/pricing, or edit the per-million-token values yourself, add new models by their model name, and change `date_read`.

## Privacy

Nothing leaves your computer. `spend.py` makes no network calls. `dashboard.html` contains your prompts and project folder names, so do not share it as-is. It is listed in `.gitignore`.

## Build your own instead

[PROMPT.md](PROMPT.md) is a prompt you can paste into Claude Code to have it build its own version of this skill from scratch.

## Update and uninstall

Update: run `git pull` inside the skill folder. Uninstall: delete the skill folder (`~/.claude/skills/claude-spend`, or `%USERPROFILE%\.claude\skills\claude-spend` on Windows).

