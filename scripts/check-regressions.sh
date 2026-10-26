#!/usr/bin/env bash
# Reusable local/CI gate. No deployment, commits, model calls or public ports.
set -Eeuo pipefail

task_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---local}"
if [[ "$mode" != "--local" && "$mode" != "--containers" ]]; then
  echo "Usage: bash scripts/check-regressions.sh [--local|--containers]" >&2
  exit 2
fi

cd "$task_repo_root"
node scripts/sync-visualization-contract.mjs --check
cd "$task_repo_root/frontend"
npm run type-check
npm test
npm run build
npm run test:browser
cd "$task_repo_root/plugin-host"
npm test
cd "$task_repo_root"
./run.sh config --quiet
python3 -m unittest discover -s deploy/production -v

if [[ "$mode" == "--containers" ]]; then
  # Existing images only. Test dependencies live inside disposable containers;
  # repository mounts are read-only, and tests use their own fixtures.
  # Test containers must not inherit production volumes, credentials or service
  # endpoints. Direct disposable runs also work after a release owns the stack.
  docker run --rm --entrypoint sh \
    --label com.docker.compose.project=datalab-regression --label com.docker.compose.service=backend-tests \
    -v "$task_repo_root:/workspace:ro" -w /workspace/backend \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e API_KEY=test-key -e API_BASE=http://127.0.0.1:9 \
    -e MONGODB_URI=mongodb://127.0.0.1:9 -e REDIS_HOST=127.0.0.1 -e REDIS_PORT=9 \
    -e TOOL_PLUGINS_DIR=/workspace/tools \
    -e PLUGIN_RUNTIME_TOOLS_DIR=/workspace/tools \
    -e PLUGIN_RUNTIME_EXECUTION_CONTRACT_DIR=/workspace/sandbox \
    "${DATALAB_TEST_BACKEND_IMAGE:-datalab-backend:latest}" -c 'uv pip install --python /opt/venv/bin/python --index-url https://pypi.org/simple pytest pytest-asyncio pytest-mock requests && uv run --no-sync python scripts/check_persistence_contracts.py --check && uv run --no-sync pytest -o addopts= -o log_cli=false -p no:cacheprovider -q -ra'
  docker run --rm --entrypoint sh \
    --label com.docker.compose.project=datalab-regression --label com.docker.compose.service=sandbox-tests \
    -v "$task_repo_root:/workspace:ro" -w /workspace/sandbox \
    -e PYTHONDONTWRITEBYTECODE=1 -e UV_PROJECT_ENVIRONMENT=/app/.venv \
    -e AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1 \
    "${DATALAB_TEST_SANDBOX_IMAGE:-datalab-sandbox:latest}" -c 'uv pip install --python /app/.venv/bin/python --index-url https://pypi.org/simple pytest pytest-asyncio pytest-mock httpx requests pyteomics psims rosettasciio && uv run --no-sync pytest -o addopts= -o log_cli=false -p no:cacheprovider -q -ra && cd /workspace && DATALAB_BENCHMARK_DATASETS=/workspace/backend/app/resources/datasets /app/.venv/bin/python -m unittest discover -s benchmarks/dataseek/tests -p "test_*.py"'
else
  cd "$task_repo_root/backend"
  uv run python scripts/check_persistence_contracts.py --check
  uv run pytest
  cd "$task_repo_root/sandbox"
  AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK=1 uv run pytest
  cd "$task_repo_root"
  python3 -m unittest discover -s benchmarks/dataseek/tests -p 'test_*.py'
fi
