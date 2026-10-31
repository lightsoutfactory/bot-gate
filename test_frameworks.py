"""bot-gate inside real frameworks: Flask (WSGI), Starlette and FastAPI (ASGI). Each test is skipped when its
framework is not installed.

    pip install flask fastapi httpx
    python3 -m unittest test_frameworks
"""
import unittest

import bot_gate as gate

CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
FAKE = {"User-Agent": CHROME, "CF-Connecting-IP": "198.51.100.20"}
BROWSER = {**FAKE, "Sec-Fetch-Mode": "navigate"}

try:
    import flask
except ImportError:
    flask = None
try:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
except ImportError:  # Starlette, or httpx for its test client
    Starlette = None
try:
    import fastapi
except ImportError:
    fastapi = None


class Steps:
    """The same four requests in every framework. get(headers) returns (status, text, set-cookie header)."""

    def check(self, get):
        self.assertEqual(get(FAKE)[0], 403, "a Chrome user agent without browser headers")
        self.assertEqual(get(BROWSER)[:2], (200, "home page"), "a stranger within the rate")
        status, _, cookie = get(BROWSER)
        self.assertEqual(status, 503, "a stranger over the rate")
        self.assertIn("nw_hi=1", cookie)
        self.assertEqual(get(BROWSER)[:2], (200, "home page"), "the same visitor with the cookie the gate set")


@unittest.skipIf(flask is None, "Flask is not installed")
class TestFlask(Steps, unittest.TestCase):
    def test_flask(self):
        app = flask.Flask(__name__)
        app.add_url_rule("/", "home", lambda: "home page")
        app.wsgi_app = gate.BotGateWSGI(app.wsgi_app, rate=0.001, burst=1, secure_cookie=False)
        client = app.test_client()          # keeps cookies, as a browser does

        def get(headers):
            r = client.get("/", headers=headers)
            return r.status_code, r.get_data(as_text=True), r.headers.get("Set-Cookie", "")

        self.check(get)
        self.assertEqual(app.wsgi_app.gate.inflight, 0)


@unittest.skipIf(Starlette is None, "Starlette or httpx is not installed")
class TestStarlette(Steps, unittest.TestCase):
    def run_app(self, app):
        with TestClient(gate.BotGate(app, rate=0.001, burst=1, secure_cookie=False)) as client:
            def get(headers):
                r = client.get("/", headers=headers)
                return r.status_code, r.text, r.headers.get("set-cookie", "")

            self.check(get)

    def test_starlette(self):
        async def home(request):
            return PlainTextResponse("home page")

        self.run_app(Starlette(routes=[Route("/", home)]))

    @unittest.skipIf(fastapi is None, "FastAPI is not installed")
    def test_fastapi(self):
        app = fastapi.FastAPI()
        app.get("/", response_class=PlainTextResponse)(lambda: "home page")
        self.run_app(app)


if __name__ == "__main__":
    unittest.main()
