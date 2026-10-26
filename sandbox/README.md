# DataLab Sandbox

DataLab's analysis runtime provides shell execution, file operations, scientific readers, Chromium, and a VNC desktop inside containers managed by the backend. Start with the [project introduction](../README.md) or [中文说明](../README_zh.md), then the [deployment](../docs/deployment.md) and [development](../docs/development.md) guides.

## Runtime responsibilities

- Execute analysis programs and expose status, output, cancellation, and private execution evidence.
- Read selected inputs through read-only object-backed mounts and write computation files to separate workspaces.
- Support file reads, writes, search, and version-checked atomic patching for eligible writable files.
- Provide scientific readers and analysis packages for tabular, array, spatial, biological, and other supported data.
- Render actual output artifacts for byte-identity checks and durable backend delivery; content and scientific checks have their own explicit scope.
- Run Chromium, VNC, and supporting processes under Supervisor.

The `sandbox-image` Compose service prepares the image and exits successfully. The backend creates actual per-session analysis containers. A new image is used by new containers; existing analysis containers are not replaced in place during an update.

## Input isolation and dependency preparation

Only selected, authorized input files may be exposed. Inputs remain read-only and object-backed; the task workspace is writable and separate. Do not download entire input objects into session caches, create a shared full-file input cache, or copy files as a fallback when mounting is unavailable. The mount helper needs Linux FUSE and shared mount propagation, and receives scoped read grants instead of MinIO credentials.

The image provides a substantial preinstalled scientific stack, specified by [`pyproject.toml`](pyproject.toml), [`uv.lock`](uv.lock), and [`Dockerfile`](Dockerfile). Runtime capability catalogs report observed package and module availability; they are not exhaustive permission lists. Missing dependencies may be prepared inside the task's isolated environment when required, while preserving authorized data access, input immutability, and execution isolation. Network restrictions or unavailable packages must be reported accurately.

Program preflight reports Python/package versions and import resolution. Missing unconditional imports can prevent launch before program execution; conditional and lazy imports still require runtime handling. Static dependency checks do not execute every package initializer or establish scientific validity.

## Layout

```text
sandbox/
├── app/
│   ├── api/          Shell, file and Supervisor routes
│   ├── services/     Execution, file, parsing and preview services
│   ├── schemas/      Request and response contracts
│   ├── models/       Runtime models
│   ├── core/         Configuration
│   └── main.py       FastAPI application
├── scripts/          Scientific helpers, validation and runtime tooling
├── scientific_operators/  Reusable scientific operators
├── tests/            Unit, contract and runtime tests
├── Dockerfile        Full Linux runtime image; build context is repository root
├── pyproject.toml    Python dependencies
└── supervisord.conf  Process configuration
```

## Development and image builds

The Python project requires Python 3.10+ and `uv`. Some dependencies and readers require Linux system packages; a local API process is not equivalent to the full image.

