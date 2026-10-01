#!/usr/bin/env python3
"""Token Tracker — a small dashboard for Claude Code token usage.

Reads ~/.claude/projects/**/*.jsonl (usage fields only, never message text),
serves a local dashboard on 127.0.0.1 and opens it in a small app window.

    python3 token_meter.py            # real data
    python3 token_meter.py --demo     # synthetic data, to preview the UI
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
BLOCK = timedelta(hours=5)


# ---------------------------------------------------------------- collection

def parse_ts(ts):
    # Python 3.9's fromisoformat only accepts 3- or 6-digit fractions
    ts = ts.replace("Z", "+00:00")
    if "." in ts:
        head, rest = ts.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        ts = f"{head}.{(frac + '000000')[:6]}{tz}"
    return datetime.fromisoformat(ts)


class Collector:
    """Incrementally tails every transcript, keeping only usage metadata."""

    def __init__(self, root):
        self.root = root
        self.offsets = {}   # path -> bytes read
        self.events = {}    # dedupe key -> event
        self.lock = threading.Lock()

    def refresh(self):
        with self.lock:
            for path in self.root.glob("**/*.jsonl"):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                start = self.offsets.get(path, 0)
                if size < start:          # file rewritten
                    start = 0
                if size == start:
                    continue
                with open(path, "rb") as f:
                    f.seek(start)
                    data = f.read()
                # only consume complete lines
                end = data.rfind(b"\n") + 1
                for line in data[:end].splitlines():
                    self._ingest(line)
                self.offsets[path] = start + end
            return list(self.events.values())

    def _ingest(self, line):
        try:
            d = json.loads(line)
        except ValueError:
            return
        if d.get("type") != "assistant":
            return
        msg = d.get("message") or {}
        usage = msg.get("usage")
        ts = d.get("timestamp")
        if not usage or not ts or msg.get("model") == "<synthetic>":
            return
        try:
            ts = parse_ts(ts)
        except ValueError:
            return
        key = f"{msg.get('id')}:{d.get('requestId')}"
        self.events[key] = {
            "ts": ts,
            "session": d.get("sessionId", "?"),
            "project": Path(d.get("cwd") or "unknown").name,
            "model": msg.get("model", "unknown"),
            "input": usage.get("input_tokens", 0) or 0,
            "output": usage.get("output_tokens", 0) or 0,
            "cache_write": usage.get("cache_creation_input_tokens", 0) or 0,
            "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
        }


def demo_events():
    """Plausible fake history for previewing the dashboard."""
    rnd = random.Random(7)
    now = datetime.now(timezone.utc)
    projects = [("web-app", "claude-opus-5-5"), ("api-server", "claude-sonnet-5-5"),
                ("infra", "claude-sonnet-5-5"), ("scripts", "claude-haiku-4-5-20251001")]
    events = []
    for day in range(7, -1, -1):
        for _ in range(rnd.randint(2, 5)):
            proj, model = rnd.choice(projects)
            start = now - timedelta(days=day, hours=rnd.uniform(0, 14))
            if start > now:
                continue
            sid = f"demo-{day}-{rnd.randint(0, 99999)}"
            ctx = rnd.randint(8_000, 20_000)
            t = start
            for _ in range(rnd.randint(10, 60)):
                t += timedelta(seconds=rnd.randint(20, 240))
                if t > now:
                    break
                growth = rnd.randint(800, 9_000)
                ctx = min(ctx + growth, 190_000)
                events.append({"ts": t, "session": sid, "project": proj, "model": model,
                               "input": rnd.randint(1, 50), "output": rnd.randint(200, 6_000),
                               "cache_write": growth, "cache_read": ctx - growth})
    # one session that is active right now
    t, ctx = now - timedelta(minutes=95), 15_000
    while t < now:
        growth = rnd.randint(1_500, 6_000)
        ctx = min(ctx + growth, 165_000)
        events.append({"ts": t, "session": "demo-live", "project": "token-meter",
                       "model": "claude-opus-5-5", "input": 3, "output": rnd.randint(500, 5_000),
                       "cache_write": growth, "cache_read": ctx - growth})
        t += timedelta(seconds=rnd.randint(40, 150))
    return events


# ---------------------------------------------------------------- analysis

def billable(e):
    """Tokens that count toward the 5-hour window estimate.
    Cache reads are excluded: they are cheap and would swamp everything else."""
    return e["input"] + e["output"] + e["cache_write"]


def context_used(e):
    return e["input"] + e["cache_read"] + e["cache_write"]


def context_window(model, used):
    return 1_000_000 if used > 200_000 or "[1m]" in model else 200_000


def blocks(events):
    """Split usage into 5-hour blocks (start = first message, floored to the hour)."""
    out, cur = [], None
    for e in sorted(events, key=lambda e: e["ts"]):
        if cur is None or e["ts"] >= cur["end"]:
            start = e["ts"].replace(minute=0, second=0, microsecond=0)
            cur = {"start": start, "end": start + BLOCK, "tokens": 0, "last": e["ts"]}
            out.append(cur)
        cur["tokens"] += billable(e)
        cur["last"] = e["ts"]
    return out


def stats(events):
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat()

    # 5-hour window
    bl = blocks(events)
    active = bl[-1] if bl and bl[-1]["end"] > now else None
    past_max = max((b["tokens"] for b in bl if b is not active), default=0)
    recent = [e for e in events if now - e["ts"] <= timedelta(minutes=30)]
    burn = sum(billable(e) for e in recent) / 30.0  # tokens / minute

    # sessions
    by_session = {}
    for e in sorted(events, key=lambda e: e["ts"]):
        s = by_session.setdefault(e["session"], {"id": e["session"], "project": e["project"],
                                                 "model": e["model"], "tokens": 0, "turns": 0,
                                                 "start": e["ts"]})
        s["tokens"] += billable(e)
        s["turns"] += 1
        s["last"] = e["ts"]
        s["model"] = e["model"]
        s["context"] = context_used(e)
    sessions = sorted(by_session.values(), key=lambda s: s["last"], reverse=True)[:8]
    for s in sessions:
        s["window"] = context_window(s["model"], s["context"])
        s["active"] = now - s["last"] < timedelta(minutes=30)
        s["start"], s["last"] = iso(s["start"]), iso(s["last"])

    # hourly series, last 24h
    hours = [0] * 24
    for e in events:
        h = int((now - e["ts"]).total_seconds() // 3600)
        if 0 <= h < 24:
            hours[23 - h] += billable(e)

    # per weekday, last 7 days (local time)
    week = [0] * 7
    for e in events:
        if now - e["ts"] <= timedelta(days=7):
            week[e["ts"].astimezone().weekday()] += billable(e)

    # by model
    models = {}
    for e in events:
        if now - e["ts"] <= timedelta(days=7):
            m = e["model"].replace("claude-", "").split("-2025")[0]
            models[m] = models.get(m, 0) + billable(e)

    today = now.astimezone().date()
    return {
        "now": iso(now),
        "window": {
            "active": bool(active),
            "used": active["tokens"] if active else 0,
            "start": iso(active["start"]) if active else None,
            "reset": iso(active["end"]) if active else None,
            "past_max": past_max,
        },
        "burn_per_min": burn,
        "today_tokens": sum(billable(e) for e in events if e["ts"].astimezone().date() == today),
        "week_tokens": sum(week),
        "sessions": sessions,
        "hourly": hours,
        "weekday": week,
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
        "total_events": len(events),
    }


# ---------------------------------------------------------------- live updates

class Hub:
    """Watches the transcripts once a second and wakes every open dashboard
    the moment new usage is written. A heartbeat every 15s keeps time-based
    numbers (burn rate, countdowns) fresh even when nothing changes."""

    HEARTBEAT = 15

    def __init__(self, get_events, demo):
        self.get_events, self.demo = get_events, demo
        self.cond = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self._sig = None
        self._last_push = 0.0

    def _build(self):
        events = self.get_events()
        sig = (len(events), max((e["ts"] for e in events), default=None))
        return sig, json.dumps({**stats(events), "demo": self.demo}).encode()

    def run(self):
        while True:
            try:
                sig, payload = self._build()
            except Exception as exc:          # never let the watcher die
                print("refresh failed:", exc, file=sys.stderr)
                time.sleep(1)
                continue
            now = time.monotonic()
            if sig != self._sig or now - self._last_push >= self.HEARTBEAT:
                with self.cond:
                    self._sig, self.payload, self._last_push = sig, payload, now
                    self.version += 1
                    self.cond.notify_all()
            time.sleep(1)

    def wait(self, seen):
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout=self.HEARTBEAT * 2)
            return self.version, self.payload


# ---------------------------------------------------------------- server

def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/stream"):
                return self.stream()
            if self.path.startswith("/api/stats"):
                body, ctype = hub.payload, "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def stream(self):
            """Server-Sent Events: push stats whenever they change."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                while True:
                    seen, payload = hub.wait(seen)
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def log_message(self, *a):
            pass
    return Handler


