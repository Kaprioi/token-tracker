#!/usr/bin/env python3
"""Token Tracker — a small dashboard for Claude Code and Codex token usage.

Reads ~/.claude/projects/**/*.jsonl and ~/.codex/sessions/**/*.jsonl
(usage fields only, never message text),
serves a local dashboard on 127.0.0.1 and opens it in a small app window.

    python3 token_meter.py            # real data
    python3 token_meter.py --demo     # synthetic data, to preview the UI
"""
import argparse
import http.client
import json
import os
import random
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
CODEX_DIR = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
DATA_DIR = Path.home() / ".token-tracker"
API_LOG = DATA_DIR / "api-usage.jsonl"
PROXY_PORT = 8788

# Agents call http://127.0.0.1:8788/<name>/... instead of the real API.
UPSTREAMS = {"xai": "https://api.x.ai", "deepseek": "https://api.deepseek.com"}
CONTEXT_WINDOWS = {"grok-4": 256_000, "grok-code": 256_000, "grok-3": 131_072,
                   "deepseek": 128_000}
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
        self.limits = None  # latest rate-limit snapshot, if the tool logs one
        self.lock = threading.Lock()

    def available(self):
        return self.root.is_dir()

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
                    self._ingest(line, path)
                self.offsets[path] = start + end
            return list(self.events.values()), self.limits


class ClaudeCollector(Collector):
    def _ingest(self, line, path):
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


class CodexCollector(Collector):
    """Codex logs cumulative token_count events plus its exact rate limits."""

    def __init__(self, root):
        super().__init__(root)
        self.meta = {}      # path -> session id, project, model, last total

    def _ingest(self, line, path):
        try:
            d = json.loads(line)
        except ValueError:
            return
        p = d.get("payload")
        if not isinstance(p, dict):
            return
        meta = self.meta.setdefault(path, {"session": path.stem, "project": "unknown",
                                           "model": "codex", "total": None})
        kind = d.get("type")
        if kind == "session_meta":
            meta["session"] = p.get("id") or meta["session"]
            meta["project"] = Path(p.get("cwd") or "unknown").name
        elif kind == "turn_context":
            meta["model"] = p.get("model") or meta["model"]
            if p.get("cwd"):
                meta["project"] = Path(p["cwd"]).name
        elif kind == "event_msg" and p.get("type") == "token_count":
            try:
                ts = parse_ts(d.get("timestamp", ""))
            except ValueError:
                return
            rl = p.get("rate_limits")
            if rl and rl.get("primary") and (self.limits is None or ts >= self.limits["ts"]):
                self.limits = {"ts": ts, "primary": rl.get("primary"),
                               "secondary": rl.get("secondary"), "plan": rl.get("plan_type")}
            info = p.get("info") or {}
            u = info.get("last_token_usage")
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            if not u or total == meta["total"]:   # Codex repeats unchanged counts
                return
            meta["total"] = total
            cached = u.get("cached_input_tokens", 0) or 0
            self.events[f"{meta['session']}:{total}"] = {
                "ts": ts,
                "session": meta["session"],
                "project": meta["project"],
                "model": meta["model"],
                "input": max(0, (u.get("input_tokens", 0) or 0) - cached),
                "output": u.get("output_tokens", 0) or 0,
                "cache_write": u.get("cache_write_input_tokens", 0) or 0,
                "cache_read": cached,
                "ctx_window": info.get("model_context_window"),
            }


def demo_codex():
    now = datetime.now(timezone.utc)
    events = demo_events(seed=11, projects=[("chess-game", "gpt-5.6-sol"), ("landing-page", "gpt-5.6-sol"),
                                            ("data-tools", "gpt-5.6-mini")], live="chess-game",
                         live_model="gpt-5.6-sol", window=258_400)
    limits = {"ts": now,
              "primary": {"used_percent": 41.0, "window_minutes": 300,
                          "resets_at": (now + timedelta(hours=2, minutes=50)).timestamp()},
              "secondary": {"used_percent": 23.0, "window_minutes": 10080,
                            "resets_at": (now + timedelta(days=4)).timestamp()},
              "plan": "plus"}
    return events, limits


