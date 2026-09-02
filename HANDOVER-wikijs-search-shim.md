# Handover: `wikijs-search-shim`

A small HTTP service that gives Wiki.js 2.x a URL-addressable search endpoint, packaged
as a container so it runs continuously and serves every machine on the LAN.

Audience: a coding agent with filesystem and shell access. Build the whole thing, test it,
hand back a running container.

---

## 1. Objective

Wiki.js 2.5.314 has no search results page. Search is a client-side overlay in the header,
so there is no `/search?q=foo` route. That means no browser omnibox keyword, no linkable
result URLs, no scripted queries. A results page with URL parameters was only added in v3.

Build a shim that sits beside the wiki and provides that missing route by proxying to the
Wiki.js GraphQL API. Elasticsearch stays exactly where it is — it is already wired up as
the wiki's search backend, and the shim never talks to it directly.

```
browser / script  ──►  shim :8099  ──►  Wiki.js /graphql  ──►  Elasticsearch
```

Success looks like: any machine on the network can hit
`https://search.<domain>/search?q=proxmox` and get ranked results, and Firefox/Chrome can
register that URL as a search engine with a keyword.

---

## 2. Constraints

These are firm. Do not relax them without asking.

- **Python 3.11+ standard library only.** No Flask, FastAPI, requests, httpx, Jinja,
  aiohttp. No `pip install` anywhere in the build. If a requirement seems to need a
  dependency, solve it with `urllib.request`, `json`, `html`, and `http.server` instead.
- **Single application file.** All server logic in one `.py`. No package layout, no
  `src/` tree, no `__init__.py`.
- **Stateless.** No database, no cache, no volumes for app data. Every request is a
  fresh GraphQL call.
- **No build step.** No npm, no bundler, no compilation. CSS is inlined in a Python
  string; there are no static asset files.
- **Rootless-friendly container.** Must run under rootless Podman as a non-root user
  without capabilities.

---

## 3. Deliverables

```
wikijs-search-shim/
├── wikijs-search-shim.py       # the service
├── Containerfile               # OCI build (Docker-compatible syntax)
├── compose.yaml                # docker compose / podman compose
├── wikijs-search-shim.container # Podman Quadlet unit (systemd-native alternative)
├── .env.example                # documented config template
├── .dockerignore
└── README.md                   # operator-facing: build, run, browser setup, troubleshooting
```

A working reference implementation of `wikijs-search-shim.py` may be supplied alongside
this document. If it is, start from it and apply the deltas in §7 rather than rewriting.
If it is not, build to the spec in §5 and §6.

---

## 4. Verify the API contract first

Before writing application code, confirm the GraphQL contract against the live wiki. Do
not skip this — the response shape drives everything downstream.

```bash
curl -s https://wiki.<domain>/graphql \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $WIKI_TOKEN" \
  -d '{"query":"query($q:String!,$l:String!){pages{search(query:$q,locale:$l){results{id title description path locale} suggestions totalHits}}}","variables":{"q":"test","l":"en"}}' \
  | python3 -m json.tool
```

Expected shape:

```json
{ "data": { "pages": { "search": {
  "results": [ { "id": 12, "title": "...", "description": "...", "path": "net/unifi", "locale": "en" } ],
  "suggestions": [],
  "totalHits": 1
} } } }
```

Notes on the contract:

- `path` has no leading slash and no locale prefix. The viewable page URL is
  `{WIKI_URL}/{locale}/{path}`.
- `description` is frequently an empty string. Handle that, do not render an empty block.
- `suggestions` comes from the Elasticsearch completion suggester and is usually empty on
  a successful match. It is populated when the query misses.
- `totalHits` may exceed `len(results)` — Wiki.js caps the returned set. Report `totalHits`
  in the UI, render what you got.
- GraphQL returns HTTP 200 even on failure, with an `errors` array in the body. Check for
  `errors` before reading `data`.

If the wiki allows guest reads, the same call succeeds with no `Authorization` header.
Test both and record which one this deployment needs.

---

## 5. Application spec

### Routes

| Route | Behavior |
|---|---|
| `GET /search?q=TERM` | HTML results page. Empty or missing `q` renders the form with a hint, not an error. |
| `GET /search?q=TERM&f=json` | Same query, JSON body. For scripts and other services. |
| `GET /opensearch.xml` | OpenSearch 1.1 descriptor, served as `application/opensearchdescription+xml`. |
| `GET /healthz` | `200 ok`. Must not touch Wiki.js — it is a liveness probe for the shim only. |
| `GET /` | Same as `/search` with no query. |
| anything else | `404` plain text. |

Only `GET` is supported. Other methods may 404 or 405.

### Configuration

