<p align="center">
  <img src="frontend/public/datalab-logo.png" alt="DataLab logo" width="96" />
</p>

# DataLab

English | [中文](README_zh.md)

DataLab is a conversational data exploration and analysis laboratory. It connects data discovery, file acquisition, reusable Data Skills, isolated analysis, and result delivery in one workflow.

This independent project starts from the [`datalab` branch of AI-DATASEEK](https://github.com/km345297256/AI-DATASEEK/tree/datalab) at commit [`5efc7d6`](https://github.com/km345297256/AI-DATASEEK/commit/5efc7d66dcc3b83c40aee47813d422fd3c2701f0), whose earlier lineage includes AI-MANUS. The DataLab repository is [km345297256/DataLab](https://github.com/km345297256/DataLab), published as a clean initial snapshot without the upstream Git history. Original license and attribution notices are retained; historical documentation and internal storage identifiers may still use DataSeek names.

## Included capabilities

- Versioned Data Skill packages, workflow categories, explicit task selection, and a reusable CSV quality-check example. See [Data Skills](data-skills/README.md).
- A common data-connector contract, with NCBI Datasets, GEO, and SRA discovery and bounded file downloads. See [connector capabilities and limits](docs/data-connector-standard.md).
- Selectable search results, file import into the task sandbox, and a handoff to analysis with the selected files.
- Run history, actual skill-version receipts, source references, and file-content digests. Run history supports inspection; automatic execution recovery is still future work.
- Third-party temporary dataset submission API at `POST /api/v1/datasets/submissions`.
- Scientific dataset exploration at `/dataset/seek/:datasetId`, with recursive file discovery, model-generated analysis questions, and read-only dataset mounts.
- General data-exploration tasks (`/chat` and `/chat/:sessionId`).
- Skill, MCP, and Renderer plugins.
- System administration limited to resource usage and configuration, tasks, MCP, and skills.
- Per-session Docker sandboxes, task history, file results, and previews.
- Token usage accounting for operations and capacity analysis, without per-user quotas.
- A unified model driver, bounded request context and execution timeouts, and metadata-only context-compaction traces. See [model driver and context budget](docs/model-driver-and-context-budget.md).
- Domain presets with on-demand Cordis tool loading, plus opt-in restricted Code Mode and bounded domain SubAgents. See [presets and experimental execution modes](docs/domain-presets-and-code-mode.md).

The transitive runtime modules required by these capabilities are intentionally retained: Agent execution, session events, safety review, auditing, permissions, file storage, MongoDB, Redis, and the sandbox runtime. See [docs/architecture-scope.md](docs/architecture-scope.md) for the exact boundary, [docs/cordis-plugin-architecture.md](docs/cordis-plugin-architecture.md) for the analysis-tool plugin boundary, and [docs/spill-artifact-store.md](docs/spill-artifact-store.md) for oversized tool-result retention.

## Runtime topology

```text
Browser -> Frontend -> Backend -> MongoDB / Redis / MinIO
                            \
                             -> per-session Docker sandbox
                                  -> dataset directory (read-only)
```

DataLab uses `docker-compose.yml` with Compose project and resource prefix `datalab`. The default frontend port is `10030`; application images use the `datalab-*` prefix. Networks and volumes are isolated from an existing DataSeek deployment. Internal database and bucket identifiers remain compatible with the inherited code.

## Quick start

Requirements: Docker 20.10+ and Docker Compose.

```bash
cp .env.example .env
# Set API_KEY, API_BASE, MODEL_NAME, and a strong MINIO_SECRET_KEY.
# Set SERVER_HOST to the URL used by your browser.
./run.sh up -d --build
```

Open `http://localhost:10030`.

DataLab has a fixed no-login access model. Browser and API requests do not
use Bearer tokens or `X-API-Key`; every caller operates as the same built-in
system administrator. Token consumption is recorded for usage reporting only
and never checked against or deducted from a user quota.
Model-runtime limits bound each request's estimated context and execution
timeouts. There is no cumulative task token or call-count quota.

For a Snap-packaged Docker daemon, expose host dataset paths through:

```env
DATASET_DOCKER_HOST_ROOT=/var/lib/snapd/hostfs
```

Allowed dataset roots are controlled by `DATASET_HOST_PATH_ALLOWLIST`. Submitted server directories are validated and mounted read-only into the task sandbox.

## Development and updates

Use the supported Compose stack for containerized runs and updates:

```bash
./run.sh up -d --build
```

The optional deployment override template is [deploy/datalab/compose.env.example](deploy/datalab/compose.env.example). Keep the project name, resource prefix, image names, and frontend URL consistent when overriding defaults. Renaming the project does not migrate or modify an existing deployment's data.

Useful checks:

```bash
cd frontend && npm ci && npm run type-check && npm run build
cd ../backend && uv sync && uv run pytest
cd ../sandbox && uv sync && uv run pytest
docker compose config --quiet
```

## Third-party integration

Users can explore datasets through `/datasets` and register data through `/dataset/setup`. Third-party systems can separately call the temporary dataset submission API and redirect users to `/dataset/seek/:datasetId`. See [the third-party API guide](docs/dataset-seek-third-party-api.md).

## Project layout

```text
DataLab/
├── frontend/   Vue 3 data exploration UI
├── backend/    FastAPI APIs, Agent execution, plugins, and administration
├── sandbox/    isolated scientific analysis environment
├── data-skills/ versioned skill packages, samples, and contribution guidance
├── data-connectors/ connector contracts and manifests
├── docs/       integration and architecture documentation
└── docker-compose.yml
```

## Security notes

- DataLab performs no application-level caller authentication. Deploy it only on a trusted network.
- Before exposing it beyond a trusted network, put it behind network-level access control such as a VPN, firewall allowlist, or authenticated reverse proxy/API gateway, and terminate HTTPS there.
- Anyone who can reach the service has the shared system-administrator authority; there is no per-caller isolation or attribution.
- Never put model keys or real host paths in URLs or browser storage.
- Restrict `DATASET_HOST_PATH_ALLOWLIST` to dedicated dataset roots.
- Dataset host paths remain validated against that allowlist and are mounted into analysis sandboxes read-only; real paths must not be returned to the browser or persisted in URL, `localStorage`, or `sessionStorage`.
- Mounting the Docker socket grants the backend control over local containers; deploy it only on a trusted host.
- Replace every placeholder secret in `.env.example` before production use.

## License

See [LICENSE](LICENSE).
