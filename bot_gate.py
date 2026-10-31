"""bot-gate: keep your pages up for people while a scraper floods the site. One file, no dependencies, MIT licence.

    from bot_gate import BotGate, BotGateWSGI
    app = BotGate(app, allow_ips={"203.0.113.7"})               # ASGI: FastAPI, Starlette, Django ASGI
    app.wsgi_app = BotGateWSGI(app.wsgi_app, cookies=("session",))  # WSGI: Flask, Django, gunicorn

Part of Night Watch (https://lightsoutfactory.ai/kits/night-watch/), which adds an uptime watch, a Cloudflare
Free-plan shield it switches on by itself and a firewall-event summary.

What it does with each GET or HEAD request for a page:
- a user agent that claims Chrome 90 or newer but sends neither Sec-Fetch-* nor client-hint headers is not a
  browser: 403 before your app does any work. Robots that name themselves are not touched by this;
- a page for a stranger (none of your cookies, no Referer, not a known search engine or link preview) is served only
  while this worker has fewer than `inflight` requests in progress and within `rate` such pages per second (with a
  burst of `burst`). Over that, a tiny 503 hands out a cookie and reloads itself after 2 seconds: a person gets the
  page on the second try, a scraper that keeps no cookies never does;
- everything else passes: other methods, the paths in `skip_prefixes` (API, webhooks, static files, sitemaps,
  health checks), `allow_ips`, and - when `behind_cloudflare` is true - requests that did not come through
  Cloudflare (no CF-Connecting-IP header: your own health checks and internal calls).
Once a minute, if the gate turned anybody away, it logs one line to the "bot_gate" logger:
    bot gate 60s: fake-chrome 403=6712, strangers 503=6690, strangers served=540, peak in flight=12
The limits are per worker process: with 4 workers, rate=3 serves up to 12 stranger pages a second in all.
"""
import logging
import re
import threading
import time
from http.cookies import SimpleCookie

log = logging.getLogger("bot_gate")

KNOWN_BOT = re.compile(
    rb"google|bingbot|adidxbot|bingpreview|yandex|applebot|duckduck|baiduspider|petalbot|seznam|naver|qwant|"
    rb"facebookexternalhit|facebot|twitterbot|telegrambot|whatsapp|slackbot|discordbot|linkedinbot|pinterest|"
    rb"skypeuripreview|redditbot|embedly|oai-searchbot|chatgpt-user|perplexity|claude-user|claude-searchbot|lighthouse",
    re.I)
# any robot that names itself (Googlebot and bingbot also write "Chrome/" in their user agents)
ANY_BOT = re.compile(rb"bot|crawl|spider|slurp|google|bing|yandex|preview|lighthouse|headless", re.I)
CHROME = re.compile(rb"Chrome/(\d+)")
DEFAULT_SKIP = ("/api", "/webhook", "/static", "/media", "/assets", "/health", "/robots.txt", "/sitemap",
                "/favicon", "/apple-touch-icon", "/manifest", "/.well-known", "/feed", "/rss", "/llms.txt",
                "/ads.txt", "/app-ads.txt", "/sw.js")


