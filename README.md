# knot-api

This is a REST API wrapper for managing zones on a **local Knot DNS primary** that runs on
the binary LMDB confdb and publishes its zones to secondaries through a **generated catalog zone**.

## How it works

| Operation | What happens                                                                                                                                                                                        |
|---|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Create** (`PUT`, zone unknown) | validate name → `kzonecheck` the body → write zonefile atomically → *txn:* `conf-set 'zone[X]'` + `conf-set 'zone[X].template'` → commit (zone loads from the file) → report the initial serial     |
| **Update** (`PUT`, zone known) | validate → atomically replace the zonefile → blocking `zone-reload` - **no config transaction at all**; Knot diffs the file, bumps the serial, notifies secondaries                                 |
| **Delete** (`DELETE`) | *txn:* `conf-unset 'zone[X]'` → commit → `zone-purge +orphan` (journal, timers, KASP, catalog state) → unlink the zonefile (an orphan purge cannot delete the file - Knot no longer knows its path) |

All file I/O and validation happen **outside** the transaction window, so a
transaction only ever contains two or three control messages. On any failure
the transaction is aborted (with a fresh-connection fallback), and a create
rolls its zonefile back so the filesystem stays consistent with the confdb.
If someone else (e.g. an admin in `knotc`) has a transaction open, the API
retries with backoff and eventually answers `503` + `Retry-After`.

The catalog zone updates automatically on the primary when member zones are
added or removed; secondaries purge de-cataloged zones themselves. The API
never has to touch the secondaries.

## Requirements

- Knot DNS **3.x** on the same host (control socket access) - tested with 3.5.x
- Python **3.12+**
- `libknot.so.16` and `kzonecheck` available
- The PyPI `libknot` bindings are pinned `>=3.5,<3.6` with the installed Knot major.minor.

### Required knotd template

Zones created by the API get `template: member` (configurable). The template
**must** contain the settings from [`deploy/member-template.conf`](deploy/member-template.conf):

```yaml
template:
  - id: member
    storage: /var/lib/knot/zones      # must equal KNOT_API_ZONES_DIR
    file: "%s.zone"
    catalog-role: member
    catalog-zone: <your-catalog-zone>.
    zonefile-load: difference-no-serial
    zonefile-sync: -1                 # load-bearing, see below
    journal-content: all              # hard requirement of difference-no-serial
    serial-policy: unixtime
```

Explanation:

- `zonefile-load: difference-no-serial` - **Knot owns the SOA serial.**
  Clients submit zonefiles with any serial; Knot computes an IXFR-friendly
  diff on each reload and bumps the serial per `serial-policy`. The serial
  in the on-disk file is intentionally stale; the live serial is in the API
  response / `zone-status`.
- `zonefile-sync: -1` - Knot must never flush zones back to disk, otherwise
  its background flushes race the API's atomic file writes and can silently
  clobber an update.

## Configuration

Via environment variables or an `.env` file - see [`.env.example`](.env.example).
The only required value is `KNOT_API_TOKEN`. Set `KNOT_API_CATALOG_ZONE` so
the catalog zone is refused for writes (`403`); a PUT would otherwise flip it
to the member template.

## Running

```console
$ python3 -m venv .venv && .venv/bin/pip install .
$ KNOT_API_TOKEN=... .venv/bin/uvicorn --factory knot_api.app:create_app \
      --host 127.0.0.1 --port 8080 --workers 1
```

**`--workers 1` is required.** Zone and transaction locks are in-process;
multiple workers would race each other (knotd would still serialize
transactions, but ordering guarantees per zone would be gone). One worker
with FastAPI's threadpool handles concurrent requests fine: different zones
proceed in parallel, same-zone requests serialize.

Run as the `knot` user (or a member of the knot group): the process needs the
control socket (`/run/knot/knot.sock`) and write access to the zones
directory. A hardened systemd unit is in
[`deploy/knot-api.service`](deploy/knot-api.service); put the environment
file at `/etc/knot-api.env` with mode `0600`.

Bind to localhost (or put a TLS-terminating reverse proxy in front) - the
bearer token is plaintext on the wire.

## API

All endpoints except `GET /v1/healthz` require `Authorization: Bearer <token>`.
Zone names are normalized (case, trailing dot); IDNs must be punycode.

```console
# create (201) or update (200)
$ curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d '{"zonefile": "example.com. 3600 SOA ns1.example.com. hostmaster.example.com. 1 86400 900 691200 3600\nexample.com. 3600 NS ns1.example.com.\nns1.example.com. 3600 A 192.0.2.1\n"}' \
      http://127.0.0.1:8080/v1/zones/example.com
{"name":"example.com","serial":"1751500000","knot":{...},"created":true}

$ curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/zones
$ curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/zones/example.com
$ curl -X DELETE -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/v1/zones/example.com
```

| Status | Meaning |
|---|---|
| `401` | missing/invalid token |
| `403` | protected zone (catalog zone, `KNOT_API_PROTECTED_ZONES`) |
| `404` | zone not configured |
| `413` | zonefile exceeds `KNOT_API_MAX_ZONEFILE_BYTES` |
| `422` | invalid zone name or zonefile (`detail` carries the kzonecheck output) |
| `500` | knotd rejected an operation (`detail` carries the daemon error) |
| `503` | knotd unreachable, or a foreign config transaction stayed open (`Retry-After: 30`) |

Interactive OpenAPI docs: `http://127.0.0.1:8080/docs`.

## Operations notes

- **Stale transaction**: if a config transaction is ever left open (admin
  `knotc conf-begin` without commit, crashed tooling), all writes answer
  `503`. Remediate with `knotc conf-abort`, or set
  `KNOT_API_ABORT_STALE_TXN_ON_STARTUP=true` on single-operator hosts.
- **Partial delete**: delete is `conf-unset` → purge → unlink. If the purge
  fails after the unset, the API answers `500` with the manual remediation
  (`knotc zone-purge -f <zone>. +orphan`); a repeated DELETE returns `404`
  because the zone is already deconfigured.
- The response `serial` can be `null` right after a create of a very large
  zone (the API polls ~5 s for the initial load); check `GET /v1/zones/<zone>`.

## Development

```console
$ .venv/bin/pip install -e . pytest httpx ruff mypy
$ .venv/bin/pytest          # unit tests, no Knot needed (FakeKnot + fake kzonecheck)
$ .venv/bin/ruff check src tests
$ .venv/bin/mypy
```

### End-to-end harness

`deploy/docker-compose.yml` runs knotd 3.5 (confdb bootstrapped from
`deploy/knot.conf`, catalog zone `catalog.`) plus the API, sharing the
control socket and zones directory:

```console
$ docker compose -f deploy/docker-compose.yml up --build -d
$ curl -s -H 'Authorization: Bearer e2e-secret' http://127.0.0.1:8080/v1/zones
$ KNOT_API_E2E=1 .venv/bin/pytest -m integration
$ docker compose -f deploy/docker-compose.yml down -v
```
