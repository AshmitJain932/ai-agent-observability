# Local demo with Docker: `docker compose up`

One command starts the whole backend on your machine, with sample traces already in it:
PostgreSQL 16, the collector (ingestion API) and the dashboard. It needs nothing but Docker.

> **For local demos only.** The database password and the dashboard token in `compose.yaml` are
> well-known demo values, and the image is alpha software that is not production-hardened (no
> TLS, no rate limiting, no retention job). Run it on your own machine, not on a server.

## Start it

**Prerequisites:** Docker with the Compose plugin (`docker compose version`), for example Docker
Desktop, or Docker Engine 25 or newer. From the repository root:

```bash
docker compose up
```

The first run builds the `aobs-server:local` image, which takes a minute or two. After that, the
stack is up in about 20 seconds. Add `-d --wait` to run it in the background and return once
everything is ready.

**Open <http://localhost:8800/#token=aobs-demo-token>**, then choose **Stored traces** in the
*Dataset* menu at the top right.

What happens, in order:

| Service | What it does |
|---|---|
| `postgres` | PostgreSQL 16. The data lives in the `pgdata` volume. Its port is not published on your machine. |
| `migrate` | Runs `aobs-server db upgrade` once, then exits. |
| `collector` | `aobs-server serve`: the ingestion API on <http://127.0.0.1:8000> (`POST /v1/ingest`, `GET /readyz`). |
| `seed` | Runs `aobs-server demo seed` once, then exits (below). |
| `dashboard` | `aobs-server dashboard` on <http://127.0.0.1:8800>: the sample files, and the demo project's stored traces. |

**The seed step** creates the organization `aobs-demo` and the project `demo`. The project stores
metadata only, like `project create`'s default. Then it sends the sample recordings to the
collector's `POST /v1/ingest`, the same path an SDK uses: 21 batches, 73 runs. The recordings are
the ones in `examples/diagnose/`, recorded with the SDK, and include healthy, slow, flaky, failing
and crashed runs.

- **It uses a temporary ingest key.** The key is never printed, and it is revoked before the step
  exits.
- **It runs only once.** On the next `docker compose up` it finds every sample trace already
  stored and sends nothing. If an earlier run stopped halfway, it sends the rest; traces already
  stored are counted as duplicates, not stored twice.
- **The dashboard shows the same recordings twice:** as the three *Sample* datasets, read from
  files, and as *Stored traces*, read from PostgreSQL. Only the stored traces have been through
  the collector.
- **Start times are when the samples were recorded**, not when you started the demo.

`docker compose logs seed` shows what the step did, with the organization and project ids.

## See it working

```bash
curl http://127.0.0.1:8000/readyz                     # {"status":"ready"}
docker compose ps                                     # collector and dashboard: healthy; migrate and seed: exited (0)
docker compose exec collector aobs-server demo project-id
docker compose exec collector aobs-server traces list --project <project id> --limit 5
docker compose exec collector aobs-server analyze --project <project id> 'db:status=error&limit=1'
```

Every `aobs-server` command works this way inside the `collector` container; the database URL is
already set there. See [stored-analysis.md](stored-analysis.md) for `analyze`, `compare` and `gate`
on stored traces.

## Send your own agent's traces

**1. Create an ingest key** for the demo project. `key create` prints the key once (`api_key`,
starting with `aobs_ik_`); only its hash is stored:

```bash
docker compose exec collector aobs-server demo project-id          # prints the project id
docker compose exec collector aobs-server key create --project <project id> --name my-agent
```

**2. Point the SDK at the collector**, <http://127.0.0.1:8000>:

```python
import aobs

aobs.init(service_name="my-agent", environment="dev", endpoint="http://127.0.0.1:8000", api_key="aobs_ik_…")
```

Or set `AOBS_ENDPOINT=http://127.0.0.1:8000` and `AOBS_API_KEY`. To try it with the example agent
(it needs `uv sync` in this repository; no model API key):

