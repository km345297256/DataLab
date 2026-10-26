# DataLab Backend

FastAPI backend for dataset discovery, owned file references, conversational analysis, skills, and the administration needed by those workflows.

Start with the [project introduction](../README.md) or [中文说明](../README_zh.md). Detailed setup and operations are in the [deployment guide](../docs/deployment.md); local checks are in the [development guide](../docs/development.md).

## Responsibilities

- Individual password accounts, administrator approval, token validation, and owner-scoped access.
- Analysis sessions, input selection, execution events, results, explicit sharing, and sandbox/Jupyter access.
- Durable public-data search and acquisition, resumable transfer staging, file verification, and personal data references.
- MinIO-backed file delivery and compatibility reads for historical GridFS objects.
- Read-only object-backed input mounts, execution evidence, checkpoints, and analysis job coordination.
- Data Skills, private skill drafts, registered analysis tools, visualization contracts, and MCP integration.
- Usage reporting, resource configuration, task inspection, account approvals, and skill/MCP administration.

The API router is [`app/interfaces/api/routes.py`](app/interfaces/api/routes.py), mounted under `/api/v1` by [`app/main.py`](app/main.py). New directory-registration APIs, Claw, A2A networks, knowledge-base indexing, and scientific-site publishing are outside the product scope.

## Access and data boundaries

DataLab uses `AUTH_PROVIDER=password`. Private APIs validate the authenticated user and resource ownership; the backend does not provide a shared anonymous administrator identity. The initial administrator can be created with `BOOTSTRAP_ADMIN_EMAIL` and a `BOOTSTRAP_ADMIN_PASSWORD` of at least 12 characters. Bootstrap creates a missing account only, and ordinary registrations require administrator approval. Configure a persistent random `JWT_SECRET_KEY` of at least 32 characters before startup.

Inputs are selected by owned file reference and mounted read-only. Object keys, credentials, and host mount paths stay private to the backend. Legacy host paths must resolve beneath `DATASET_HOST_PATH_ALLOWLIST`; they must not appear in browser responses, URLs, or browser storage. Unsupported legacy inputs fail explicitly instead of creating a full-file compatibility cache.

The default `hybrid` provider writes new files to MinIO and can read historical GridFS objects. Saving a personal library entry records a reference without duplicating the object. Generated results use durable object references; source downloads are not relabeled as generated artifacts.

## Analysis execution and skill creation contracts

Saved Python analysis runs through `program_run`, preserving the underlying program's exit status and tracked execution identity. `python_inspect` uses the same task environment to observe installed module versions, signatures, and return documentation for uncertain APIs. It imports the module without calling the selected target functions; imports can have initialization effects. An API observation, a successful process exit, mechanical file delivery, and scientific validity are distinct evidence.

New skill creation requires complete evidence for the latest analysis request and uses related public history and verified source references. It does not skip an incomplete latest request to reuse an older success. Historical method sources must be retained or explicitly accounted for, including superseded or out-of-scope methods and replaced versions within the same turn. Private raw events and execution receipts are not copied into the resulting skill.

New workflow authoring uses `implementation.interface_version=2`; each script declares a versioned `interface_contract` before its implementation is generated. It covers CLI options, input/configuration structure, missing-value behavior, and output bindings. The existing version-1 representation remains readable. Structural checks and independent review compare the implementation with the frozen interface; synthetic verification uses the same public contract.

Review repair is localized only when host-validated target identities establish that the defect lies within existing script implementations. Method, workflow, or interface defects retain the workflow-revision route. Scoped edits do not bypass full package validation, independent review, or supported synthetic execution. Progress distinguishes a received review response, an accepted method, and a required revision.

Privacy contexts are derived separately for each generation stage. Public interface syntax and synthetic bindings receive only narrowly validated roles; ordinary dictionary keys, explanatory prose, or tool output cannot grant privacy exemptions. A source-filename collision can be renamed during initial synthetic-plan authoring only if the independently reviewed interface says every input name is a location binding without analytical meaning. Expected values, scientific data, tolerances, and already frozen plans remain protected.

See [result-check contracts](../docs/analysis-result-contracts.md) and the [2026-10-26 release notes](../docs/releases/2026-10-26-analysis-and-skills.md) for supported scope and evidence. These mechanisms do not establish a general reduction in model errors or task duration.

## Layout

```text
app/
├── application/      Application services and errors
├── domain/           Models, repositories, execution services and tool contracts
├── infrastructure/   Storage, model, sandbox, data-source and repository adapters
├── interfaces/       API routes, schemas, middleware and dependencies
├── core/             Configuration
└── main.py           FastAPI application and service lifecycle
scripts/              Validation and maintenance commands
tests/                Unit, contract and integration tests
```

## Development

The backend requires Python 3.12+ and `uv`:

```bash
# From backend/
uv sync
uv run pytest
```

For manual API development, prepare private configuration and isolated development MongoDB, Redis, and MinIO services first. Export the required settings or place a private `.env` in the process working directory, then run:

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

This command starts the API process only. Full analysis also needs a prepared plugin host, Docker access, analysis/Jupyter images, and supported object mounts. Use the repository Compose stack for the complete system. The backend's `/docs` and `/openapi.json` are available at the development backend address; application routes use `/api/v1`.

Tests have environment-specific prerequisites and isolated fixtures; do not point test configuration at production data or use a real model key for synthetic regression checks. The required project-wide checks also include frontend type checking/build, sandbox tests, and Compose validation.

## Related contracts

- [Architecture scope](../docs/architecture-scope.md)
- [Analysis execution recovery](../docs/analysis-runtime-recovery.md)
- [Result delivery](../docs/analysis-delivery-contract.md)
- [Executable result checks](../docs/analysis-result-contracts.md)
- [2026-10-26 analysis and skills changes](../docs/releases/2026-10-26-analysis-and-skills.md)
- [Model driver and context](../docs/model-driver-and-context-budget.md)
- [MinIO storage](../docs/minio-file-storage.md)
- [Persistence contracts](../docs/persistence/README.md)
