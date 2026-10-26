# Artificial code-authoring regression

`run_analysis_code_real_model.py` is an explicitly invoked acceptance harness,
outside automatic pytest discovery. It calls the backend's configured model
through the production `ExecutionAgent`, model driver, `FileToolkit` and
`ShellToolkit`. Agent memory and usage persistence are in-memory; it does not
create a user session or read application databases. Each case uses a disposable
sandbox with synthetic CSV input mounted read-only. Its test API exposes the
unchanged production shell/file routers and services, as the sandbox test suite
does, without starting unrelated browser or Supervisor services.

Run both variants against **the same base sandbox image**. Source snapshots are
the intervention: the runner overlays application/helper source without changing
installed scientific packages. The backend container supplies configured model
credentials internally; no credentials or `.env` files enter source archives.

```bash
python3 backend/tests/acceptance/run_analysis_code_real_model.py \
  --source-root /absolute/path/to/baseline-snapshot \
  --sandbox-image datalab-sandbox:code-flow-20261026 \
  --variant baseline --output .local/analysis-regression/baseline.json

python3 backend/tests/acceptance/run_analysis_code_real_model.py \
  --source-root /absolute/path/to/candidate-snapshot \
  --sandbox-image datalab-sandbox:code-flow-20261026 \
  --variant candidate --output .local/analysis-regression/candidate.json

python3 backend/tests/acceptance/run_analysis_code_real_model.py \
  --compare .local/analysis-regression/baseline.json .local/analysis-regression/candidate.json \
  --output .local/analysis-regression/comparison.json
```

The fixed cases cover retained-row identity after a missing middle row,
row/group conservation, a nested OLS F test with an independent closed-form
answer, and consistent JSON/CSV/report facts plus an actual PNG file. The oracle
does **not** certify arbitrary prose, scientific method suitability, or plot
pixel semantics. Each outcome explicitly lists its unchecked scope. The
statistical unit is one case execution; a single pass per case is a smoke sample,
not evidence of a population failure-rate reduction. Use `--repeat` equally for
both variants to obtain more observations; stochastic providers can still vary.

The comparison refuses different tasks, fixtures, harness bytes, model route or
configuration, observed provider model names, scientific runtime packages, base
image, repetitions or test deadlines. It also refuses incomplete execution
record windows, missing real model calls, harness errors, changed fixtures, and
incomplete case matrices. Source fingerprints are recorded separately and may
differ by design.

Metrics distinguish preparation failures, confirmed native `program_run`
requests, and changed-source reruns of a previously failed script. Interface
queries are excluded from program success/repair counts. An explicit
`--timeout-seconds` bounds only this artificial experiment; it changes no
production execution policy.

Private artifacts are saved next to each report in `*-artifacts/`: only the
requested synthetic outputs and source snapshots authenticated at the actual
execution boundary. They are not printed to shared stdout. Containers and
temporary fixture mounts are removed after each case. A detached CLI process
does not imply its backend worker has stopped; after external interruption,
inspect the uniquely named artificial worker/container before cleanup.
