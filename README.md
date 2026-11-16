# claude-spend

See what your Claude Code usage cost today, subagents included.

It reads the logs Claude Code already keeps on your computer and works out the cost at API prices.

## What you get

A page with the full picture for the day:

- Today's total, compared with the last 30 days
- TLDR: what stood out, what it means, and the top 3 tips to reduce spend
- Cost by type: what the money paid for, such as output, re-reading and caching
- Cost by model
- Biggest sessions
- Biggest subagents, by name
- Cost by day for the last 7 days
- Cost by hour for today
- The math be mathing: how it was counted

Claude also answers in a few lines in the chat: the total, the biggest sessions, the biggest subagents and every model used.

If your Claude can make artifacts, the page is saved to your Claude account as a private link. Each time you ask, the same link shows the latest numbers. If not, the page opens in your browser.

## Install

1. Open Claude Code on your computer, in the desktop app or a terminal, in any folder.
2. Send Claude this message:

```
Install the claude-spend skill: clone https://github.com/kgsubs/claude-spend into ~/.claude/skills/claude-spend (on Windows: %USERPROFILE%\.claude\skills\claude-spend). If git isn't installed, download https://github.com/kgsubs/claude-spend/archive/refs/heads/main.zip and put the files from its claude-spend-main folder there instead. Then tell me what I've spent today.
```

3. If Claude asks for permission, say yes.

Claude installs it and tells you today's total. If Claude doesn't seem to know the skill yet, start a new Claude Code session.

It needs Python 3. Most computers already have it. If Claude says it's missing, ask Claude to help you install it. Nothing else is needed.

## Use it

Ask Claude:

- "What have I spent today?"
- "Show me my spend dashboard."
- "What did I spend yesterday?"

To run it yourself, open a terminal in the skill folder and run:

```
python3 spend.py
```

On Windows, use `python spend.py`.

## Good to know

- It's an estimate at API prices. On a Pro or Max plan you don't pay this. It's what the same work would cost on the API. Prices come from Anthropic's pricing page.
- Background calls are included. Claude Code makes some calls on its own, like summarizing a long session, and doesn't save them in its logs. Their cost is added from Claude Code's own running total once a session closes. A session still open is added when it closes.
- It only sees Claude Code on your own computer. It can't see the regular Claude chat or Claude Code on the web (claude.ai/code), because those run on Anthropic's computers.

## Privacy

Everything stays on your computer. The script doesn't connect to the internet; it only reads Claude Code's logs. The one exception is the artifact link, which is private to your Claude account.

The page shows your messages, subagent names and project folders, so don't share it as is.

## Update or remove

- To update, ask Claude: "Update the claude-spend skill."
- To remove, ask Claude: "Remove the claude-spend skill."

## More

- All the commands, how the cost is worked out, keeping prices current and running it on a server: [DETAILS.md](DETAILS.md)
- Want Claude to build its own version from scratch instead? Use [PROMPT.md](PROMPT.md).

MIT license. See [LICENSE](LICENSE).
