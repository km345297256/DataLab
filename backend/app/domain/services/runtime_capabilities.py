"""Minimal, path-free installed runtime facts for planning and code authoring."""
from __future__ import annotations

import asyncio
import re

from app.domain.models.tool_result import ToolResult

_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)
_DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z", re.ASCII)
_VERSION = re.compile(r"[0-9][A-Za-z0-9.+!_-]{0,63}\Z")


def runtime_capabilities_prompt(data: object) -> str:
    """Render observed packages with format validation, without a package-name list."""
    lines = []
    if isinstance(data, dict) and data.get("version") == 1 and data.get("check") == "runtime_catalog" and data.get("state") == "observed":
        python = data.get("python_version")
        if isinstance(python, str) and _VERSION.fullmatch(python):
            lines.append(f"Python {python}")
        packages = data.get("packages")
        if isinstance(packages, list) and len(packages) <= 32:
            seen = set()
            for item in packages:
                if not isinstance(item, dict):
                    continue
                module, distribution = item.get("module"), item.get("distribution")
                if (not isinstance(module, str) or not _MODULE.fullmatch(module) or module in seen
                        or not isinstance(distribution, str) or not _DISTRIBUTION.fullmatch(distribution)):
                    continue
                version, available = item.get("version"), item.get("available")
                if type(available) is not bool:
                    continue
                if version is not None and (not isinstance(version, str) or not _VERSION.fullmatch(version)):
                    continue
                seen.add(module)
                state = f"available, version {version or 'unknown'}" if available else "unavailable"
                lines.append(f"- {module}: {state}")
        helpers = data.get("analysis_helpers")
        if isinstance(helpers, dict) and helpers.get("datalab_analysis") is True:
            lines.append("- datalab_analysis: available; RowPartition for row conservation, "
                         "aligned_groups for index alignment, StageCache for validated derived-stage reuse")
        if isinstance(helpers, dict) and helpers.get("datalab_statistics") is True:
            lines.append("- datalab_statistics: available (optional); executable checks for nested OLS F tests, "
                         "linear contrasts and grouped model-residual Levene tests. Inspect the chosen "
                         "function's actual signature and supported assumptions with python_inspect. "
                         "These functions return plain dicts (for example result['F'], not result.F). "
                         "Preserve original pandas row indices when fitting; unlabeled nested models "
                         "require both row_ids and restricted_row_ids explicitly")
        if isinstance(helpers, dict) and helpers.get("datalab_results") is True:
            lines.append("- datalab_results: available (optional); frozen computed results shared by tables, "
                         "supported plots and factual report fragments, with actual export checks. "
                         "Inspect its API with python_inspect; export consistency does not prove method validity")
        if isinstance(helpers, dict) and any(helpers.get(name) is True for name in ("datalab_statistics", "datalab_results")):
            lines.append("Optional helpers do not replace a simple direct calculation with explicit checks. "
                         "Use them only when their documented contract fits the work; do not add a framework or extra probe solely because it is installed.")
    facts = "\n".join(lines) if lines else "Installed runtime capabilities could not be confirmed. Do not assume any package is available."
    return ("<analysis_runtime>\n" + facts + "\n"
            "These are installation/import-resolution observations, not validation of scientific results. "
            "All libraries and APIs are allowed; this catalog is neither exhaustive nor an allowlist or denylist. "
            "When available, use python_inspect to batch uncertain API names and inspect installed signatures "
            "and return documentation before coding; reuse the observations. Use a small API smoke check "
            "with known expected behavior when interface metadata cannot establish compatibility. "
            "A missing library is an environment issue: prepare the required dependency in the task's isolated "
            "environment, within the authorized scope, and verify actual availability. Do not silently replace "
            "a requested established statistical method with hand-written formulas. If preparation cannot succeed, "
            "report the actual gap. Prefer a verified equivalent with the same assumptions and outputs only when "
            "appropriate; if equivalence is uncertain, explain the gap and ask the user before changing the method. "
            "Program preflight still validates actual script dependencies. "
            "Unavailable environment metadata is not evidence that the user's data is invalid.\n</analysis_runtime>")


async def inspect_runtime_capabilities(sandbox: object) -> str:
    probe = getattr(sandbox, "runtime_capabilities", None)
    try:
        # This bounds a metadata request only, never analysis execution/tokens.
        result = await asyncio.wait_for(probe(), timeout=15) if callable(probe) else None
        data = result.data if isinstance(result, ToolResult) and result.success is True else None
    except Exception:
        data = None
    return runtime_capabilities_prompt(data)
