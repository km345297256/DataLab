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
commit messages. The user has authorized deployment after completed changes
pass the required checks; commit and push still require a separate request.
Preserve unrelated working-tree edits; do not include credentials in commits or
container build contexts. Publishing this independent project does not request
an update to an existing DataSeek deployment.

## Services

| Service | Stack | Purpose |
|---|---|---|
| Frontend | Vue 3 + TypeScript + Vite | Dataset and general analysis UI |
| Backend | FastAPI + Beanie + Redis | APIs, Agent execution, plugins, administration |
| Sandbox | FastAPI + Chromium + VNC | Isolated analysis runtime |

## Dependencies and APIs

Do not impose admission or denial lists on library names, module exports,
APIs, or attribute names. Runtime capability catalogs are observations, not
exhaustive lists of permitted dependencies. Missing dependencies may be prepared
inside the task's isolated environment. Preserve read-only inputs, authorized
data access, and execution isolation when preparing or using dependencies.

## Data storage and analysis inputs

Object storage is the canonical store for downloaded data, user uploads, and
delivered analysis results. The `hybrid` file provider writes new files to MinIO
and retains compatibility reads for older GridFS objects; the legacy `gridfs`
configuration value is an alias for this behavior. Selecting a file or
saving it in the personal data library records an owned file reference; it must
not duplicate the underlying object.

Analysis inputs must be exposed to sandboxes and Jupyter through read-only
object-backed mounts. Do not download entire inputs into per-session files,
create a shared full-file input cache, or fall back to file copying when a mount
is unavailable. Object reads may transfer requested byte ranges using bounded
buffers. Only explicitly selected files may be visible; keep bucket keys,
storage credentials, and host mount paths private to the backend.

Keep writable computation space separate from mounted inputs. Deliver results
through durable object-storage file references; never republish acquired source
files as generated analysis artifacts. Ingestion may retain temporary resumable
transfer staging until object commit; computation may create temporary working
files. Neither is a durable source replica or an analysis input cache. Reclaim
successful ingestion staging rather than mounting it as the source. Preserve
existing registered datasets and ownership for compatibility without adding new
directory-registration entry points or performing an implicit storage migration.
Reject legacy sources that require a new full-file analysis copy instead of
silently creating a compatibility cache.

## Deployment

DataLab uses `docker-compose.yml`, Compose project `datalab`, resource prefix
`datalab`, and application images named `datalab-*`. The frontend defaults to
`http://localhost:10030`; `.env` may override its host and port. The template in
`deploy/datalab/compose.env.example` uses the same isolated defaults. Keep
DataLab networks and volumes separate from existing DataSeek deployments.

After changes pass the required checks, deploy them with `./run.sh` by default,
unless the user explicitly asks to skip deployment. Update only the affected
DataLab services and verify their health and the changed behavior. Preserve
internal database and bucket identifiers for compatibility unless a data
migration is explicitly requested.
Check available Docker disk space before building, and reuse unchanged sandbox
and Jupyter images. Never remove data volumes to make room for a deployment.

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
