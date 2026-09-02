#!/usr/bin/env python3
"""
wikijs-search-shim — a URL-addressable search front end for Wiki.js 2.x.

Wiki.js 2.x has no /search?q= route; search is a client-side overlay only.
This shim adds one, by proxying to the Wiki.js GraphQL search query. That
gives you a linkable results URL, browser omnibox keyword search, and an
OpenSearch descriptor for auto-discovery.

Standard library only. No pip install, no build step, no framework.

Routes
  GET /search?q=TERM        HTML results page
  GET /search?q=TERM&f=json JSON results (for scripts)
  GET /opensearch.xml       OpenSearch descriptor
  GET /healthz              liveness probe
  GET /                     empty search page

Environment
  WIKI_URL      required   e.g. https://wiki.example.lan
  WIKI_TOKEN    optional   API token; omit if guests can read the wiki
  WIKI_LOCALE   default en
  PUBLIC_URL    default derived from Host header; set it if behind a proxy
  LISTEN_ADDR   default 127.0.0.1
  LISTEN_PORT   default 8099
  SITE_NAME     default Wiki
  TIMEOUT       default 10 (seconds)
"""

import html
import json
import os
import signal
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WIKI_URL = os.environ.get("WIKI_URL", "").rstrip("/")
WIKI_TOKEN = os.environ.get("WIKI_TOKEN", "").strip()
WIKI_LOCALE = os.environ.get("WIKI_LOCALE", "en")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
LISTEN_ADDR = os.environ.get("LISTEN_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))
SITE_NAME = os.environ.get("SITE_NAME", "Wiki")
TIMEOUT = float(os.environ.get("TIMEOUT", "10"))

GRAPHQL = """
query ($q: String!, $locale: String!) {
  pages {
    search(query: $q, locale: $locale) {
      results { id title description path locale }
      suggestions
      totalHits
    }
  }
}
"""


class SearchError(Exception):
    pass


def search(term):
    """Query Wiki.js. Returns (results, suggestions, total_hits)."""
    payload = json.dumps(
        {"query": GRAPHQL, "variables": {"q": term, "locale": WIKI_LOCALE}}
    ).encode("utf-8")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if WIKI_TOKEN:
        headers["Authorization"] = "Bearer " + WIKI_TOKEN

    req = urllib.request.Request(
        WIKI_URL + "/graphql", data=payload, headers=headers, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SearchError(
            "Wiki.js returned HTTP %d. Check WIKI_URL and that the API is "
            "enabled under Administration \u2192 API Access." % exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise SearchError("Could not reach %s (%s)." % (WIKI_URL, exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise SearchError("Wiki.js sent a response that was not JSON.") from exc

    if body.get("errors"):
        msg = body["errors"][0].get("message", "unknown GraphQL error")
        raise SearchError("Wiki.js rejected the query: %s" % msg)

    try:
        block = body["data"]["pages"]["search"]
    except (KeyError, TypeError) as exc:
        raise SearchError("Unexpected response shape from Wiki.js.") from exc

    return (
        block.get("results") or [],
        block.get("suggestions") or [],
        block.get("totalHits") or 0,
    )


def page_url(result):
    locale = result.get("locale") or WIKI_LOCALE
    path = (result.get("path") or "").lstrip("/")
    return "%s/%s/%s" % (WIKI_URL, locale, path)


CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --fg: #1c1c1a; --muted: #5f6360;
  --link: #14532d; --visited: #4a2d63; --rule: #dedcd6; --field: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --fg: #e6e4df; --muted: #969a97;
    --link: #7fc79b; --visited: #b7a1d4; --rule: #2b2f33; --field: #1e2126;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
main { max-width: 44rem; margin: 0 auto; }
form { display: flex; gap: .5rem; margin: 0 0 1.75rem; }
input[type=search] {
  flex: 1; padding: .6rem .75rem; font-size: 1rem; color: var(--fg);
  background: var(--field); border: 1px solid var(--rule); border-radius: 3px;
}
button {
  padding: .6rem 1.1rem; font-size: 1rem; color: var(--bg); cursor: pointer;
  background: var(--fg); border: 1px solid var(--fg); border-radius: 3px;
}
:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.count { color: var(--muted); font-size: .875rem; margin: 0 0 1.5rem; }
ol { list-style: none; margin: 0; padding: 0; }
li { padding: 0 0 1.4rem; }
li + li { border-top: 1px solid var(--rule); padding-top: 1.4rem; }
a.title { color: var(--link); font-size: 1.0625rem; text-decoration: none; }
a.title:visited { color: var(--visited); }
a.title:hover { text-decoration: underline; }
.path { color: var(--muted); font-size: .8125rem; margin: .15rem 0 .35rem; }
.desc { margin: 0; }
.notice { border-left: 3px solid var(--rule); padding-left: .9rem; color: var(--muted); }
.suggest { color: var(--muted); font-size: .9375rem; margin-top: 1.5rem; }
.suggest a { color: var(--link); }
"""


def render(term, results, suggestions, total, error, public):
    esc = html.escape
    head = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title>"
        "<link rel=\"search\" type=\"application/opensearchdescription+xml\" "
        "href=\"%s/opensearch.xml\" title=\"%s\">"
        "<style>%s</style></head><body><main>"
        % (
            esc("%s \u2014 %s" % (term, SITE_NAME)) if term else esc("Search " + SITE_NAME),
            esc(public),
            esc(SITE_NAME),
            CSS,
        )
    )

    form = (
        "<form action=\"/search\" method=\"get\" role=\"search\">"
        "<input type=\"search\" name=\"q\" value=\"%s\" autofocus "
        "placeholder=\"Search %s\" aria-label=\"Search terms\">"
        "<button type=\"submit\">Search</button></form>"
        % (esc(term), esc(SITE_NAME))
    )

    if error:
        body = "<p class=\"notice\">%s</p>" % esc(error)
    elif not term:
        body = "<p class=\"notice\">Type a query, or add this page as a browser search engine.</p>"
    elif not results:
        body = "<p class=\"notice\">No pages matched that query.</p>"
        if suggestions:
            links = ", ".join(
                "<a href=\"/search?q=%s\">%s</a>"
                % (urllib.parse.quote(s), esc(s))
                for s in suggestions[:6]
            )
            body += "<p class=\"suggest\">Try instead: %s</p>" % links
    else:
        items = []
        for r in results:
            url = page_url(r)
            desc = r.get("description") or ""
            items.append(
                "<li><a class=\"title\" href=\"%s\">%s</a>"
                "<div class=\"path\">%s</div>%s</li>"
                % (
                    esc(url),
                    esc(r.get("title") or r.get("path") or "(untitled)"),
                    esc(url),
                    "<p class=\"desc\">%s</p>" % esc(desc) if desc else "",
                )
            )
        plural = "" if total == 1 else "s"
        body = (
            "<p class=\"count\">%d page%s</p><ol>%s</ol>"
            % (total, plural, "".join(items))
        )

    return (head + form + body + "</main></body></html>").encode("utf-8")


def opensearch_xml(public):
    esc = html.escape
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<OpenSearchDescription xmlns=\"http://a9.com/-/spec/opensearch/1.1/\">"
        "<ShortName>%s</ShortName>"
        "<Description>Search %s</Description>"
        "<InputEncoding>UTF-8</InputEncoding>"
        "<Url type=\"text/html\" method=\"get\" template=\"%s/search?q={searchTerms}\"/>"
        "<Url type=\"application/json\" method=\"get\" "
        "template=\"%s/search?f=json&amp;q={searchTerms}\"/>"
        "</OpenSearchDescription>"
        % (esc(SITE_NAME[:16]), esc(SITE_NAME), esc(public), esc(public))
    ).encode("utf-8")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "wikijs-search-shim"
    protocol_version = "HTTP/1.1"

    def public_base(self):
        if PUBLIC_URL:
            return PUBLIC_URL
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return "%s://%s" % (proto, host) if host else ""

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        term = (params.get("q", [""])[0]).strip()
        public = self.public_base()

        if parsed.path == "/healthz":
            return self.reply(200, b"ok\n", "text/plain; charset=utf-8")

        if parsed.path == "/opensearch.xml":
            return self.reply(
                200,
                opensearch_xml(public),
                "application/opensearchdescription+xml; charset=utf-8",
            )

        if parsed.path not in ("/", "/search"):
            return self.reply(404, b"not found\n", "text/plain; charset=utf-8")

        results, suggestions, total, error = [], [], 0, None
        if term:
            try:
                results, suggestions, total = search(term)
            except SearchError as exc:
                error = str(exc)

        if params.get("f", [""])[0] == "json":
            payload = json.dumps(
                {
                    "query": term,
                    "totalHits": total,
                    "suggestions": suggestions,
                    "results": [
                        {
                            "title": r.get("title"),
                            "description": r.get("description"),
                            "url": page_url(r),
                        }
                        for r in results
                    ],
                    "error": error,
                },
                indent=2,
            ).encode("utf-8")
            return self.reply(
                502 if error else 200, payload, "application/json; charset=utf-8"
            )

        body = render(term, results, suggestions, total, error, public)
        self.reply(502 if error else 200, body, "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    if not WIKI_URL:
        sys.exit("WIKI_URL is not set. Example: WIKI_URL=https://wiki.example.lan")
    server = Server((LISTEN_ADDR, LISTEN_PORT), Handler)
    sys.stderr.write(
        "wikijs-search-shim listening on %s:%d, proxying to %s\n"
        % (LISTEN_ADDR, LISTEN_PORT, WIKI_URL)
    )

    def terminate(signum, frame):
        # shutdown() blocks until serve_forever() returns, so it cannot run in
        # the thread that owns serve_forever(). Hand it to a throwaway thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
