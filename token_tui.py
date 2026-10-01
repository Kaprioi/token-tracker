#!/usr/bin/env python3
"""Token Tracker in the terminal — same data as the app, redrawn every second.

    python3 token_tui.py           # your real data
    python3 token_tui.py --demo    # synthetic data

Keys: 1-4 or ←/→ switch tool · q quits
"""
import argparse
import os
import select
import shutil
import sys
import termios
import time
import tty
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import token_meter as tm  # noqa: E402

ORDER = ["claude", "codex", "xai", "deepseek"]
TOOLS = {
    #            name           tab       glyph  accent (r,g,b)    secondary         dim
    "claude":   ("Claude Code", "Claude",   "✳", (217, 119, 87), (212, 162, 127), (115, 114, 108)),
    "codex":    ("Codex",       "Codex",    ">_", (236, 236, 236), (16, 163, 127), (120, 120, 120)),
    "xai":      ("Grok · API",  "Grok",     "/", (255, 255, 255), (163, 163, 163), (110, 110, 110)),
    "deepseek": ("DeepSeek · API", "DeepSeek", "≈", (77, 107, 254), (142, 160, 255), (107, 113, 137)),
}
ESC = "\x1b["
RESET, BOLD = ESC + "0m", ESC + "1m"


def fg(rgb):
    return f"{ESC}38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def bg(rgb):
    return f"{ESC}48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def fmt(n):
    if n >= 1e6:
        return f"{n / 1e6:.0f}M" if n >= 1e7 else f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k" if n >= 1e5 else f"{n / 1e3:.1f}k"
    return str(round(n))


def dur(seconds):
    m = max(0, round(seconds / 60))
    if m >= 1440:
        return f"{m // 1440}d {m % 1440 // 60}h"
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def visible_len(s):
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\x1b":
            i = s.index("m", i) + 1
            continue
        out += 1
        i += 1
    return out


def pad(s, width):
    return s + " " * max(0, width - visible_len(s))


def meter(frac, width, color, empty=(60, 60, 60)):
    """Smooth horizontal bar using eighth-blocks."""
    cells = min(max(frac, 0), 1) * width
    full = int(cells)
    part = " ▏▎▍▌▋▊▉"[int((cells - full) * 8)].strip() if full < width else ""
    return fg(color) + "█" * full + part + fg(empty) + "░" * (width - full - len(part)) + RESET


def window_left(s):
    w = s["window"]
    if not w["active"]:
        return 100.0
    if w.get("exact"):
        return max(0.0, 100 - w["used_percent"])
    limit = max(w["past_max"], 1_000_000)
    return max(0.0, 100 - 100 * w["used"] / limit)


def tab_note(s):
    if not s["available"]:
        return "set up" if s["kind"] == "api" else "—"
    if s["kind"] == "api" and not s["window"]["active"]:
        return f"{fmt(s['today_tokens'])} today"
    return f"{round(window_left(s))}%"


def column_chart(values, height, color, dim):
    """Vertical bars, one column per value, eighth-block resolution."""
    top = max(values) or 1
    levels = " ▁▂▃▄▅▆▇█"
    rows = []
    for r in range(height - 1, -1, -1):
        line = ""
        for v in values:
            cells = v / top * height
            if cells >= r + 1:
                ch = "█"
            elif cells > r:
                ch = levels[max(1, int((cells - r) * 8))]
            else:
                ch = " " if r else "▁"
            line += (fg(color) if ch != "▁" or v else fg(dim)) + ch * 2
        rows.append(line + RESET)
    return rows