```bash
AOBS_API_KEY=aobs_ik_… uv run python examples/support_agent/run.py --scenario refund-outage --endpoint http://127.0.0.1:8000
```

**3. Reload the dashboard.** New runs appear at the top of *Stored traces*. The dashboard shows the
200 most recently started traces of the project.

If your agent runs in a container on the same Compose network, use `http://collector:8000`
instead. Behind a proxy, add `127.0.0.1` and `localhost` to `NO_PROXY`. Everything else about
ingestion is in [ingestion.md](ingestion.md), and the SDK in [sdk.md](sdk.md).

## Stop and reset

```bash
docker compose down        # stop; the stored traces stay in the pgdata volume
docker compose down -v     # stop and delete the volume: every stored trace, key and project
docker image rm aobs-server:local   # also remove the image
```

After `down -v`, the next `docker compose up` starts from an empty database and seeds it again.
After changing the server's code, rebuild with `docker compose up --build`.

## Settings

Set these in your shell or in a `.env` file next to `compose.yaml`:

| Variable | Default | What it sets |
|---|---|---|
| `AOBS_DEMO_DB_PASSWORD` | `aobs-demo-password` | The PostgreSQL password. Demo only. It is applied when the `pgdata` volume is created, so after changing it run `docker compose down -v`. |
| `AOBS_DEMO_DASHBOARD_TOKEN` | `aobs-demo-token` | The dashboard token, which protects *Stored traces*. Demo only. |
| `AOBS_DEMO_BIND` | `127.0.0.1` | The host address the ports are published on. Another value, such as `0.0.0.0`, makes the collector and the dashboard reachable from other machines: change the password and the token first. |
| `AOBS_DEMO_COLLECTOR_PORT` | `8000` | The collector's port on your machine. |
| `AOBS_DEMO_DASHBOARD_PORT` | `8800` | The dashboard's port on your machine. |
| `AOBS_DEMO_IMAGE` | `aobs-server:local` | The image name to build and run. |
| `AOBS_DEMO_PYTHON_IMAGE` | `python:3.13-slim` | The base image to build on, for example a mirror or one with your network's CA certificate. |

The dashboard logs `listening on 0.0.0.0, reachable from other machines`. That is inside its
container; on your machine, the port is published on `AOBS_DEMO_BIND` only.

## The image

`Dockerfile` builds one image for every `aobs-server` command (`docker build -t aobs-server:local .`):

- It has two stages. The first installs the locked dependencies (`uv sync --locked --no-dev`) and
  aobs-server itself, not editable, into a virtual environment. The second copies only that
  environment onto `python:3.13-slim`.
- It runs as the unprivileged user `aobs` (uid 10001). `compose.yaml` also makes the containers'
  file systems read-only and drops all Linux capabilities.
- No secret is baked in. The database URL and the dashboard token come from the environment at
  run time.
- Only the workspace metadata, `uv.lock` and `server/` are sent to the build (`.dockerignore`), so
  local trace files and `.env` files stay out of it.
- The sample recordings are installed with the package (`aobs_server/dashboard/samples`), so the
  dashboard's sample datasets work without a clone of the repository.

## What was tested

- `server/tests/test_demo.py` runs `aobs-server demo seed` against a collector on a local port and
  a real PostgreSQL 16. It covers the first seed, seeding twice, finishing a partial seed and an
  unreachable collector. It checks that the key is revoked and never printed, and that the
  dashboard shows the 73 runs.
- `.github/workflows/docker.yml` builds the image on pull requests that touch it, starts this stack
  and checks the collector and the seeded dashboard with `curl`.
- The stack was also run by hand with Docker Engine 29 and Compose 5: starting from an empty
  volume, starting again (nothing re-sent), sending a run of the example agent from the host, the
  dashboard in a browser, and `down -v`. The base image was rebuilt with an extra CA certificate
  for that network (`AOBS_DEMO_PYTHON_IMAGE`). Docker Desktop on macOS or Windows was not tried.
