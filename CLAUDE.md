# CLAUDE.md

## Project

`wikijs-search-shim` — a small HTTP service giving Wiki.js 2.x the
URL-addressable `/search?q=` route it lacks. Proxies to the Wiki.js GraphQL
search query; Elasticsearch (the wiki's real backend) is never touched directly.
Runs bare-metal or as a rootless container so many LAN machines share one
instance. Full spec: `HANDOVER-wikijs-search-shim.md`.

## Hard constraints (do not relax without asking)

- **Python 3.11+ standard library only.** No Flask/FastAPI/requests/httpx/etc.
  No `pip install` anywhere. Solve with `urllib.request`, `json`, `html`,
  `http.server`.
- **Single application file** — `wikijs-search-shim.py`. No package layout, no
  `src/` tree.
- **Stateless.** No DB, cache, or app-data volume. Every request is a fresh
  GraphQL call.
- **No build step.** CSS is an inlined Python string. No static assets.
- **Rootless-container friendly.** Runs under rootless Podman as non-root, no
  capabilities.
- Every value interpolated into HTML/XML goes through `html.escape` — wiki
  content is attacker-influenced. `test_shim.py` item 5 guards this.

## Layout

| File | Purpose |
|---|---|
| `wikijs-search-shim.py` | the service |
| `test_shim.py` | acceptance tests, stubs GraphQL, no live wiki |
| `Containerfile` | OCI build — base image + one file, plain `COPY` + `RUN chmod 0644` (no BuildKit-only `--chmod`) |
| `compose.yaml` | podman/docker compose, loopback publish, read-only, cap_drop ALL |
| `wikijs-search-shim.container` | Podman Quadlet unit for `systemctl --user` |
| `.env.example` | config template; real `.env` is git/docker-ignored |
| `README.md` | operator-facing: contract check, run, browser setup, security |

## Build / Test

```bash
python3 test_shim.py                                    # unit/acceptance, no wiki
podman build -t localhost/wikijs-search-shim:latest -f Containerfile .
podman compose up -d --build && curl -s localhost:8099/healthz
```

Verified working: all 12 test checks pass; container builds, serves `/healthz`,
and stops in <1s on `SIGTERM`.

## Conventions

- **Dependency cooldown:** N/A while the constraint above holds — there is no
  package manager and no dependency resolution. If that constraint is ever
  lifted, add the 8-day minimum-package-age gate to whatever manager is
  introduced (see user global CLAUDE.md for per-tool values) before adding any
  dependency.
- Container images: pin the base by digest once resolvable (comment in
  `Containerfile`).
- Secrets: `WIKI_TOKEN` only ever in `.env` at mode `0600`. Wiki.js 2.x tokens
  are unscoped = full admin. Never commit, never log.

## Open operator decisions (from HANDOVER §12)

Placeholders used throughout: `wiki.example.lan`, `search.example.lan`. Before a
real deployment confirm: wiki hostname + whether guest read is enabled (→ token
needed at all?); which reverse proxy and the shim's public hostname; Podman vs
Docker and rootless vs rootful (→ Quadlet or compose as primary); whether the
container joins the wiki's container network or reaches it over the LAN.