class Gate:
    """The decision and the counters, shared by the ASGI and WSGI wrappers."""

    def __init__(self, inflight=12, rate=3.0, burst=10.0, allow_ips=(), skip_prefixes=DEFAULT_SKIP, cookies=(),
                 cookie_name="nw_hi", behind_cloudflare=True, title="One moment", secure_cookie=True,
                 report_every=60.0):
        self.max_inflight, self.rate, self.burst = int(inflight), float(rate), float(burst)
        self.allow = {ip.strip().encode() for ip in allow_ips if ip.strip()}
        self.skip = tuple(skip_prefixes)
        self.cookie_name = cookie_name
        # with cookie names given, only those count as "has been here before"; without, any cookie does
        names = b"|".join(re.escape(c.encode()) for c in (*cookies, cookie_name))
        self.cookies = re.compile(rb"(?:^|;)\s*(?:" + names + rb")=") if cookies else re.compile(rb"=")
        self.behind_cloudflare = behind_cloudflare
        self.report_every = report_every
        self.lock = threading.Lock()
        self.inflight = 0
        self.tokens = self.burst
        self.refilled = time.monotonic()
        self.reported = time.monotonic()
        self.stats = {"fake": 0, "shed": 0, "stranger": 0, "peak": 0}
        c = SimpleCookie()
        c[cookie_name] = "1"
        c[cookie_name]["path"] = "/"
        c[cookie_name]["max-age"] = 30 * 86400
        c[cookie_name]["samesite"] = "Lax"
        if secure_cookie:
            c[cookie_name]["secure"] = True
        self.set_cookie = c[cookie_name].OutputString().encode("latin-1")
        safe = re.sub(r"[<>&\"]", "", title)
        self.wait_page = (
            '<!doctype html><html><head><meta charset="utf-8"><meta name="robots" content="noindex">'
            '<meta http-equiv="refresh" content="2"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{safe}</title></head><body style="font:17px system-ui,sans-serif;text-align:center;margin:20vh 1em">'
            f"<p>{safe}&hellip;</p></body></html>").encode()

    def verdict(self, method, path, h, peer=None):
        """"pass", "stranger" (serve, counted), "fake" (403) or "shed" (503). h: {lower-case name bytes: value bytes}."""
        if method not in ("GET", "HEAD") or path.startswith(self.skip):
            return "pass"
        if self.behind_cloudflare:
            ip = h.get(b"cf-connecting-ip")
            if ip is None:
                return "pass"
        else:
            fwd = h.get(b"x-forwarded-for", b"")
            ip = fwd.split(b",")[0].strip() if fwd else (peer or b"")
        if ip in self.allow:
            return "pass"
        ua = h.get(b"user-agent", b"")
        if KNOWN_BOT.search(ua):
            return "pass"
        m = CHROME.search(ua)
        if (m and int(m.group(1)) >= 90 and b"sec-fetch-mode" not in h and b"sec-ch-ua" not in h
                and not ANY_BOT.search(ua)):
            return "fake"
        if b"referer" in h or self.cookies.search(h.get(b"cookie", b"")):
            return "pass"
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.refilled) * self.rate)
            self.refilled = now
            if self.inflight < self.max_inflight and self.tokens >= 1:
                self.tokens -= 1
                return "stranger"
        return "shed"

    def refuse(self, verdict):
        """(status, headers, body) for "fake" or "shed"."""
        with self.lock:
            self.stats[verdict] += 1
        self.report()
        if verdict == "fake":
            status, body, headers = 403, b"Forbidden", [(b"content-type", b"text/plain; charset=utf-8")]
        else:
            status, body = 503, self.wait_page
            headers = [(b"content-type", b"text/html; charset=utf-8"), (b"retry-after", b"10"),
                       (b"set-cookie", self.set_cookie)]
        headers += [(b"cache-control", b"no-store"), (b"content-length", str(len(body)).encode())]
        return status, headers, body

    def enter(self, verdict):
        with self.lock:
            if verdict == "stranger":
                self.stats["stranger"] += 1
            self.inflight += 1
            self.stats["peak"] = max(self.stats["peak"], self.inflight)

    def leave(self):
        with self.lock:
            self.inflight -= 1
        self.report()

    def report(self):
        now = time.monotonic()
        with self.lock:
            if now - self.reported < self.report_every:
                return
            s, self.stats = self.stats, {"fake": 0, "shed": 0, "stranger": 0, "peak": 0}
            took, self.reported = now - self.reported, now
        if s["fake"] or s["shed"]:
            log.warning("bot gate %.0fs: fake-chrome 403=%d, strangers 503=%d, strangers served=%d, peak in flight=%d",
                        took, s["fake"], s["shed"], s["stranger"], s["peak"])


class BotGate:
    """ASGI middleware: app = BotGate(app, **options) - the options are Gate's."""

    def __init__(self, app, **options):
        self.app = app
        self.gate = Gate(**options)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        h = {k.lower(): v for k, v in scope.get("headers") or []}
        peer = (scope.get("client") or ("",))[0].encode()
        verdict = self.gate.verdict(scope.get("method", "GET"), scope.get("path", "/"), h, peer)
        if verdict in ("fake", "shed"):
            status, headers, body = self.gate.refuse(verdict)
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": body if scope.get("method") != "HEAD" else b""})
            return
        self.gate.enter(verdict)
        try:
            await self.app(scope, receive, send)
        finally:
            self.gate.leave()


class BotGateWSGI:
    """WSGI middleware: app.wsgi_app = BotGateWSGI(app.wsgi_app, **options) - the options are Gate's."""

    def __init__(self, app, **options):
        self.app = app
        self.gate = Gate(**options)

    def __call__(self, environ, start_response):
        h = {k[5:].lower().replace("_", "-").encode(): v.encode("latin-1", "replace")
             for k, v in environ.items() if k.startswith("HTTP_") and isinstance(v, str)}
        verdict = self.gate.verdict(environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/"), h,
                                    environ.get("REMOTE_ADDR", "").encode())
        if verdict in ("fake", "shed"):
            status, headers, body = self.gate.refuse(verdict)
            reason = "Forbidden" if status == 403 else "Service Unavailable"
            start_response(f"{status} {reason}", [(k.decode(), v.decode("latin-1")) for k, v in headers])
            return [body if environ.get("REQUEST_METHOD") != "HEAD" else b""]
        self.gate.enter(verdict)
        try:
            result = self.app(environ, start_response)
        except BaseException:
            self.gate.leave()
            raise
        return _Counted(result, self.gate)


class _Counted:
    """The app's response, counted as in flight until its whole body has been sent or the server closes it, whichever
    comes first (streamed bodies included). Some callers, such as Flask's test client, read the body and never close."""

    def __init__(self, result, gate):
        self.result, self.gate, self.done = result, gate, False

    def __iter__(self):
        for chunk in self.result:         # not "yield from": that would close the app's iterable a second time
            yield chunk
        self._leave()

    def close(self):
        try:
            if hasattr(self.result, "close"):
                self.result.close()
        finally:
            self._leave()

    def _leave(self):
        if not self.done:
            self.done = True
            self.gate.leave()
