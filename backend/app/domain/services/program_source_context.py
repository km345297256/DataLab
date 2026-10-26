"""Bounded diagnostic projections of authenticated immutable execution bytes.

This is an inspection plan, never permission, execution success or data evidence.
The caller authenticates the native operation; actual file reads must still
match the failed line before a correction is allowed.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from app.domain.services.program_execution import validated_source_snapshot

MAX_DIAGNOSTIC_CONTEXT_BYTES = 12_000
MAX_DIAGNOSTIC_WINDOWS = 4
CONTEXT_RADIUS = 3


@dataclass(frozen=True)
class DiagnosticSourceContext:
    source_digest: str
    failed_line: int  # zero based, as in file_read
    failed_line_content: str
    windows: tuple[tuple[int, int], ...]  # zero based, exclusive end
    selected_bytes: int
    related_attributes: tuple[str, ...]


def diagnostic_source_context(snapshot: dict | None, source_digest: str,
                              diagnostic: dict | None) -> DiagnosticSourceContext | None:
    """Select exact failure lines first, then matching attribute expressions.

    Large files do not consume the diagnosis budget: only selected lines do.
    Oversized adjacent lines are excluded, never silently truncated. A single
    failure line larger than this projection is not automatic read authority.
    Source strings/hashes remain private and are not put in public ToolResults.
    """
    validated = validated_source_snapshot(snapshot, source_digest)
    line = diagnostic.get("line") if isinstance(diagnostic, dict) else None
    if validated is None or type(line) is not int:
        return None
    content = validated["content"].replace("\r\n", "\n").replace("\r", "\n")
    lines = content.splitlines()
    if not 1 <= line <= len(lines):
        return None
    failed = line - 1
    sizes = [len(value.encode("utf-8")) + 1 for value in lines]
    if sizes[failed] > MAX_DIAGNOSTIC_CONTEXT_BYTES:
        return None
    selected: set[int] = set()
    windows: list[tuple[int, int]] = []
    selected_bytes = 0

    def add_window(center: int) -> None:
        nonlocal selected_bytes
        if center in selected or selected_bytes + sizes[center] > MAX_DIAGNOSTIC_CONTEXT_BYTES:
            return
        start, end = center, center + 1
        added = {center}
        selected_bytes += sizes[center]
        # Contiguous whole lines only: a requested range must not include an
        # unaccounted huge neighboring literal or previously omitted content.
        for direction in (-1, 1):
            for distance in range(1, CONTEXT_RADIUS + 1):
                index = center + direction * distance
                if not 0 <= index < len(lines):
                    break
                size = 0 if index in selected else sizes[index]
                if selected_bytes + size > MAX_DIAGNOSTIC_CONTEXT_BYTES:
                    break
                selected_bytes += size
                added.add(index)
                start, end = min(start, index), max(end, index + 1)
        selected.update(added)
        windows.append((start, end))

    add_window(failed)
    attributes: set[str] = set()
    try:
        tree = ast.parse(content)
        # Inspect only an expression at the failing line, not every attribute
        # in an enclosing function/loop. An attribute name is a search hint,
        # not a conclusion that all its uses are erroneous.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.lineno <= line <= (node.end_lineno or node.lineno)):
                attributes.add(node.attr)
        related = sorted({node.lineno - 1 for node in ast.walk(tree)
                          if isinstance(node, ast.Attribute) and node.attr in attributes})
        for index in related:
            if len(windows) >= MAX_DIAGNOSTIC_WINDOWS:
                break
            add_window(index)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        pass  # Exact failure context remains useful without AST search.
    # Merge overlaps to avoid redundant file_read calls and duplicate text.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    # Present the failing range before other related uses.
    merged.sort(key=lambda bounds: (not bounds[0] <= failed < bounds[1], bounds[0]))
    return DiagnosticSourceContext(source_digest, failed, lines[failed], tuple(merged),
                                   selected_bytes, tuple(sorted(attributes)))