Environment variables only. No config file, no CLI flags.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WIKI_URL` | yes | — | Wiki.js base URL, no trailing slash. Exit non-zero at startup if unset. |
| `WIKI_TOKEN` | no | empty | API token. Omit entirely if guests can read the wiki. |
| `WIKI_LOCALE` | no | `en` | Locale passed to the search query. |
| `PUBLIC_URL` | no | derived | External base URL. Needed for correct OpenSearch templates behind a proxy. |
| `LISTEN_ADDR` | no | `127.0.0.1` | Bind address. The container image overrides this to `0.0.0.0`. |
| `LISTEN_PORT` | no | `8099` | Bind port. |
| `SITE_NAME` | no | `Wiki` | Shown in page title, form placeholder, OpenSearch `ShortName`. |
| `TIMEOUT` | no | `10` | Seconds before giving up on the GraphQL call. |

When `PUBLIC_URL` is unset, derive the base from `X-Forwarded-Proto` and
`X-Forwarded-Host`, falling back to `Host`. Setting `PUBLIC_URL` explicitly is the
documented recommendation for any proxied deployment.

### HTML output

Server-rendered, single page, no client-side JavaScript at all. CSS inlined in a `<style>`
block. Requirements:

- A search form at the top that posts back to `/search` via GET, prefilled with the
  current query, `autofocus`, `type="search"`, and an `aria-label`.
- `<link rel="search" type="application/opensearchdescription+xml" href="{PUBLIC_URL}/opensearch.xml">`
  in the head so Firefox can discover it.
- Result list: linked title, the full destination URL as a secondary line, description
  when present.
- Hit count above the list, singular/plural correct.
- Distinct states for: no query yet, zero results (offer `suggestions` as clickable
  links when present), and backend error.
- `prefers-color-scheme` handled via CSS custom properties. Visible `:focus-visible`
  outline. Readable at 380px wide.
- **Every interpolated value goes through `html.escape`.** Titles, descriptions, paths,
  and the echoed query are all attacker-influenced. There is a test for this in §9.

Keep the visual design plain and functional — this is a utility page a person looks at for
two seconds before clicking through. No hero, no cards, no animation.

### JSON output

```json
{
  "query": "unifi",
  "totalHits": 2,
  "suggestions": [],
  "results": [ { "title": "...", "description": "...", "url": "https://wiki.../en/net/unifi" } ],
  "error": null
}
```

Resolve `url` server-side so consumers never have to know the locale-prefix rule.

### Error handling

Never render a stack trace. Catch and convert to an operator-readable sentence:

| Condition | Message should point at |
|---|---|
| `urllib.error.HTTPError` | wrong `WIKI_URL`, or API not enabled under Administration → API Access |
| `urllib.error.URLError` | DNS/network reachability to the wiki |
| `json.JSONDecodeError` | got HTML instead of JSON — usually a proxy or login wall in the path |
| GraphQL `errors` present | surface `errors[0].message` (bad or expired token lands here) |

Return HTTP 502 with the rendered error page or error JSON, so monitoring can distinguish
"shim is up but the wiki is broken" from a normal empty result set.

### Response headers

Set on every response: `Content-Type` with charset, `Content-Length`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

---

## 6. Container spec

### Containerfile

- Base `docker.io/library/python:3.13-slim`. Pin by digest if you can resolve one.
- No `RUN pip install`. No `apt-get`. The image should be base + one copied file.
- `COPY` the script to `/app/wikijs-search-shim.py`.
- Create and switch to a non-root user, e.g. UID 10001. Rootless Podman maps this into the
  user namespace fine.
- `ENV LISTEN_ADDR=0.0.0.0 LISTEN_PORT=8099 PYTHONUNBUFFERED=1`. The loopback default in
  the script is correct for bare-metal use and wrong for a container, so override it here
  rather than changing the script default.
- `EXPOSE 8099`.
- `HEALTHCHECK` — there is no curl in the slim image, so use the interpreter:
  ```
  HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz',timeout=3).status==200 else 1)"
  ```
- `ENTRYPOINT ["python3", "/app/wikijs-search-shim.py"]`.

### compose.yaml

- Publish `127.0.0.1:8099:8099` by default, with a commented note that binding `0.0.0.0`
  is only appropriate when a reverse proxy is not fronting the service.
- `env_file: .env`. Do not put the token inline in the compose file.
- `restart: unless-stopped`.
- `read_only: true`, `tmpfs: /tmp`, `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]`. Nothing in this service writes to disk.
- Optionally attach to the existing wiki network so the shim can reach Wiki.js by
  container name instead of over the LAN. Leave this commented with a note, since the
  network name is deployment-specific.

### Quadlet unit

Provide `wikijs-search-shim.container` for `~/.config/containers/systemd/`, so it can be
managed with `systemctl --user` and started at boot via lingering. Cover
`[Container] Image/EnvironmentFile/PublishPort`, `[Service] Restart=always`, and
`[Install] WantedBy=default.target`. Mention `loginctl enable-linger` in the README.

### Lifecycle

`podman stop` must not take ten seconds. Install a `SIGTERM` handler that calls
`server.shutdown()`, and set `daemon_threads = True` on the `ThreadingHTTPServer` subclass
so in-flight keep-alive connections do not hold the process open. Keep the existing
`KeyboardInterrupt` path for foreground runs.

---

## 7. Deltas from the reference implementation

If the reference `wikijs-search-shim.py` was supplied, these are the only changes it needs:

1. Add a `SIGTERM` handler that triggers `server.shutdown()`.
2. Set `daemon_threads = True` on the server class.
3. Nothing else. `LISTEN_ADDR` is overridden by the image `ENV`, not by editing the script.

Everything else in the deliverables list is new.

---

## 8. Multi-system access

The point of the container is that several machines use one instance.

- Terminate TLS at the existing reverse proxy on a hostname like `search.<domain>`, and
  proxy to the container. Both Firefox and Chrome want HTTPS before they will treat a URL
  as a real search engine.
- If the proxy is nginx, forward `X-Forwarded-Proto` and `X-Forwarded-Host`, or just set
  `PUBLIC_URL` and skip the header dependency. Setting `PUBLIC_URL` is more robust.
- No CSP or iframe concerns here — the page is standalone, first-party, and loads no
  external resources.
- Browser setup for the README: Firefox discovers the engine from the `<link rel="search">`
  after one visit, then a keyword is assigned in Settings → Search. Chrome no longer
  auto-adds reliably, so document the manual path at `chrome://settings/searchEngines`
  with `https://search.<domain>/search?q=%s`.

