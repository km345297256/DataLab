"""Private host observations for bounded authoring repair progress.

Public diagnostics intentionally omit matched values and may truncate issues.
They cannot identify whether moving a line actually resolved a private value.
These complete observations remain inside one gateway call; no identities are
sent to the model, retained in checkpoints, or included in operational logs.
"""
from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json

from .privacy import PrivacyContext


@dataclass(frozen=True)
class PrivacyFailureSnapshot:
    problems: Counter[str] = field(repr=False)


def privacy_failure_snapshot(value, privacy: PrivacyContext) -> PrivacyFailureSnapshot | None:
    """Bind actual matches to fields and values, independent of source lines.

    Different values in the same field remain different problems. Multiple
    occurrences count independently, so removing some of them is real progress.
    Python literal escapes are decoded only for identity comparison; the
    privacy scanner remains the sole authority for whether a match exists.
    """
    document = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    pointer_locations = privacy._amendment_pointer_locations(document)
    problems = Counter()

    def walk(node, location):
        if isinstance(node, str):
            occurrences = {}
            for match in privacy._matches(node, location, assertion_pointer=location in pointer_locations):
                literal = node[match.start:match.end]
                identities = Counter({(match.category, literal): 1})
                # Decoded source matches cover the whole quoted literal. Use
                # its actual private substrings, not surrounding prose/quotes.
                try:
                    # AST spans of adjacent string tokens omit their outer
                    # grouping parentheses. Preserve the scanner's treatment
                    # of multiline tokens, comments and physical newlines.
                    decoded = ast.literal_eval("(" + literal + ")")
                except (SyntaxError, ValueError, TypeError, RecursionError):
                    decoded = None
                if isinstance(decoded, str):
                    nested = privacy._matches(decoded)
                    identities = Counter((item.category, decoded[item.start:item.end]) for item in nested)
                    if not identities:
                        identities = Counter({(match.category, literal): 1})
                for (category, private_value), count in identities.items():
                    identity = hashlib.sha256(json.dumps(
                        [location, category, private_value], ensure_ascii=False,
                        separators=(",", ":")).encode()).hexdigest()
                    occurrences.setdefault(identity, []).append((match.start, match.end, count))
            for identity, ranges in occurrences.items():
                # Raw and decoded scans can name the same occurrence. Merge
                # overlapping spans without erasing distinct repeated values.
                merged = []
                for start, end, count in sorted(ranges, key=lambda item: (item[0], -item[1])):
                    if merged and start < merged[-1][1]:
                        prior = merged[-1]
                        merged[-1] = (prior[0], max(prior[1], end), max(prior[2], count))
                    else:
                        merged.append((start, end, count))
                problems[identity] += sum(item[2] for item in merged)
        elif isinstance(node, dict):
            for index, (key, nested) in enumerate(node.items()):
                walk(str(key), (*location, "<key>", index))
                walk(nested, (*location, str(key)))
        elif isinstance(node, (list, tuple)):
            for index, nested in enumerate(node):
                walk(nested, (*location, index))

    walk(document, ())
    return PrivacyFailureSnapshot(problems) if problems else None


class PrivacyRepairProgress:
    """Count repeated failures whose actual prior problems all still remain.

    Removing an occurrence or replacing a resolved problem with a different
    one permits repair to continue. Moving lines, changing surrounding prose,
    or adding more private values does not. Intervening transport/schema
    failures never erase an observation that has not been disproved.
    """
    def __init__(self):
        self._observations = []

    def record(self, snapshot: PrivacyFailureSnapshot) -> int:
        current = snapshot.problems
        repeated = 1 + sum(all(current[key] >= count for key, count in previous.items())
                           for previous in self._observations)
        self._observations.append(current.copy())
        return repeated
