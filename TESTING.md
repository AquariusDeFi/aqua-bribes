# Testing aqua-bribes

Tests and lint live in `scripts/`; both are designed to run inside the project's
Docker image so the Postgres + Pipfile environment matches CI.

## Full suite (lint + imports + tests) — CI parity

```sh
docker compose -f docker/docker-compose.test.yml -p aquabribes_test \
  up --build --abort-on-container-exit --exit-code-from backend
docker compose -f docker/docker-compose.test.yml -p aquabribes_test down -v
```

This runs `scripts/runtests.sh`:

1. `manage.py check`
2. `manage.py makemigrations --dry-run --check`
3. `flake8 .`
4. `isort . --check-only --rr`
5. `manage.py test --noinput --keepdb`

Flake8/isort will fail if any pre-existing style violations sit in files you
did not touch; see the "tests-only" flow below for dev iteration.

## Tests only (skip lint / imports) — dev and audit verification

```sh
# one-time — leave db up for the session
docker compose -f docker/docker-compose.test.yml -p aquabribes_test up -d db

# run the whole suite
docker compose -f docker/docker-compose.test.yml -p aquabribes_test run --rm backend \
  bash -c "/code/scripts/wait-for-it.sh db:5432 -t 60 && /code/scripts/run_django_tests.sh"

# run a specific TestCase / module / method
docker compose -f docker/docker-compose.test.yml -p aquabribes_test run --rm backend \
  bash -c "/code/scripts/wait-for-it.sh db:5432 -t 60 && /code/scripts/run_django_tests.sh \
  aquarius_bribes.rewards.tests.ReconcileAndMonitoringTests -v 2"

# shut down when done
docker compose -f docker/docker-compose.test.yml -p aquabribes_test down -v
```

`run_django_tests.sh` wraps `manage.py check` + `makemigrations --dry-run --check`
+ `manage.py test --noinput --keepdb "$@"` — forwards any extra args.

`--keepdb` persists the test database across runs, so the second invocation
skips schema setup and is noticeably faster.

## Notes

- `-p aquabribes_test` pins a dedicated Compose project name; use it in every
  command so `down -v` only removes this project's volumes.
- `depends_on: [db]` is declared in `docker-compose.test.yml`, so `compose run`
  auto-starts the db service if it is not already up.
- Tests write to `postgres://test:test@db/test` via `DATABASE_URL` injected by
  the compose file; no local Postgres needed.
