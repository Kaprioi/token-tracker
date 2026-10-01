# Token Tracker

A small macOS app that shows, in real time, how many tokens you have left in **Claude Code** and **Codex**, plus **Grok** and **DeepSeek** API usage from your agents. Use it as a Mac app or in the terminal.

![icon](packaging/icon_1024.png)

- **5-hour window left**: % remaining, tokens used and a countdown to the reset
- **Context left**: how full your current session's context window is
- **Burn rate and "Runs out"**: tokens per minute and when you'd hit the limit at that pace
- Tokens per hour (24h), by model, by weekday, and recent sessions
- **Claude Code / Codex / Grok / DeepSeek tabs**: each tab has its tool's own look (Claude's warm cream and coral, Codex's black and white) and follows light/dark mode
- **Menu bar** readout of both (`CC 81% · CX 55%`), which keeps tracking when the window is closed
- Keep on top (⌘T), Open at Login

Updates are pushed within about 1 second of either tool writing a message.

## Terminal version
```bash
python3 "/Applications/Token Tracker.app/Contents/Resources/token_tui.py"
```
Keys: **1–4** or **←/→** switch tools, **q** quits. Options: `--tool codex|claude|xai|deepseek`, `--demo`, `--once`.

Optional shortcut:
```bash
echo 'alias tt="python3 \"/Applications/Token Tracker.app/Contents/Resources/token_tui.py\""' >> ~/.zshrc
```

## Grok and DeepSeek (API calls from agents)
While the app is running, it relays API calls on `127.0.0.1:8788` and records token counts.
Change your agent's base URL and keep your API key the same:

| Tool | From | To |
|---|---|---|
| Grok | `https://api.x.ai/v1` | `http://127.0.0.1:8788/xai/v1` |
| DeepSeek | `https://api.deepseek.com` | `http://127.0.0.1:8788/deepseek` |

Only calls sent through the relay are counted, and the app must be running for them to work.
The Grok and DeepSeek websites and phone apps can't be tracked, because they don't expose usage.

## Privacy
Everything runs locally. It reads only the token counts in `~/.claude/projects/**/*.jsonl` and
`~/.codex/sessions/**/*.jsonl`, never prompts or responses. The API relay forwards calls
unchanged and saves only token counts to `~/.token-tracker/api-usage.jsonl`, never prompts,
replies or API keys.

## Install
Download `Token Tracker.dmg` from Releases, open it and drag the app into Applications.
It needs macOS 13+ and Python 3 (included with the Xcode Command Line Tools).
The app is not notarized, so macOS will block it the first time. Run this once after installing:
```bash
xattr -dr com.apple.quarantine "/Applications/Token Tracker.app"
```

## Run without building
```bash
python3 token_meter.py          # your real data, opens a small window
python3 token_meter.py --demo   # fake data, to preview the UI
```

## Build the app
```bash
packaging/build.sh              # creates dist/Token Tracker.app and dist/Token Tracker.dmg
```

## About the limits
**Codex** logs its exact 5-hour and weekly limits, so those numbers are exact.

**Claude Code**: Anthropic doesn't publish exact token caps for Pro or Max plans, so the % left is an
estimate until you set your own limit by clicking the purple card. The window counts
input, output and cache-write tokens; cache reads are excluded.
