#!/usr/bin/env python3
"""Acceptance tests for wikijs-search-shim. Stdlib only, no live wiki.

    python3 test_shim.py

Covers items 1-9 of HANDOVER section 9. Item 10 (container build + stop timing)
is a manual step, see README.
"""

import http.client
import importlib.util
import io
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

os.environ.setdefault("WIKI_URL", "http://10.255.255.1")  # unroutable on purpose
os.environ.setdefault("PUBLIC_URL", "https://search.example.lan")
os.environ.setdefault("WIKI_LOCALE", "en")
os.environ.setdefault("SITE_NAME", "Homelab Wiki")
os.environ["LISTEN_PORT"] = "0"

_spec = importlib.util.spec_from_file_location("shim", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wikijs-search-shim.py"))
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def stub_graphql(results=None, suggestions=None, total=None, errors=None,
                 raw=None, exc=None):
    def _urlopen(req, timeout=None):
        if exc is not None:
            raise exc
        if raw is not None:
            return _Resp(raw.encode("utf-8"))
        body = {"data": {"pages": {"search": {
            "results": results or [],
            "suggestions": suggestions or [],
            "totalHits": total if total is not None else len(results or []),
        }}}}
        if errors is not None:
            body = {"errors": errors}
        return _Resp(json.dumps(body).encode("utf-8"))
    shim.urllib.request.urlopen = _urlopen


def serve():
    srv = shim.Server(("127.0.0.1", 0), shim.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read().decode("utf-8")
    hdrs = r.headers
    conn.close()
    return r.status, body, hdrs


# 1. healthz never touches Wiki.js (WIKI_URL is unroutable)
srv, base = serve()
code, body, _ = get(base, "/healthz")
check("1 healthz 200 without wiki", code == 200 and body.strip() == "ok")

# 2. opensearch.xml well-formed, ShortName <= 16, absolute template honoring PUBLIC_URL
code, body, hdrs = get(base, "/opensearch.xml")
ok = code == 200
try:
    root = ET.fromstring(body)
    ns = "{http://a9.com/-/spec/opensearch/1.1/}"
    short = root.findtext(ns + "ShortName")
    urls = [u.get("template") for u in root.findall(ns + "Url")]
    ok = (ok and len(short) <= 16
          and all(u.startswith("https://search.example.lan/") for u in urls))
except ET.ParseError:
    ok = False
check("2 opensearch.xml valid, ShortName<=16, absolute PUBLIC_URL template", ok)

# 3. two results render titles, correct URLs, "2 pages"
stub_graphql(results=[
    {"title": "UniFi", "description": "controller notes", "path": "net/unifi", "locale": "en"},
    {"title": "Proxmox", "description": "", "path": "virt/proxmox", "locale": "en"},
], total=2)
code, body, _ = get(base, "/search?q=net")
check("3 two results: titles + resolved URLs + '2 pages'", (
    "UniFi" in body and "Proxmox" in body
    and shim.WIKI_URL + "/en/net/unifi" in body
    and shim.WIKI_URL + "/en/virt/proxmox" in body
    and "2 pages" in body))

# 4. singular
stub_graphql(results=[{"title": "Only", "description": "", "path": "x", "locale": "en"}], total=1)
code, body, _ = get(base, "/search?q=only")
check("4 one result says '1 page' not '1 pages'",
      "1 page" in body and "1 pages" not in body)

# 5. escaping — result fields and echoed query
stub_graphql(results=[{
    "title": "<script>alert(1)</script>",
    "description": "a & b < c",
    "path": "p", "locale": "en"}], total=1)
code, body, _ = get(base, "/search?q=" + urllib.parse.quote("<img src=x onerror=1>"))
check("5 result escaping: no raw <script, has &lt;",
      "<script" not in body and "&lt;" in body)
check("5 query echo escaping: no raw <img onerror",
      "<img src=x onerror" not in body)

# 6. zero results -> empty state + suggestions as encoded links
stub_graphql(results=[], suggestions=["propved", "prox mox"], total=0)
code, body, _ = get(base, "/search?q=proxmoxx")
check("6 zero results empty state + encoded suggestion links", (
    "No pages matched" in body
    and "/search?q=prox%20mox" in body))

# 7. each error class -> 502, readable message, no traceback
cases = {
    "HTTPError": urllib.error.HTTPError(shim.WIKI_URL, 403, "Forbidden", {}, None),
    "URLError": urllib.error.URLError("name resolution failed"),
}
err_ok = True
for label, exc in cases.items():
    stub_graphql(exc=exc)
    code, body, _ = get(base, "/search?q=x")
    err_ok = err_ok and code == 502 and "Traceback" not in body and len(body) > 0
stub_graphql(raw="<html>login</html>")
code, body, _ = get(base, "/search?q=x")
err_ok = err_ok and code == 502 and "Traceback" not in body
stub_graphql(errors=[{"message": "token expired"}])
code, body, _ = get(base, "/search?q=x")
err_ok = err_ok and code == 502 and "token expired" in body
check("7 error classes -> 502, human message, no traceback", err_ok)

# 8. ?f=json -> valid JSON, resolved absolute url
stub_graphql(results=[{"title": "UniFi", "description": "n", "path": "net/unifi", "locale": "en"}], total=1)
code, body, hdrs = get(base, "/search?q=unifi&f=json")
try:
    doc = json.loads(body)
    ok = (code == 200 and doc["results"][0]["url"] == shim.WIKI_URL + "/en/net/unifi"
          and doc["totalHits"] == 1 and doc["error"] is None)
except (ValueError, KeyError, IndexError):
    ok = False
check("8 f=json valid JSON with absolute url", ok)

# 8b. json error path -> 502
stub_graphql(exc=urllib.error.URLError("down"))
code, body, _ = get(base, "/search?q=x&f=json")
check("8b f=json error -> 502 with error field",
      code == 502 and json.loads(body)["error"])

# 9. unknown path -> 404
code, body, _ = get(base, "/nope")
check("9 unknown path -> 404", code == 404)

# headers on every response
code, body, hdrs = get(base, "/healthz")
check("headers: nosniff + no-referrer present",
      hdrs.get("X-Content-Type-Options") == "nosniff"
      and hdrs.get("Referrer-Policy") == "no-referrer")

srv.shutdown()
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