def open_window(url, width=440, height=780):
    """Open a small chromeless window (Chrome/Edge/Brave app mode), else the default browser."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        shutil.which("google-chrome") or "", shutil.which("chromium") or "",
        shutil.which("msedge") or "",
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            profile = Path(tempfile.gettempdir()) / "token-meter-window"
            subprocess.Popen([exe, f"--app={url}", f"--window-size={width},{height}",
                              f"--user-data-dir={profile}", "--no-first-run",
                              "--no-default-browser-check"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


def exit_with_parent(pid):
    """When launched by the Mac app, quit if the app goes away."""
    while True:
        if os.getppid() != pid:
            os._exit(0)
        time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use synthetic data")
    ap.add_argument("--port", type=int, default=8737, help="0 = pick a free port")
    ap.add_argument("--no-window", action="store_true", help="don't open a window")
    ap.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.demo:
        fixed = demo_events()
        get_events = lambda: fixed
    else:
        get_events = Collector(PROJECTS_DIR).refresh

    hub = Hub(get_events, args.demo)
    threading.Thread(target=hub.run, daemon=True).start()
    if args.parent_pid:
        threading.Thread(target=exit_with_parent, args=(args.parent_pid,), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(hub))
    server.daemon_threads = True
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"READY {port}", flush=True)   # the Mac app waits for this line
    print(f"Token Tracker running at {url}  ({'demo data' if args.demo else PROJECTS_DIR})", flush=True)
    if not args.no_window:
        threading.Timer(0.4, open_window, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
