Build me a Claude Code skill called `claude-spend` that shows how much my Claude Code usage has cost today, including every subagent, as a simple local dashboard. It must work on macOS, Linux and Windows, and it must be safe to publish as open source: no personal paths, names or keys anywhere in it.

## What I want to see

1. Today's estimated spend in dollars, in my computer's local time zone, at the top in large type, with the time of the last update.
2. Today's spend split by model.
3. Today's spend split by session, one row per session: project folder, session start time, the first line of the session's first prompt (cut to 80 characters), the main session's own cost, the cost of its subagents, and the total. Most expensive first.
4. Today's spend by hour, as a simple bar chart.
5. The last 7 days, one bar per day.
6. Token totals for today: input, output, cache writes and cache reads.
7. Sessions with activity in the last 5 minutes marked "running", because subagents may still be working.
8. A line under the total that reads: "Estimate at API list prices. On a Pro or Max plan this is what the same usage would cost on the API, not what you are billed."
9. A visible note on the page that it contains my prompts and should not be shared as-is.

## Where the data is

1. Claude Code writes every session to `<config>/projects/<project-folder>/<session-id>.jsonl`. `<config>` is `$CLAUDE_CONFIG_DIR` when that variable is set, otherwise `.claude` in the home folder: `~/.claude` on macOS and Linux, `%USERPROFILE%\.claude` on Windows. Resolve the home folder with `os.path.expanduser("~")` and build every path with `pathlib` or `os.path`. Do not hard-code `/` or `\`, do not use `glob` on folder names (project folders can contain `[`, `]`, `*` or `?`), and do not use POSIX-only modules.
2. Subagent transcripts are in `<config>/projects/<project-folder>/<session-id>/subagents/agent-*.jsonl`. Count them under their parent session.
3. Each line is a JSON object. Lines with `"type": "assistant"` carry `message.model`, `message.id`, `message.usage`, `requestId` and `timestamp` (UTC, ISO 8601). Convert timestamps to the computer's local time zone before deciding which day or hour they belong to, so a day ends at local midnight.
4. `message.usage` has `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `cache_creation.ephemeral_5m_input_tokens` and `cache_creation.ephemeral_1h_input_tokens`. It may also have `server_tool_use.web_search_requests`, `server_tool_use.web_fetch_requests`, `speed` (for example `"fast"`) and `inference_geo` (for example `"us"`). When the 5-minute and 1-hour split is missing, price all cache writes as 5-minute writes. When the split adds up to less than `cache_creation_input_tokens`, price the rest as 5-minute writes.
5. Duplicates: Claude Code writes the same response on several lines with identical usage. Count each `message.id` + `requestId` pair once across all files, or the totals come out far too high. In one real session file, 1,060 of 1,687 keys appeared more than once, always with identical usage. A line with no `message.id` has nothing to deduplicate on and is counted once as it stands.
6. Lines with `"model": "<synthetic>"` carry zero usage. Ignore them: they are not a model, not a price and not an unpriced row.
7. Model ids can end in a date, such as `claude-sonnet-5-5-20261101`. Match the price on the id with a trailing `-YYYYMMDD` removed when the full id has no entry.
8. Before writing the parser, open two or three real files on this computer and confirm these field names. If a field is missing or named differently, adapt to what the files contain and say so in the README.
9. Read files as UTF-8 with `errors="replace"`, one line at a time, so large files never load whole into memory. Skip any line that fails to parse and keep going: the last line of a live file is often half written. Open files read only. Never write to, lock, rename or delete a transcript: sessions are still writing to them.
10. If no transcripts are found, print a plain message that says which folder was searched and that Claude Code on the web (claude.ai/code) runs in the cloud, so its sessions are not on this computer and cannot be counted. Exit with code 0. Do not show a traceback.

## Prices