```bash
# From sandbox/
uv sync --dev
uv run pytest
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

To build the sandbox image for a new development installation, run from the repository root:

```bash
docker system df
./run.sh build sandbox-image
```

Existing production releases use the controlled deployment procedure. Reuse an unchanged sandbox image, and never remove data volumes to free build space. If building directly for development, the Dockerfile requires the repository-root context:

```bash
docker build -f sandbox/Dockerfile -t datalab-sandbox:latest .
```

The backend supplies mounts, execution configuration, and the intended network. Standalone containers without that setup cannot verify the full analysis lifecycle. Do not expose shell, browser-debugging, or VNC ports directly to untrusted networks.

## Service interfaces

Internal container ports are `8080` (FastAPI), `9222` (Chromium debugging), `5900` (VNC), and `5901` (VNC WebSocket). These are runtime interfaces, not public application entry points.

The API prefix is `/api/v1`; routes are registered in [`app/api/router.py`](app/api/router.py):

| Group | Operations |
| --- | --- |
| `/shell` | Start execution, observe output, wait, send input, and cancel processes. |
| `/file` | Read, write, patch, replace, search, and find eligible files. |
| `/supervisor` | Observe and manage the internal service lifecycle and optional timeout. |

Use the local development server's `/docs` or `/openapi.json` for the exact current request/response schemas. Shell wait timeout may return `status: "running"` with `returncode: null`; it does not prove failure and must not trigger command resubmission. A command's confirmed exit and permission to replay partially completed work are separate decisions.

## Explicit analysis and result checks

Saved Python analysis programs use the backend's `program_run` interface with explicit script, working-directory, and argument bindings. The retired `dataset_quicklook` helper has been removed from the current sandbox source and tool catalog. Analysis chooses registered tools or saved programs based on the observed inputs and requested outputs.

The new `python_inspect.py` helper supports the backend's `python_inspect` tool. It batches up to eight relative API names from one module and reports actual package versions, signatures, and bounded documentation. It runs through the tracked task program executor; importing a package can initialize code, but the inspected functions are not invoked. Existing containers without this helper report it unavailable and are not silently rebuilt or made to rerun completed analysis.

Optional result-check helpers are installed alongside the normal scientific packages:

| Module | Contract |
| --- | --- |
| `datalab_analysis` | `RowPartition` checks that accepted and disjoint excluded rows cover the original identities; `aligned_groups` requires aligned row/group identities. `StageCache` reuses only explicitly bound, validated stages. |
| `datalab_statistics` | `nested_ols_f_test`, `linear_contrast`, and `residual_levene` validate their supported inputs and assumptions and return named dictionary results. Unlabeled nested models require both sets of row IDs. |
| `datalab_results` | `ResultBundle` freezes finite JSON-compatible computed values and exports checked JSON, CSV, supported line/scatter plots, and controlled report facts from that snapshot. `verify()` rechecks registered artifacts and facts. |

Inspect the actual helper signature before relying on an uncertain return convention. For example, `RowPartition.exclude()` mutates the audit and returns `None`, `RowPartition.verify()` returns evidence rather than row IDs, and `StageCache.run()` returns a `StageResult` object. Keep retained input identities separately.

The helpers are optional and do not impose a library/API list or force a different statistical method. Export consistency does not prove scientific suitability, arbitrary report prose, or the statistical interpretation of supplied plot bounds. `ResultBundle` does not upload artifacts or copy source inputs. Its outputs still use the ordinary backend delivery path. See [result-check contracts](../docs/analysis-result-contracts.md) and the [release notes](../docs/releases/2026-10-26-analysis-and-skills.md).

## JSON result helper

The image installs `datalab_json` for strict, atomic JSON output:

```python
from datalab_json import write_json, not_estimable

write_json("/home/ubuntu/output/summary.json", {
    "statistics": dataframe.groupby("group").agg(["mean", "count"]).to_dict(),
    "validation_score": not_estimable("No independent validation observations"),
})
```

This example assumes `dataframe` has already been created from an authorized input and the output directory is writable. `write_json` normalizes NumPy/pandas objects and atomically replaces a regular output file. Existing artifacts survive normalization or encoding failures before replacement. DataFrame/Series representations preserve axis labels; non-string mapping keys receive a structured representation to avoid collisions. Pandas missing values become JSON null, while NaN/infinity and unavailable estimates have explicit representations instead of silently becoming zero.

The JSON helper does not bypass backend byte-identity checks or object-storage delivery. See [execution reliability](../docs/changes-2026-10-25-execution-reliability.md) for version-checked patches, stage dependencies, and result-set evidence.

## Configuration

| Variable | Meaning |
| --- | --- |
| `ORIGINS` | CORS origins for the sandbox API. Current default is `["*"]`; network isolation and backend-mediated access still matter. |
| `SERVICE_TIMEOUT_MINUTES` | Optional service timeout; default is unset. This is distinct from analysis completion and cancellation. |
| `LOG_LEVEL` | Logging level, default `INFO`. |

Actual container creation and lifecycle policy is configured by the backend. Do not infer task-wide analysis quotas from an individual tool timeout or the Supervisor settings.

## Verification

Run `uv run pytest` in this directory. For Linux-specific scientific stacks and image behavior, use the container workflow in the [development guide](../docs/development.md) and [`scripts/check-regressions.sh`](../scripts/check-regressions.sh). Test results must identify the actual environment; skipped readers or missing tools are not evidence of successful execution.

Related documents: [analysis recovery](../docs/analysis-runtime-recovery.md), [delivery contract](../docs/analysis-delivery-contract.md), [scientific visualization](../docs/scientific-visualization-integration.md), and [repository contributor rules](../AGENTS.md).
