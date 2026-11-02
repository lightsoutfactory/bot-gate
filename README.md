# bot-gate

Keep your pages up for people while a scraper floods your site. One Python file, no dependencies, for any ASGI app
(FastAPI, Starlette, Django under ASGI) or WSGI app (Flask, Django, anything under gunicorn).

```python
from bot_gate import BotGate, BotGateWSGI

app = BotGate(app, allow_ips={"203.0.113.7"})                    # ASGI
app.wsgi_app = BotGateWSGI(app.wsgi_app, cookies=("session",))   # WSGI (Flask)
```

## What it decides

For each GET or HEAD request for a page:

1. **Fake Chrome: 403.** The user agent claims Chrome 90 or newer, but there is no `Sec-Fetch-Mode` and no
   `Sec-CH-UA` header, and the user agent does not name itself a robot. Answered before your app does any work.
2. **A stranger over capacity: 503 with a cookie.** No cookie of yours, no Referer, not a search engine or a link
   preview. Strangers are served while the worker has fewer than `inflight` requests in progress and within `rate`
   pages a second; over that, a 400-byte page answers 503, sets a cookie and reloads after 2 seconds. A person gets the
   page on the second try. A scraper that keeps no cookies never does.
3. **Everything else passes:** other methods, `skip_prefixes` (API, webhooks, static files, sitemaps, health
   checks...), `allow_ips`, anyone with your cookies or a Referer, search engines, and, with `behind_cloudflare=True`
   (the default), every request that did not come through Cloudflare (your health checks and internal calls).

Once a minute, while it is turning anybody away, it logs one line to the `bot_gate` logger:
`bot gate 60s: fake-chrome 403=6712, strangers 503=6690, strangers served=540, peak in flight=12`

## Tested

20 seconds of synthetic flood (40 threads) on a page that takes 0.2 s to render, behind uvicorn:

| Requests | Scraper without browser headers | Headless browser, no cookies | Returning visitor | From a link | Googlebot |
|---|---|---|---|---|---|
| 35,268 (1,747/s) | 15,944 refused (403) | 10 served, 15,741 turned away (503) | 1,788 served | 1,087 served | 698 served |

Every returning visitor, every visitor from a link and every Googlebot request got its page in a median 0.20 s, the
page's own time. The same logic has run in production on one of our sites since a flood of about 225 requests a second
from residential proxies took it down ([the story](https://lightsoutfactory.ai/field-notes/scraper-flood-cloudflare-free-plan/)).

Python 3.9 or newer, no dependencies. `python3 -m unittest test_bot_gate` runs the tests; `test_frameworks.py` runs
the gate inside Flask, Starlette and FastAPI when they are installed. Both pass on Python 3.9 to 3.13 with Flask 3.1,
Starlette 0.49 to 1.10 and FastAPI 0.128 to 0.162.

## Options

| Option | Default | Meaning |
|---|---|---|
| `inflight` | 12 | requests in progress per worker above which strangers are turned away |
| `rate`, `burst` | 3.0, 10 | stranger pages per second per worker, and how many may come at once |
| `allow_ips` | none | addresses that always pass |
| `skip_prefixes` | API, webhooks, static, sitemaps, health, feeds... | path prefixes never touched |
| `cookies` | any cookie | names of cookies that mean "has been here before" |
| `behind_cloudflare` | True | trust `CF-Connecting-IP`; False: use `X-Forwarded-For` or the peer address |
| `title`, `secure_cookie`, `cookie_name`, `report_every` | | the 503 page's text, the cookie's flags and name, the log interval |

The limits are per worker process: with 4 workers and `rate=3`, up to 12 stranger pages a second are served in all.

## The rest of the kit

This is the in-app part of **[Night Watch](https://lightsoutfactory.ai/kits/night-watch/)**, which also watches every
site you run and emails you when one goes down and when it is back, installs three Cloudflare Free-plan rules that stop
most floods at the edge (the free version is [cloudflare-free-shield](https://github.com/lightsoutfactory/cloudflare-free-shield)),
switches the emergency one on by itself during a flood and summarises what Cloudflare's firewall challenged.

Also free from us: [cloudflare-free-shield](https://github.com/lightsoutfactory/cloudflare-free-shield) (three
Cloudflare Free-plan rules against scraper floods),
[translation-checks](https://github.com/lightsoutfactory/translation-checks) (catch translations that would break your
app) and [vllm-health](https://github.com/lightsoutfactory/vllm-health) (a vLLM health check that never waits in the
queue).

MIT licence.