def demo_events(seed=7, projects=None, live="token-meter", live_model="claude-opus-5-5", window=None):
    """Plausible fake history for previewing the dashboard."""
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc)
    cap = int((window or 200_000) * 0.95)
    projects = projects or [("web-app", "claude-opus-5-5"), ("api-server", "claude-sonnet-5-5"),
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
                ctx = min(ctx + growth, cap)
                events.append({"ts": t, "session": sid, "project": proj, "model": model,
                               "input": rnd.randint(1, 50), "output": rnd.randint(200, 6_000),
                               "cache_write": growth, "cache_read": ctx - growth})
    # one session that is active right now
    t, ctx = now - timedelta(minutes=95), 15_000
    while t < now:
        growth = rnd.randint(1_500, 6_000)
        ctx = min(ctx + growth, int(cap * .85))
        events.append({"ts": t, "session": "demo-live", "project": live,
                       "model": live_model, "input": 3, "output": rnd.randint(500, 5_000),
                       "cache_write": growth, "cache_read": ctx - growth})
        t += timedelta(seconds=rnd.randint(40, 150))
    for e in events:
        e["ctx_window"] = window
    return events


class ApiLog(Collector):
    """Usage recorded by the local API relay (one file, all providers)."""

    def __init__(self, path):
        super().__init__(path.parent)
        self.path = path
        self.limits = {}    # provider -> latest rate-limit snapshot

    def refresh(self):
        with self.lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                return
            start = self.offsets.get(self.path, 0)
            if size < start:
                start = 0
            if size > start:
                with open(self.path, "rb") as f:
                    f.seek(start)
                    data = f.read()
                end = data.rfind(b"\n") + 1
                for line in data[:end].splitlines():
                    self._ingest(line, self.path)
                self.offsets[self.path] = start + end

    def view(self, provider):
        def available():
            return any(e["provider"] == provider for e in self.events.values())

        def refresh():
            self.refresh()
            return ([e for e in self.events.values() if e["provider"] == provider],
                    self.limits.get(provider))
        return available, refresh

    def _ingest(self, line, path):
        try:
            d = json.loads(line)
            ts = parse_ts(d["ts"])
        except (ValueError, KeyError):
            return
        ua = d.get("agent") or "agent"
        e = {"ts": ts, "provider": d.get("provider"), "model": d.get("model") or "unknown",
             "session": f"{ua}:{ts.astimezone().date()}", "project": ua,
             "input": d.get("input", 0), "output": d.get("output", 0),
             "cache_read": d.get("cache_read", 0), "cache_write": d.get("cache_write", 0),
             "ctx_window": api_context_window(d.get("model") or "")}
        self.events[f"{len(self.events)}"] = e
        if d.get("ratelimit"):
            self.limits[e["provider"]] = {"ts": ts, **d["ratelimit"]}


def api_context_window(model):
    for prefix, n in CONTEXT_WINDOWS.items():
        if model.startswith(prefix):
            return n
    return 128_000


def demo_api(provider):
    if provider == "xai":
        events = demo_events(seed=23, projects=[("research-agent", "grok-4"), ("scraper", "grok-code-fast-1")],
                             live="research-agent", live_model="grok-4", window=256_000)
        now = datetime.now(timezone.utc)
        limits = {"ts": now, "requests_limit": 480, "requests_remaining": 391,
                  "tokens_limit": 2_000_000, "tokens_remaining": 1_310_000}
    else:
        events = demo_events(seed=5, projects=[("summarizer", "deepseek-chat"), ("planner", "deepseek-reasoner")],
                             live="planner", live_model="deepseek-reasoner", window=128_000)
        limits = None
    return events[::3], limits


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


def stats(events, limits=None, kind="local"):
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
        s["ctx_window"] = e.get("ctx_window")
    sessions = sorted(by_session.values(), key=lambda s: s["last"], reverse=True)[:8]
    for s in sessions:
        s["window"] = s.pop("ctx_window", None) or context_window(s["model"], s["context"])
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

    window = {
        "exact": False,
        "active": bool(active),
        "used": active["tokens"] if active else 0,
        "start": iso(active["start"]) if active else None,
        "reset": iso(active["end"]) if active else None,
        "past_max": past_max,
    }
    if limits and "primary" in limits:
        window = exact_window(events, limits, now)
    elif kind == "api":
        window = api_window(events, limits, now)

    today = now.astimezone().date()
    return {
        "now": iso(now),
        "kind": kind,
        "calls_today": sum(1 for e in events if e["ts"].astimezone().date() == today),
        "window": window,
        "burn_per_min": burn,
        "today_tokens": sum(billable(e) for e in events if e["ts"].astimezone().date() == today),
        "week_tokens": sum(week),
        "sessions": sessions,
        "hourly": hours,
        "weekday": week,
        "models": sorted(models.items(), key=lambda kv: -kv[1]),
        "total_events": len(events),
    }


def exact_window(events, limits, now):
    """Window numbers straight from the tool's own rate-limit report (Codex)."""
    def part(r):
        if not r or r.get("resets_at") is None:
            return None
        reset = datetime.fromtimestamp(r["resets_at"], timezone.utc)
        expired = reset <= now          # window rolled over since the last log line
        return {"used_percent": 0.0 if expired else float(r.get("used_percent") or 0),
                "reset": None if expired else reset.isoformat(),
                "minutes": r.get("window_minutes")}
    primary, weekly = part(limits.get("primary")), part(limits.get("secondary"))
    w = {"exact": True, "active": bool(primary and primary["reset"]), "plan": limits.get("plan"),
         "used_percent": primary["used_percent"] if primary else 0.0,
         "reset": primary["reset"] if primary else None, "weekly": weekly,
         "used": 0, "limit": None, "past_max": 0}
    if w["active"]:
        start = datetime.fromisoformat(w["reset"]) - timedelta(minutes=primary["minutes"] or 300)
        w["used"] = sum(billable(e) for e in events if e["ts"] >= start)
        if w["used_percent"] >= 1 and w["used"]:
            w["limit"] = w["used"] / (w["used_percent"] / 100)   # implied, for the ETA
    return w


def api_window(events, limits, now):
    """Pay-per-token APIs: no plan window, but xAI reports per-minute rate limits."""
    w = {"exact": True, "api": True, "active": False, "used": 0, "limit": None, "past_max": 0,
         "reset": None, "used_percent": 0.0}
    fresh = limits and now - limits["ts"] < timedelta(minutes=2)
    if fresh and limits.get("tokens_limit"):
        w.update(active=True, rate=True,
                 used_percent=100 - 100 * limits["tokens_remaining"] / limits["tokens_limit"],
                 requests_left=limits.get("requests_remaining"), requests_limit=limits.get("requests_limit"))
    return w


# ---------------------------------------------------------------- API relay

def tls_context():
    """python.org builds ship without CA certs; fall back to the Mac's system roots."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"]:
        return ctx
    pem = DATA_DIR / "system-roots.pem"
    if not pem.exists():
        DATA_DIR.mkdir(exist_ok=True)
        roots = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p",
                                "/System/Library/Keychains/SystemRootCertificates.keychain"],
                               capture_output=True, check=True).stdout
        pem.write_bytes(roots)
    ctx.load_verify_locations(str(pem))
    return ctx


def merge_usage(chunks):
    """Combine usage blocks from a response or stream (OpenAI or Anthropic style)."""
    u = {}
    for c in chunks:
        for k, v in c.items():
            if isinstance(v, (int, float)):
                u[k] = max(u.get(k, 0), v)
            elif isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, (int, float)):
                        key = f"{k}.{k2}"
                        u[key] = max(u.get(key, 0), v2)
    if not u:
        return None
    cached = u.get("prompt_cache_hit_tokens") or u.get("prompt_tokens_details.cached_tokens") \
        or u.get("cache_read_input_tokens", 0)
    if "prompt_tokens" in u:   # OpenAI-style: prompt includes cached tokens
        return {"input": max(0, u["prompt_tokens"] - cached), "cache_read": cached,
                "output": u.get("completion_tokens", 0), "cache_write": 0}
    return {"input": u.get("input_tokens", 0), "cache_read": cached,
            "output": u.get("output_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0)}


def find_usage(obj, out):
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            out.append(obj["usage"])
        for v in obj.values():
            if isinstance(v, dict):
                find_usage(v, out)


def rate_limits(headers):
    def num(name):
        try:
            return int(float(headers.getheader(name)))
        except (TypeError, ValueError):
            return None
    r = {"requests_limit": num("x-ratelimit-limit-requests"),
         "requests_remaining": num("x-ratelimit-remaining-requests"),
         "tokens_limit": num("x-ratelimit-limit-tokens"),
         "tokens_remaining": num("x-ratelimit-remaining-tokens")}
    return r if r["tokens_limit"] else None


class Relay(BaseHTTPRequestHandler):
    """Transparent pass-through to xAI / DeepSeek that records token counts only.
    Prompts, responses and API keys are forwarded untouched and never stored."""

    ctx = None
    log_lock = threading.Lock()
    HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding",
           "keep-alive", "proxy-connection", "te", "upgrade"}

    def do_GET(self): self.relay()
    def do_POST(self): self.relay()
    def do_PUT(self): self.relay()
    def do_DELETE(self): self.relay()
    def do_PATCH(self): self.relay()

    def relay(self):
        name, _, rest = self.path.lstrip("/").partition("/")
        if name not in UPSTREAMS:
            self.send_error(404, f"Use /{'/ or /'.join(UPSTREAMS)}/ as the base path")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        model = None
        try:
            req = json.loads(body) if body else None
            if isinstance(req, dict):
                model = req.get("model")
                # ask for usage in the final stream chunk (OpenAI-style APIs)
                if req.get("stream") and "stream_options" not in req and "messages" in req \
                        and not rest.startswith("anthropic"):
                    req["stream_options"] = {"include_usage": True}
                    body = json.dumps(req).encode()
        except ValueError:
            pass

        up = urlsplit(UPSTREAMS[name])
        conn = http.client.HTTPSConnection(up.hostname, timeout=600, context=self.ctx)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in self.HOP}
        headers["Accept-Encoding"] = "identity"
        try:
            conn.request(self.command, "/" + rest, body=body or None, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            self.send_error(502, f"Upstream unreachable: {exc}")
            return

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in self.HOP and k.lower() != "content-encoding":
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        usages, buf = [], b""
        stream = "text/event-stream" in (resp.getheader("Content-Type") or "")
        try:
            while True:
                chunk = resp.read1(65536) if stream else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if stream:
                    self.wfile.flush()
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        if line.startswith(b"data:") and b"usage" in line:
                            try:
                                find_usage(json.loads(line[5:]), usages)
                            except ValueError:
                                pass
                elif len(buf) < 50_000_000:
                    buf += chunk
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
        if not stream:
            try:
                find_usage(json.loads(buf), usages)
            except ValueError:
                pass

        usage = merge_usage(usages)
        if usage or rate_limits(resp):
            agent = (self.headers.get("X-Title") or self.headers.get("User-Agent") or "agent")
            self.record({"ts": datetime.now(timezone.utc).isoformat(), "provider": name,
                         "model": model, "agent": agent.split()[0].split("/")[0][:40],
                         **(usage or {}), "ratelimit": rate_limits(resp)})

    def record(self, entry):
        with self.log_lock:
            DATA_DIR.mkdir(exist_ok=True)
            with open(API_LOG, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def log_message(self, *a):
        pass


def start_relay():
    try:
        Relay.ctx = tls_context()
        relay = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Relay)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"API relay not started ({exc})", file=sys.stderr)
        return
    relay.daemon_threads = True
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    print(f"API relay on http://127.0.0.1:{PROXY_PORT}/  (" +
          ", ".join(f"/{k}/ -> {v}" for k, v in UPSTREAMS.items()) + ")", flush=True)


# ---------------------------------------------------------------- live updates

class Hub:
    """Watches the transcripts once a second and wakes every open dashboard
    the moment new usage is written. A heartbeat every 15s keeps time-based
    numbers (burn rate, countdowns) fresh even when nothing changes."""

    HEARTBEAT = 15

    def __init__(self, sources, demo):
        self.sources, self.demo = sources, demo   # name -> (available(), refresh())
        self.cond = threading.Condition()
        self.version = 0
        self.payload = b"{}"
        self._sig = None
        self._last_push = 0.0

    def _build(self):
        out = snapshot(self.sources)
        sig = tuple((p["total_events"], p["sessions"][0]["last"] if p["sessions"] else None,
                     p["window"].get("used_percent")) for p in out.values())
        return sig, json.dumps({"providers": out, "demo": self.demo}).encode()

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


def make_sources(demo=False):
    """name -> (available(), refresh() -> (events, limits)) for every tracked tool."""
    if demo:
        fixed = {"claude": (demo_events(), None), "codex": demo_codex(),
                 "xai": demo_api("xai"), "deepseek": demo_api("deepseek")}
        return {k: (lambda: True, (lambda v=v: v)) for k, v in fixed.items()}
    c, x, api = ClaudeCollector(PROJECTS_DIR), CodexCollector(CODEX_DIR), ApiLog(API_LOG)
    return {"claude": (c.available, c.refresh), "codex": (x.available, x.refresh),
            "xai": api.view("xai"), "deepseek": api.view("deepseek")}


def snapshot(sources):
    """Current stats for every tool, as the dashboard receives them."""
    out = {}
    for name, (available, refresh) in sources.items():
        events, limits = refresh()
        kind = "api" if name in ("xai", "deepseek") else "local"
        out[name] = {**stats(events, limits, kind), "available": available()}
    return out


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

    sources = make_sources(args.demo)
    if not args.demo:
        start_relay()

    hub = Hub(sources, args.demo)
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