1. Do not use prices from memory. Read Anthropic's official model pricing page in its docs (https://docs.claude.com/en/docs/about-claude/pricing) and write `prices.json` in the skill folder: per million tokens for each model, the input, output, 5-minute cache write, 1-hour cache write and cache read prices, plus the price per web search, with the page's address and the date it was read (`date_read`, `YYYY-MM-DD`). Record every modifier the page lists: a separate rate for prompts over a token threshold (long-context tier), the fast-mode multiplier, and the multiplier for US-only inference.
2. Match models by the `message.model` string. Any model with no price is shown in its own "unpriced" row with its tokens, in the terminal and on the page, and is never priced at zero without saying so. Its cost is left out of the total, and the page says so.
3. Cost of one response = input x input price + output x output price + 5-minute cache tokens x 5-minute write price + 1-hour cache tokens x 1-hour write price + cache read tokens x cache read price + web searches x search price. Then apply the modifiers the transcript says apply: use the long-context price set when the prompt (input plus cache reads plus cache writes) is over the model's threshold; multiply by the fast-mode multiplier when `usage.speed` is `"fast"` and the model has one; multiply by the US-only multiplier when `usage.inference_geo` is `"us"`.
4. Stale prices: when `date_read` is more than 60 days old, or missing, print a warning in the terminal and show the same warning on the page. The warning names the age and the pricing page address.

## How it is built

1. Skill folder: `~/.claude/skills/claude-spend/` (on Windows `%USERPROFILE%\.claude\skills\claude-spend\`) containing `SKILL.md`, `spend.py`, `prices.json`, `README.md`, `LICENSE` (MIT), `.gitignore` and a `tests` folder.
2. `spend.py` uses the Python 3 standard library only, no installs, and works on macOS, Linux and Windows. Console output must not crash on characters the Windows console cannot show: replace them.
3. `python3 spend.py` prints today's total, by model and the top 5 sessions to the terminal.
4. `python3 spend.py --html` writes one self-contained `dashboard.html` (inline CSS and SVG, no outside scripts or fonts, a clean sans-serif font) to the skill folder, or to the path given by `--out`, opens it with `webbrowser`, and prints the file path whether or not a browser opened. When no browser exists it does not fail.
5. `python3 spend.py --watch` rewrites the dashboard every 60 seconds, and the page reloads itself every 60 seconds.
6. `--days N` changes the history from 7 days, `--date YYYY-MM-DD` shows a past day, `--no-open` skips the browser. Bad flags print a clear message and exit with a non-zero code. Success exits with 0.
7. `SKILL.md` has a `name` and a `description` in its frontmatter. The description says to use the skill when I ask what I've spent, my cost today, or my token usage. The body says how to run the script and how to answer me: today's total first, then the biggest session, and the estimate line.
8. `README.md` covers what it does, install, the commands, how cost is worked out, the dedupe rule, how to update `prices.json`, privacy, and that Claude Code on the web is not covered.
9. A `.gitignore` containing `dashboard.html` and `__pycache__/`.
10. A `tests` folder with a standard-library `unittest` suite using small made-up transcripts (no real data) that covers: dedupe, subagent attribution to the parent session, an unknown model going to unpriced, `<synthetic>` lines ignored, the long-context tier, a half-written last line skipped, date filtering across local midnight, and the empty-config-folder message.

## Privacy

1. Nothing is sent anywhere. The script makes no network calls. Only building `prices.json` reads the pricing page.
2. `dashboard.html` holds my prompts and project folder names. It is in `.gitignore`, the page says not to share it as-is, and it is never committed or published.

## Check before you tell me it's done

1. Run `python3 spend.py` and show me its output.
2. Pick one session from today. Add up its deduplicated usage by hand in a separate one-off script, and show that it matches the dashboard's number for that session.
3. Show the count of assistant lines read today and the count after dedupe.
4. Run `--html`, open the page and confirm every section appears.
5. Search the skill folder for my username, my home path and anything that looks like a key, and show that nothing turns up.
6. Run the test suite and show that it passes.

Then tell me today's total, the model and the session that cost the most, and the path to the dashboard.
