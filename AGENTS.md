# DataLab contributor guide

DataLab is based on the datalab branch of AI-DATASEEK and is intentionally
scoped to dataset exploration and analysis. Do not
reintroduce removed product domains (Claw, A2A, scientific-site publishing,
knowledge-base indexing, or broad platform administration) without an explicit
architecture decision.

## Git workflow

Preserve the branch explicitly selected by the user. The inherited source
branch for this workspace is `datalab`; use it unless the user specifies
another branch. Commit and push only when requested by the user; use Chinese
commit messages. A code change does not authorize a deployment.
Preserve unrelated working-tree edits; do not include credentials in commits or
container build contexts. Publishing this independent project does not request
an update to an existing DataSeek deployment.

## Services

| Service | Stack | Purpose |
|---|---|---|
| Frontend | Vue 3 + TypeScript + Vite | Dataset and general analysis UI |
| Backend | FastAPI + Beanie + Redis | APIs, Agent execution, plugins, administration |
| Sandbox | FastAPI + Chromium + VNC | Isolated analysis runtime |

## Deployment

DataLab uses `docker-compose.yml`, Compose project `datalab`, resource prefix
`datalab`, and application images named `datalab-*`. The frontend defaults to
`http://localhost:10030`; `.env` may override its host and port. The template in
`deploy/datalab/compose.env.example` uses the same isolated defaults. Keep
DataLab networks and volumes separate from existing DataSeek deployments.

Use `./run.sh` for requested container updates. Do not install, start, rebuild,
or redeploy services merely because a change has been completed. Preserve
internal database and bucket identifiers for compatibility unless a data
migration is explicitly requested.

## Required checks

```bash
cd frontend && npm run type-check && npm run build
cd ../backend && uv run pytest
cd ../sandbox && uv run pytest
docker compose config --quiet
```

Preserve the read-only dataset mount boundary. Host paths must remain validated
against `DATASET_HOST_PATH_ALLOWLIST`, and real paths must never be returned to
the browser or persisted in URL/localStorage/sessionStorage.
