# claude-spend

See how much your Claude Code usage cost today, subagents included.

You get one number for today, plus a simple page that breaks it down by model, by chat session, by hour, and over the last 7 days.

## Install (about 2 minutes)

1. Open the Claude desktop app.
2. Start a Claude Code session on any folder on your computer.
3. Copy this whole message and send it to Claude:

```
Install the claude-spend skill: clone https://github.com/kgsubs/claude-spend into ~/.claude/skills/claude-spend (on Windows: %USERPROFILE%\.claude\skills\claude-spend). If git isn't installed, download the repo as a zip and unzip it there instead. Then tell me what I've spent today.
```

4. If Claude asks for permission, say yes.

That's it. Claude installs it and tells you today's total.

## Use it

Ask Claude any of these:

- "What have I spent today?"
- "Show me my spend dashboard."
- "What did I spend yesterday?"

## Good to know

- **It's an estimate.** The dollars are what your usage would cost at Anthropic's API prices. On a Pro or Max plan you don't pay this amount; it's what the same work would cost on the API.
- **It only works for Claude Code on your own computer**, in the Claude desktop app or a terminal. It can't see the regular Claude chat, or Claude Code in the browser (claude.ai/code), because those run on Anthropic's computers.
- **It needs Python 3.** Most computers already have it. If Claude says it's missing, ask Claude to help you install it.
- **It's private.** Nothing leaves your computer. The dashboard page shows your own prompts, so don't share it as is.
- **New session?** If Claude doesn't seem to know the skill right after installing, start a new Claude Code session.

## Update or remove

- To update, ask Claude: "Update the claude-spend skill."
- To remove, ask Claude: "Remove the claude-spend skill."

## More

- Commands, the cost math and how prices are kept current: [DETAILS.md](DETAILS.md)
- Want Claude to build its own version from scratch instead? Use [PROMPT.md](PROMPT.md).

MIT license, see [LICENSE](LICENSE).
