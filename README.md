# wikijs-search-shim

Wiki.js 2.x has no `/search?q=` results page — search is a client-side overlay in
the header, so there is no linkable result URL and no browser omnibox keyword.
This shim adds that route by proxying to the Wiki.js GraphQL search query.

```
browser / script  ──►  shim :8099  ──►  Wiki.js /graphql  ──►  Elasticsearch
```

Elasticsearch is untouched — it is already the wiki's search backend and the shim
never talks to it directly. The shim adds no index, no cache, no state. Every
request is a fresh GraphQL call.

- Python 3.11+ standard library only. No pip install, no framework, no build step.
- One application file: `wikijs-search-shim.py`.
- Runs bare-metal or as a rootless Podman / Docker container.

## Routes

| Route | Behavior |
|---|---|
| `GET /search?q=TERM` | HTML results page. Empty/missing `q` shows the form, not an error. |
| `GET /search?q=TERM&f=json` | Same query, JSON body. For scripts and other services. |
| `GET /opensearch.xml` | OpenSearch 1.1 descriptor for browser auto-discovery. |
| `GET /healthz` | `200 ok`. Never touches Wiki.js — liveness probe for the shim only. |
| `GET /` | Same as `/search` with no query. |
| anything else | `404` plain text. |

Only `GET` is served.

## Configuration

Environment variables only. No config file, no CLI flags.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WIKI_URL` | yes | — | Wiki.js base URL, no trailing slash. Process exits if unset. |
| `WIKI_TOKEN` | no | empty | API token. Omit entirely if guests can read the wiki. |
| `WIKI_LOCALE` | no | `en` | Locale passed to the search query. |
| `PUBLIC_URL` | no | derived | External base URL. Set it for any proxied deployment. |
| `LISTEN_ADDR` | no | `127.0.0.1` | Bind address. The container image overrides this to `0.0.0.0`. |
| `LISTEN_PORT` | no | `8099` | Bind port. |
| `SITE_NAME` | no | `Wiki` | Page title, form placeholder, OpenSearch `ShortName` (≤16 chars). |
| `TIMEOUT` | no | `10` | Seconds before giving up on the GraphQL call. |

When `PUBLIC_URL` is unset the base is derived from `X-Forwarded-Proto` /
`X-Forwarded-Host`, falling back to `Host`. Setting `PUBLIC_URL` explicitly is the
recommendation behind a proxy — it makes the OpenSearch templates correct without
depending on forwarded headers.

## Verify the API contract first

Before deploying, confirm the GraphQL search query works against your wiki and
decide whether a token is needed:

```bash
# with a token
curl -s https://wiki.example.lan/graphql \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $WIKI_TOKEN" \
  -d '{"query":"query($q:String!,$l:String!){pages{search(query:$q,locale:$l){results{id title description path locale} suggestions totalHits}}}","variables":{"q":"test","l":"en"}}' \
  | python3 -m json.tool

# repeat with NO Authorization header — if it still returns results,
# guest read is enabled and you should run the shim with no token.
```

`path` has no leading slash and no locale prefix; the viewable URL is
`{WIKI_URL}/{locale}/{path}`. GraphQL returns HTTP 200 even on failure with an
`errors` array — the shim checks for that and returns HTTP 502.

## Run bare-metal

```bash
WIKI_URL=https://wiki.example.lan \
PUBLIC_URL=https://search.example.lan \
SITE_NAME="Homelab Wiki" \
python3 wikijs-search-shim.py
```

Binds `127.0.0.1:8099` by default. `Ctrl-C` or `SIGTERM` stops it cleanly.

## Run as a container

Two supported paths. Pick one.

### compose

```bash
cp .env.example .env && chmod 600 .env    # then edit .env
podman compose up -d --build               # or: docker compose up -d --build
curl -s http://127.0.0.1:8099/healthz
```

`compose.yaml` publishes `127.0.0.1:8099` only (a reverse proxy on the host fronts
the public name and TLS), runs read-only with all capabilities dropped, and
restarts unless stopped.

### Podman Quadlet (systemd-native)

```bash
podman build -t localhost/wikijs-search-shim:latest -f Containerfile .
mkdir -p ~/.config/containers/systemd
cp wikijs-search-shim.container ~/.config/containers/systemd/
cp .env.example ~/.config/containers/systemd/wikijs-search-shim.env
chmod 600 ~/.config/containers/systemd/wikijs-search-shim.env   # then edit it
systemctl --user daemon-reload
systemctl --user start wikijs-search-shim
loginctl enable-linger "$USER"    # start at boot without a login session
```

### Note on the image HEALTHCHECK

`podman build` defaults to OCI format, which drops the `HEALTHCHECK` line with a
warning. `compose.yaml` and typical proxy checks do their own probing, so this is
usually fine. If you want the baked-in healthcheck, build with
`podman build --format docker ...`.

## Multi-system access

The point of the container is that many machines share one instance.

- Terminate TLS at your existing reverse proxy on a name like `search.example.lan`
  and proxy to the container. Both Firefox and Chrome want HTTPS before treating a
  URL as a real search engine.
- nginx: forward `X-Forwarded-Proto` and `X-Forwarded-Host`, or just set
  `PUBLIC_URL` and skip the header dependency (more robust).
- **Firefox:** visit `https://search.example.lan/` once; Firefox discovers the
  engine from the `<link rel="search">` tag. Assign a keyword in
  Settings → Search → Search Shortcuts.
- **Chrome:** no longer auto-adds reliably. Add it manually at
  `chrome://settings/searchEngines` with URL
  `https://search.example.lan/search?q=%s`.

## Security

- **Wiki.js 2.x API tokens are not scoped.** A token is effectively full admin,
  including write and delete. Treat it as a root credential.
- The token lives only in `.env` at mode `0600`, referenced via `env_file` /
  `EnvironmentFile`. Never in the Containerfile, compose file, a committed file,
  or a log line. `.env` is gitignored and dockerignored.
- **Prefer no token.** If guest read is enabled the search query works
  unauthenticated and this whole risk class disappears.
- Anyone who can reach the shim sees everything the token can see. Keep it on the
  LAN; do not expose it to the internet without auth in front of it.
- Every value the wiki returns is passed through `html.escape` before rendering —
  this is what stops a malicious page title from becoming stored XSS on the
  results page. `test_shim.py` covers it.

## Tests

```bash
python3 test_shim.py
```

Stubs the GraphQL call — no live wiki needed. Covers items 1–9 of the handover
acceptance list (routes, escaping, error handling, OpenSearch, JSON output).
Item 10 (container build, `/healthz` through the published port, `podman stop`
under two seconds) is verified manually with the commands above.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| HTTP 502, "returned HTTP 4xx/5xx" | wrong `WIKI_URL`, or API disabled under Administration → API Access |
| HTTP 502, "could not reach" | DNS / network path to the wiki |
| HTTP 502, "not JSON" | a proxy or login wall between shim and wiki returning HTML |
| HTTP 502, "rejected the query" | bad or expired `WIKI_TOKEN` (message is surfaced from GraphQL) |
| container exits immediately, "Permission denied" reading the script | rebuild — the Containerfile sets `--chmod=0644` on COPY |
| OpenSearch templates point at `http://` or wrong host | set `PUBLIC_URL` |

## Out of scope

No direct Elasticsearch access, no auth/sessions on the shim, no pagination or
faceting, no result caching, no write path to the wiki, no client-side
JavaScript, no changes to Wiki.js itself.