def render(data, current, width, demo):
    s = data[current]
    name, _, glyph, accent, second, dim = TOOLS[current]
    text, muted = (235, 235, 235), dim
    now = datetime.now(timezone.utc)
    api = s["kind"] == "api"
    W = width
    L = []

    # header
    live = fg(second if s["sessions"] and s["sessions"][0]["active"] else muted) + "●" + RESET
    left = f" {fg(accent)}{BOLD}{glyph}{RESET} {BOLD}Token Tracker{RESET} {fg(muted)}· {name}{RESET}"
    right = (f"{bg(accent)}{fg((0, 0, 0))} DEMO {RESET} " if demo else "") + \
        f"{live} {fg(muted)}live {datetime.now().strftime('%-I:%M:%S %p')}{RESET} "
    L.append(pad(left, W - visible_len(right)) + right)
    L.append("")

    # tabs
    tabs = " "
    for i, p in enumerate(ORDER):
        label = f" {i + 1} {TOOLS[p][1]} {tab_note(data[p])} "
        if p == current:
            tabs += bg(TOOLS[p][3]) + fg((0, 0, 0) if sum(TOOLS[p][3]) > 400 else (255, 255, 255)) + BOLD + label + RESET + " "
        else:
            tabs += fg(muted) + label + RESET + " "
    L.append(tabs)
    L.append("")

    half = (W - 4) // 2
    # hero row
    w = s["window"]
    if api and not w["active"]:
        h1_label, h1_big = "TOKENS TODAY", fmt(s["today_tokens"])
        h1_frac, h1_sub = None, f"{s['calls_today']} calls · pay per token"
    elif api:
        left_pct = window_left(s)
        h1_label, h1_big, h1_frac = "RATE LIMIT LEFT", f"{round(left_pct)}%", left_pct / 100
        h1_sub = f"{w.get('requests_left')}/{w.get('requests_limit')} requests left / min"
    elif w["active"]:
        left_pct = window_left(s)
        resets = dur((datetime.fromisoformat(w["reset"]) - now).total_seconds())
        h1_label = "5-HR LIMIT LEFT" if w.get("exact") else "5-HR WINDOW LEFT (est.)"
        h1_big, h1_frac = f"{round(left_pct)}%", left_pct / 100
        wk = w.get("weekly")
        h1_sub = f"resets in {resets}" + (f" · weekly {round(100 - wk['used_percent'])}%" if wk else "") \
            if w.get("exact") else f"{fmt(w['used'])} used · resets in {resets}"
    else:
        h1_label, h1_big, h1_frac = "5-HR WINDOW LEFT", "100%", 1.0
        h1_sub = "no active window" if s["available"] else f"{name} not found"

    cur = s["sessions"][0] if s["sessions"] else None
    if cur:
        ctx_left = max(0, 100 - 100 * cur["context"] / cur["window"])
        h2_big, h2_frac = f"{round(ctx_left)}%", ctx_left / 100
        h2_sub = f"{cur['project'][:18]} · {fmt(cur['context'])}/{fmt(cur['window'])}"
    else:
        h2_big, h2_frac, h2_sub = "–", None, "no calls yet" if api else "no sessions"
    h2_label = "LAST CALL CONTEXT" if api else "CONTEXT LEFT"

    L.append(" " + pad(fg(muted) + h1_label + RESET, half) + "  " + fg(muted) + h2_label + RESET)
    L.append(" " + pad(fg(accent) + BOLD + h1_big + RESET, half) + "  " + fg(second if current != "claude" else text) + BOLD + h2_big + RESET)
    bar_w = half - 2
    L.append(" " + pad(meter(h1_frac, bar_w, accent) if h1_frac is not None else "", half) + "  " +
             (meter(h2_frac, bar_w, second) if h2_frac is not None else ""))
    L.append(" " + pad(fg(muted) + h1_sub + RESET, half) + "  " + fg(muted) + h2_sub + RESET)
    L.append("")

    # burn + eta
    burn = f"{fmt(s['burn_per_min'])}/min" if s["burn_per_min"] > 0 else "idle"
    if api:
        eta = "per-minute limit" if w["active"] else "pay as you go"
    elif w["active"] and s["burn_per_min"] > 0:
        limit = w.get("limit") if w.get("exact") else max(w["past_max"], 1_000_000)
        if limit:
            secs = max(0, limit - w["used"]) / s["burn_per_min"] * 60
            out = datetime.now().timestamp() + secs
            reset = datetime.fromisoformat(w["reset"]).timestamp()
            eta = (fg((255, 110, 110)) + datetime.fromtimestamp(out).strftime("%-I:%M %p") + RESET) \
                if out < reset else "after reset"
        else:
            eta = "not soon"
    else:
        eta = "not soon"
    L.append(" " + pad(f"{fg(accent)}⚡{RESET} {fg(muted)}Burn rate{RESET}  {BOLD}{burn}{RESET}", half) +
             f"  {fg(accent)}◷{RESET} {fg(muted)}{'Limit' if api else 'Runs out'}{RESET}  {BOLD}{eta}{RESET}")
    L.append("")

    # 24h chart
    L.append(" " + pad(f"{BOLD}Tokens · last 24h{RESET}", W - 14) + fg(muted) + f"{fmt(s['today_tokens'])} today".rjust(12) + RESET)
    peak = max(s["hourly"])
    for i, row in enumerate(column_chart(s["hourly"], 5, accent, (55, 55, 55))):
        scale = fmt(peak) if i == 0 else ("0" if i == 4 else "")
        L.append(" " + fg(muted) + scale.rjust(6) + RESET + " " + row)
    L.append(" " + fg(muted) + " " * 7 + "-23h".ljust(12) + "-17h".ljust(12) + "-11h".ljust(12) + "-5h".ljust(10) + "now" + RESET)
    L.append("")

    # models + week side by side
    L.append(" " + pad(f"{BOLD}By model{RESET} {fg(muted)}7 days{RESET}", half) + f"  {BOLD}This week{RESET} {fg(muted)}{fmt(s['week_tokens'])}{RESET}")
    total = sum(v for _, v in s["models"]) or 1
    shades = [accent, second, dim, (90, 90, 90)]
    model_rows = []
    for i, (m, v) in enumerate(s["models"][:4]):
        model_rows.append(f"{fg(shades[i % 4])}■{RESET} {m[:14].ljust(14)} {fg(muted)}{round(100 * v / total):>3}%{RESET} " +
                          meter(v / total, max(4, half - 25), shades[i % 4], (45, 45, 45)))
    if not model_rows:
        model_rows.append(fg(muted) + "No usage yet" + RESET)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today = datetime.now().weekday()
    wmax = max(s["weekday"]) or 1
    week_rows = [(BOLD if i == today else fg(muted)) + d + RESET + " " +
                 meter(v / wmax, max(4, half - 12), accent if i == today else second, (45, 45, 45)) +
                 " " + fg(muted) + fmt(v).rjust(5) + RESET
                 for i, (d, v) in enumerate(zip(days, s["weekday"]))]
    for i in range(max(len(model_rows), len(week_rows))):
        a = model_rows[i] if i < len(model_rows) else ""
        b = week_rows[i] if i < len(week_rows) else ""
        L.append(" " + pad(a, half) + "  " + b)
    L.append("")

    # sessions / agents
    L.append(f" {BOLD}{'Agents' if api else 'Sessions'}{RESET} {fg(muted)}{'by app, per day' if api else 'context used'}{RESET}")
    if not s["sessions"]:
        if api:
            url = f"http://127.0.0.1:{tm.PROXY_PORT}/" + ("xai/v1" if current == "xai" else "deepseek")
            L.append(f" {fg(muted)}Point your agent's base URL at{RESET} {fg(accent)}{url}{RESET}")
        else:
            L.append(f" {fg(muted)}No sessions yet{RESET}")
    for sess in s["sessions"][:6]:
        frac = min(1, sess["context"] / sess["window"])
        dot = fg(second) + "●" + RESET if sess["active"] else " "
        label = f"{sess['project']} · {sess['model'].replace('claude-', '')}"[:26].ljust(26)
        ago = dur((now - datetime.fromisoformat(sess["last"])).total_seconds())
        info = f"{sess['turns']} calls" if api else f"{fmt(sess['context'])}/{fmt(sess['window'])}"
        L.append(f" {dot} {label} " + meter(frac, max(6, W - 52), (255, 110, 110) if frac > .8 else accent, (45, 45, 45)) +
                 f" {fg(muted)}{info.rjust(10)} {ago.rjust(7)} ago{RESET}")
    L.append("")
    L.append(f" {fg(muted)}1-4 / ←→ switch · q quit · reads usage metadata only{RESET}")
    return L


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use synthetic data")
    ap.add_argument("--tool", choices=ORDER, default="claude", help="tab to start on")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    args = ap.parse_args()

    sources = tm.make_sources(args.demo)
    current = args.tool

    def frame():
        cols = shutil.get_terminal_size((90, 40)).columns
        width = max(64, min(cols, 96))
        return render(tm.snapshot(sources), current, width, args.demo)

    if args.once or not sys.stdin.isatty():
        print("\n".join(frame()))
        return

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")        # alternate screen, hide cursor
    try:
        tty.setcbreak(fd)
        last_draw = 0.0
        while True:
            if time.monotonic() - last_draw >= 1:
                # overwrite in place (no full clear) so it doesn't flicker
                out.write("\x1b[H" + "\n".join(line + "\x1b[K" for line in frame()) + "\x1b[J")
                out.flush()
                last_draw = time.monotonic()
            r, _, _ = select.select([sys.stdin], [], [], 0.2)
            if not r:
                continue
            key = os.read(fd, 8).decode(errors="ignore")
            if key in ("q", "Q", "\x03"):
                break
            if key in ("1", "2", "3", "4"):
                current = ORDER[int(key) - 1]
            elif key in ("\x1b[C", "\t", "l"):
                current = ORDER[(ORDER.index(current) + 1) % 4]
            elif key in ("\x1b[D", "h"):
                current = ORDER[(ORDER.index(current) - 1) % 4]
            last_draw = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


if __name__ == "__main__":
    main()
