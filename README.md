# paylab-sut

The system under test for the PayLab QA portfolio project: a small payment checkout
service with a web UI, a mock payment provider, and twelve seeded defects that can be
switched on one at a time. The test suite lives in a separate repo, and this repo
produces what it tests.

> **Private repo.** Testers get the published images, `compose.consumer.yml` and
> [SUT_GUIDE.md](SUT_GUIDE.md), not this repository. [DEFECTS.md](DEFECTS.md) and
> `scripts/verify_defects.py` contain the answers.

## Documents

| File | Audience | Contents |
|------|----------|----------|
| [SUT_GUIDE.md](SUT_GUIDE.md) | tester | how to run it, seeded data, API rules, provider fault modes, test hooks |
| [DEFECTS.md](DEFECTS.md) | maintainer only | each defect: trigger, symptom, test layer, code location |
| this README | maintainer | development, verification, releases |

## Quick start

Requirements: Docker with Compose v2. For development you also need
[uv](https://docs.astral.sh/uv/).

```bash
make up              # build and start; returns once both services are healthy
open http://localhost:8000        # web UI   (API docs: /docs)
make down            # stop and delete the database volume
```

To switch defects on: `BUGS=B03 make up` (comma-separate for more than one).

| Service  | Port | Image |
|----------|------|-------|
| checkout (API + UI) | 8000 | `ghcr.io/<owner>/paylab-checkout` |
| provider (mock)     | 8001 | `ghcr.io/<owner>/paylab-provider` |

## Layout

```
app/
  checkout/        FastAPI checkout service (python -m app.checkout)
    routers/       one module per API area; testhooks only mounted when TEST_MODE=1
    bugs.py        bug_enabled("Bxx"), parsed once from BUGS at startup
    clock.py       the only time source (FAKE_NOW, /__test/clock)
  provider/        mock payment provider (python -m app.provider)
  common/          JSON log formatter shared by both
  web/
    templates/     index.html and app.js (Jinja2-rendered, so UI defects are compiled in)
    static/        style.css
docker/            one Dockerfile per service (python:3.12-slim, non-root uid 10001)
scripts/verify_defects.py   private end-to-end proof of baseline + every defect
```

The stack is Python 3.12, FastAPI, SQLAlchemy 2 on SQLite (in the `checkout-data`
volume), httpx, PyJWT and prometheus_client. The UI is vanilla JS with no build step.

## Development

```bash
uv sync                                   # create .venv with dev tools
make lint                                 # ruff check, ruff format --check, mypy (strict)

# run outside Docker (two terminals)
uv run python -m app.provider
DATABASE_URL=sqlite:///./data/paylab.db PROVIDER_URL=http://127.0.0.1:8001 \
CALLBACK_BASE_URL=http://127.0.0.1:8000 uv run python -m app.checkout
```

Things that are easy to break:

- **Handlers are `async def` and use a sync SQLite session on the event loop.** This is
  deliberate: it keeps writes serialized. Never hold an open write transaction across an
  `await` (commit first), or concurrent requests will deadlock on the SQLite lock.
- **One uvicorn worker only.** The fake clock, in-flight payment registry and metrics are
  process-local.
- **Money is stored as integer cents** (`Money` column type) and serialized as strings.
  Aggregates such as `SUM` over a `Money` column already come back as `Decimal`.
- **The baseline perf budget is GET /orders p95 < 300 ms** with 50 clients. It currently
  runs at about 160 ms. Keep the list endpoint to two queries.

### Changing a defect

Each defect is one small branch behind `bug_enabled("Bxx")` (or `{% if bug("Bxx") %}` in
`app.js`). Rules:

- With `BUGS` empty, the code path must be the specified behaviour.
- One ID changes one behaviour. Don't add comments describing the defect next to the code.
- No ID or description may reach responses, logs, the UI or SUT_GUIDE.md. The
  verify script checks this.
- Update DEFECTS.md and the matching `@symptom` / `affected_by=` entries in the verify script.

## Verification

```bash
make verify                                      # all runs, about 8 minutes
make verify ARGS="--only baseline,B03 --skip-build"
```

For each run, the script restarts checkout with `BUGS` set to that one ID, resets state
before every check, and asserts two things:

- the defect's symptom appears;
- every baseline check not listed as `affected_by` that ID still passes.

B07 and B12 run in headless Chromium (Playwright). A final config run covers
`TEST_MODE=0`, `FAKE_NOW` and unknown `BUGS` entries. The script exits non-zero on any
failure and prints a summary table.

Run it on a quiet machine. The perf check (30 s for the baseline and B09 runs, 10 s for
the others) is sensitive to other heavy workloads on the host.

## Releasing

```bash
git tag v1.0.0 && git push origin v1.0.0
```

[.github/workflows/release.yml](.github/workflows/release.yml) builds both images for
amd64 and arm64 and pushes them to GHCR with tags `1.0.0`, `1.0` and `latest`. It then
attaches a copy of `compose.consumer.yml` to the GitHub release, with the owner and
version filled in.

Images from a private repo are private packages. Either make the two packages public in
GHCR or give the tester read access. The tester then runs:

```bash
PAYLAB_OWNER=<owner> docker compose -f compose.consumer.yml up -d --wait
```

Hand the tester the release's `compose.consumer.yml` and `SUT_GUIDE.md`, and nothing else.
