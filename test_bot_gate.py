"""bot-gate tests: ASGI and WSGI, no network.

    python3 -m unittest test_bot_gate
"""
import asyncio
import logging
import threading
import unittest

import bot_gate as gate


CHROME = b"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


def asgi_call(app, path="/page", method="GET", headers=(), client=("192.0.2.9", 1)):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path, "headers": list(headers), "client": client}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


async def hello(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"page"})


def cf(ip="198.51.100.20", **extra):
    h = [(b"cf-connecting-ip", ip.encode()), (b"user-agent", CHROME), (b"sec-fetch-mode", b"navigate")]
    return h + [(k.replace("_", "-").encode(), v) for k, v in extra.items()]


class TestGateASGI(unittest.TestCase):
    def test_fake_chrome_and_bots(self):
        app = gate.BotGate(hello)
        self.assertEqual(asgi_call(app, headers=[(b"cf-connecting-ip", b"1.2.3.4"), (b"user-agent", CHROME)])[0], 403)
        self.assertEqual(asgi_call(app, headers=cf())[0], 200)
        g = b"Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1) Chrome/141.0 Safari/537.36"
        self.assertEqual(asgi_call(app, headers=[(b"cf-connecting-ip", b"1.2.3.4"), (b"user-agent", g)])[0], 200)
        old = b"Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/80.0 Safari/537.36"
        self.assertEqual(asgi_call(app, headers=[(b"cf-connecting-ip", b"1.2.3.4"), (b"user-agent", old)])[0], 200)

    def test_strangers_over_the_rate_get_a_cookie(self):
        app = gate.BotGate(hello, rate=0.001, burst=2, secure_cookie=False)
        codes = [asgi_call(app, headers=cf())[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 503, 503])
        status, headers, body = asgi_call(app, headers=cf())
        self.assertIn(b"nw_hi=1", headers[b"set-cookie"])
        self.assertIn(b'http-equiv="refresh"', body)
        self.assertEqual(asgi_call(app, headers=cf(cookie=b"nw_hi=1"))[0], 200)
        self.assertEqual(asgi_call(app, headers=cf(referer=b"https://example.com/"))[0], 200)
        self.assertEqual(asgi_call(app, headers=cf(), method="POST")[0], 200)
        self.assertEqual(asgi_call(app, headers=cf(), path="/api/items")[0], 200)
        self.assertEqual(asgi_call(app, headers=cf(), method="HEAD")[2], b"")

    def test_named_cookies_only(self):
        app = gate.BotGate(hello, rate=0.001, burst=0, cookies=("sessionid",))
        self.assertEqual(asgi_call(app, headers=cf(cookie=b"_ga=1"))[0], 503)
        self.assertEqual(asgi_call(app, headers=cf(cookie=b"_ga=1; sessionid=abc"))[0], 200)
        self.assertEqual(asgi_call(app, headers=cf(cookie=b"xsessionid=abc"))[0], 503)

    def test_direct_and_allowed_requests_pass(self):
        app = gate.BotGate(hello, rate=0.001, burst=0, allow_ips={"198.51.100.20"})
        self.assertEqual(asgi_call(app, headers=[(b"user-agent", CHROME)])[0], 200, "not through Cloudflare")
        self.assertEqual(asgi_call(app, headers=cf())[0], 200, "allowed address")
        self.assertEqual(asgi_call(app, headers=cf(ip="198.51.100.21"))[0], 503)

    def test_not_behind_cloudflare(self):
        app = gate.BotGate(hello, rate=0.001, burst=0, behind_cloudflare=False, allow_ips={"203.0.113.5"})
        h = [(b"user-agent", CHROME), (b"sec-fetch-mode", b"navigate")]
        self.assertEqual(asgi_call(app, headers=h)[0], 503)
        self.assertEqual(asgi_call(app, headers=h + [(b"x-forwarded-for", b"203.0.113.5, 192.0.2.1")])[0], 200)

    def test_in_flight_limit_and_log_line(self):
        started = threading.Event()
        g = gate.BotGate(None, inflight=1, rate=1000, burst=1000, report_every=0)

        async def slow(scope, receive, send):
            started.set()
            await asyncio.sleep(0.3)
            await hello(scope, receive, send)

        g.app = slow
        results = []

        def first():
            results.append(asgi_call(g, headers=cf())[0])

        th = threading.Thread(target=first)
        th.start()
        started.wait(2)
        with self.assertLogs("bot_gate", logging.WARNING) as logs:
            results.append(asgi_call(g, headers=cf())[0])
        th.join()
        self.assertEqual(sorted(results), [200, 503])
        self.assertIn("strangers 503=1", logs.output[0])

    def test_lifespan_passes(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        asyncio.run(gate.BotGate(app)({"type": "lifespan"}, None, None))
        self.assertEqual(seen, ["lifespan"])


def wsgi_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"page"]


def wsgi_call(app, path="/page", method="GET", **headers):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "REMOTE_ADDR": "192.0.2.9"}
    environ.update({"HTTP_" + k.upper(): v for k, v in headers.items()})
    got = {}

    def start_response(status, hdrs):
        got["status"], got["headers"] = int(status.split()[0]), dict(hdrs)

    result = app(environ, start_response)
    body = b"".join(result)
    if hasattr(result, "close"):
        result.close()
    return got["status"], got["headers"], body


class TestGateWSGI(unittest.TestCase):
    def test_same_rules(self):
        app = gate.BotGateWSGI(wsgi_app, rate=0.001, burst=1, secure_cookie=False)
        ua = CHROME.decode()
        self.assertEqual(wsgi_call(app, CF_CONNECTING_IP="1.2.3.4", USER_AGENT=ua)[0], 403)
        self.assertEqual(wsgi_call(app, CF_CONNECTING_IP="1.2.3.4", USER_AGENT=ua, SEC_FETCH_MODE="navigate")[0], 200)
        status, headers, body = wsgi_call(app, CF_CONNECTING_IP="1.2.3.4", USER_AGENT=ua, SEC_FETCH_MODE="navigate")
        self.assertEqual(status, 503)
        self.assertIn("nw_hi=1", headers["set-cookie"])
        self.assertEqual(wsgi_call(app, CF_CONNECTING_IP="1.2.3.4", USER_AGENT=ua, SEC_FETCH_MODE="navigate",
                                   COOKIE="nw_hi=1")[0], 200)
        self.assertEqual(wsgi_call(app, USER_AGENT=ua)[0], 200, "not through Cloudflare")
        self.assertEqual(app.gate.inflight, 0, "every response closed is out of flight")

    def test_app_error_leaves_flight(self):
        def boom(environ, start_response):
            raise RuntimeError("app failed")

        app = gate.BotGateWSGI(boom)
        with self.assertRaises(RuntimeError):
            wsgi_call(app, CF_CONNECTING_IP="1.2.3.4", USER_AGENT=CHROME.decode(), SEC_FETCH_MODE="navigate")
        self.assertEqual(app.gate.inflight, 0)


if __name__ == "__main__":
    unittest.main()