---

## 9. Acceptance tests

Write these as a script that stubs the GraphQL call — do not require a live wiki to run
the suite. Point it at a temporary server on a high port.

1. `/healthz` returns 200 without contacting Wiki.js. Prove it by leaving `WIKI_URL`
   pointed at an unroutable host.
2. `/opensearch.xml` is well-formed XML (parse it with `xml.etree.ElementTree`), the
   `ShortName` is 16 characters or fewer per the OpenSearch spec, and the template is an
   absolute URL honoring `PUBLIC_URL`.
3. A stubbed two-result response renders both titles, links to
   `{WIKI_URL}/{locale}/{path}`, and shows "2 pages".
4. A one-result response says "1 page", not "1 pages".
5. **Escaping.** Stub a result whose title is `<script>alert(1)</script>` and whose
   description contains `&` and `<`. Assert the raw `<script` substring does not appear
   in the response body and that `&lt;` does. Repeat with the echoed query string:
   `/search?q=<img src=x onerror=1>`.
6. Zero results renders the empty state, and stubbed suggestions render as links with
   correctly percent-encoded `q` values.
7. Each error class in §5 produces HTTP 502, a human-readable message, and no traceback
   in the body.
8. `?f=json` returns valid JSON with resolved absolute `url` fields.
9. An unknown path returns 404.
10. Container: build it, run it, `curl` `/healthz` through the published port, then
    `podman stop` and confirm it exits in under two seconds.

Report which of these pass. If any cannot pass, say so explicitly rather than adjusting
the test.

---

## 10. Security requirements

- Wiki.js 2.x API keys are **not finely scoped** — a token is effectively full admin
  access to the wiki, including write and delete. Treat it as a root credential.
- The token lives in `.env` at mode `0600`, referenced by `env_file`. Never in the
  Containerfile, never in compose, never in a committed file, never in a log line.
- `.env` goes in `.gitignore` and `.dockerignore`. Ship `.env.example` with placeholders.
- Prefer no token at all. If guest read is enabled on the wiki, the search query works
  unauthenticated and this whole class of risk disappears. Test for this in §4 and
  recommend it in the README if it works.
- Anyone who can reach the shim sees everything the token can see. Keep it on the LAN.
  Do not expose it to the internet without authentication in front of it.
- The shim renders content the wiki returns. Escaping (§9 test 5) is the control that
  keeps a malicious page title from becoming stored XSS on the search page. Do not skip it.

---

## 11. Out of scope

Do not build these. If any looks necessary, stop and ask.

- Talking to Elasticsearch directly. The wiki owns that index; bypassing Wiki.js also
  bypasses its page permissions.
- Authentication, sessions, or user accounts on the shim.
- Pagination, faceting, filters, or result caching.
- Any write path to the wiki.
- Client-side JavaScript, a SPA, or an autocomplete dropdown.
- Modifying the Wiki.js installation itself.

---

## 12. Operator decisions to confirm before starting

Ask about these rather than guessing:

- The wiki's hostname and whether guest read is enabled (determines whether a token is
  needed at all).
- Which reverse proxy is in front, and the hostname the shim should answer on.
- Docker or Podman, and rootless or rootful — determines whether the Quadlet unit or the
  compose file is the primary path in the README.
- Whether the container should join the existing wiki container network or reach the wiki
  over the LAN.
