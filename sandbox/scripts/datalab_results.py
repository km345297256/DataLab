"""Single-source result exports with executable, explicitly limited checks.

ResultBundle freezes finite JSON-compatible values. JSON, CSV, simple plots and
generated report facts then consume those exact values. This checks consistency,
not whether the supplied computation, units, method or interpretation is correct.
In particular, a caller's ``checks`` or ``passed`` field is ordinary input data.

Use tables of row dictionaries, e.g. ResultBundle({'test': named_test_result,
'rows': [{'x': 1, 'estimate': 2}, ...]}). Save charts/tables before the report,
then call verify() immediately before delivery. No file is uploaded automatically.
The helpers neither read nor copy analysis inputs. Arbitrary libraries and other
plotting workflows remain available; unsupported plots need their own checks.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile


class ResultConsistencyError(ValueError):
    """A concrete result/export check failed; input values are not echoed."""


def _json(value, *, pretty=False):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          indent=2 if pretty else None,
                          separators=None if pretty else (",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ResultConsistencyError("Results must contain finite JSON-compatible values") from error


def _validate_json(value):
    # Do not silently turn non-string keys, numpy arrays or custom objects into
    # ambiguous strings. Callers must explicitly select/convert computed values.
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ResultConsistencyError("Result object keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif type(value) is list:
        for item in value:
            _validate_json(item)
    elif value is None or type(value) in {str, bool, int}:
        return
    elif type(value) is float and math.isfinite(value):
        return
    else:
        raise ResultConsistencyError("Results must contain finite JSON-compatible values")


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _output(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ResultConsistencyError("Choose an absolute writable computation/output path")
    inputs = Path("/home/ubuntu/inputs")
    if path == inputs or path.is_relative_to(inputs):
        raise ResultConsistencyError("Mounted analysis inputs are read-only")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ResultConsistencyError("Result paths must not traverse symbolic links")
    if path.exists() and not path.is_file():
        raise ResultConsistencyError("A result destination must be a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".datalab-result-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cell(value):
    # CSV itself has no type schema. Check its canonical cell text, never through
    # lossy float inference or NA coercion; the frozen result retains value types.
    if type(value) is str:
        return value
    if value is None or type(value) in {int, float, bool}:
        return _json(value).decode("utf-8")
    raise ResultConsistencyError("CSV table cells must be JSON scalars")


@dataclass(frozen=True)
class _Artifact:
    path: Path
    kind: str
    digest: str
    specification: bytes


class ResultBundle:
    """Freeze computed results and export checked projections of the same values.

    ``snapshot()`` returns a fresh copy; editing it never changes this version.
    Verification only covers explicitly generated exports/facts, not arbitrary
    prose, upstream scientific correctness or arbitrary external figure files.
    """

    def __init__(self, data: dict):
        if type(data) is not dict or not data:
            raise ResultConsistencyError("Supply a nonempty named result object")
        _validate_json(data)
        self._encoded = _json(data)
        self._artifacts: dict[Path, _Artifact] = {}

    @property
    def version(self):
        """SHA-256 of canonical computed values, not a scientific certificate."""
        return _hash(self._encoded)

    def snapshot(self):
        """Return an independent JSON copy of this result version."""
        return json.loads(self._encoded)

    def _table(self, name):
        table = self.snapshot().get(name)
        if not isinstance(table, list) or not table or any(type(row) is not dict for row in table):
            raise ResultConsistencyError("A named table must contain nonempty row objects")
        columns = tuple(table[0])
        if not columns or any(set(row) != set(columns) for row in table):
            raise ResultConsistencyError("Every table row must have the same named columns")
        for row in table:
            for value in row.values():
                _cell(value)
        return table, columns

    def _destination(self, path, kind):
        path = _output(path)
        if any(item.path != path and item.path.name == path.name for item in self._artifacts.values()):
            raise ResultConsistencyError("Generated exports need distinct filenames in report inventory")
        existing = self._artifacts.get(path)
        if existing is not None and existing.kind != kind:
            raise ResultConsistencyError("Different result formats need separate destinations")
        return path

    def _record(self, path, kind, content, specification):
        self._artifacts[path] = _Artifact(path, kind, _hash(content), _json(specification))
        return {"result_version": self.version, "format": kind,
                "sha256": _hash(content), "scope": "export_consistency"}

    def write_json(self, path):
        """Write all frozen values, then check the actual JSON round trip."""
        path = self._destination(path, "json")
        content = _json(self.snapshot(), pretty=True) + b"\n"
        _atomic_write(path, content)
        if _json(json.loads(path.read_bytes())) != self._encoded:
            raise ResultConsistencyError("Written JSON differs from the result snapshot")
        return self._record(path, "json", content, {})

    def write_csv(self, path, *, table, columns=None):
        """Write a named row table and verify actual columns, rows and cells.

        Explicit columns must contain every table column exactly once; omitted
        required output columns cannot silently disappear from this export.
        """
        rows, names = self._table(table)
        columns = tuple(names if columns is None else columns)
        if len(columns) != len(names) or len(set(columns)) != len(columns) or set(columns) != set(names):
            raise ResultConsistencyError("CSV columns must include every named table column exactly once")
        path = self._destination(path, "csv")
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        expected = [list(columns), *[[_cell(row[name]) for name in columns] for row in rows]]
        writer.writerows(expected)
        content = stream.getvalue().encode("utf-8")
        _atomic_write(path, content)
        self._verify_csv(path, table, columns)
        return self._record(path, "csv", content,
                            {"table": table, "columns": list(columns), "rows": len(rows)})

    def _verify_csv(self, path, table, columns):
        rows, _ = self._table(table)
        with path.open(encoding="utf-8", newline="") as source:
            observed = list(csv.reader(source))
        expected = [list(columns), *[[_cell(row[name]) for name in columns] for row in rows]]
        if observed != expected:
            raise ResultConsistencyError("CSV columns, row order or cell values differ from the result snapshot")

    def plot(self, path, *, table, x, y, kind="line", group=None, lower=None, upper=None):
        """Render and check a simple line/scatter plot from a frozen row table.

        Lines follow table row order. Optional lower/upper columns produce error
        bars and are described only as supplied bounds: this helper cannot prove
        they are SE or a confidence interval. Missing/nonfinite data and bounds
        that do not contain y are rejected instead of being silently dropped.
        """
        import numpy as np
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.collections import PathCollection
        from matplotlib.lines import Line2D
        from matplotlib.colors import to_rgba

        rows, columns = self._table(table)
        selected = [x, y] + ([group] if group is not None else [])
        if (lower is None) != (upper is None):
            raise ResultConsistencyError("Plot bounds need both lower and upper columns")
        if lower is not None:
            selected.extend([lower, upper])
        if kind not in {"line", "scatter"} or any(name not in columns for name in selected):
            raise ResultConsistencyError("Choose supported plot geometry and existing table columns")
        path = self._destination(path, "png")
        if path.suffix.lower() != ".png":
            raise ResultConsistencyError("This plot exporter writes PNG files")
        grouped = {}
        for row in rows:
            label = _json(row[group]).decode("utf-8") if group is not None else "all observations"
            grouped.setdefault(label, []).append(row)

        def values(items, field):
            value = [row[field] for row in items]
            converted = []
            for item in value:
                try:
                    if type(item) not in {int, float}:
                        raise ValueError
                    floating = float(item)
                    if not math.isfinite(floating):
                        raise ValueError
                    # JSON integers may exceed float precision: different row
                    # coordinates must not collapse during plotting conversion.
                    if type(item) is int and int(floating) != item:
                        raise ValueError
                except (ValueError, OverflowError):
                    raise ResultConsistencyError("Plot coordinates and bounds need finite, exactly representable numbers") from None
                converted.append(floating)
            return np.asarray(converted, dtype=float)

        figure = Figure(figsize=(6, 4))
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        for label, items in grouped.items():
            xs, ys = values(items, x), values(items, y)
            if kind == "line":
                artist, = axes.plot(xs, ys, label=label, linestyle="-",
                                    marker="o" if len(items) == 1 else None)
                if not isinstance(artist, Line2D) or not np.array_equal(artist.get_xdata(), xs) or not np.array_equal(artist.get_ydata(), ys):
                    raise ResultConsistencyError("Rendered line coordinates differ from the result snapshot")
                visible = (artist.get_visible() and to_rgba(artist.get_color())[3] > 0
                           and (artist.get_alpha() is None or artist.get_alpha() > 0))
                if len(items) == 1:
                    visible = (visible and artist.get_marker() not in {None, "", "None", "none", " "}
                               and artist.get_markersize() > 0)
                else:
                    visible = (visible and artist.get_linestyle() not in {None, "", "None", "none", " "}
                               and artist.get_linewidth() > 0)
                if not visible:
                    raise ResultConsistencyError("A rendered line group has no visible geometry")
            else:
                artist = axes.scatter(xs, ys, label=label)
                if not isinstance(artist, PathCollection) or not np.array_equal(artist.get_offsets(), np.column_stack([xs, ys])):
                    raise ResultConsistencyError("Rendered scatter coordinates differ from the result snapshot")
                visible_colors = any(np.asarray(colors).size and np.any(np.asarray(colors)[:, 3] > 0)
                                     for colors in (artist.get_facecolors(), artist.get_edgecolors()))
                if (not artist.get_visible() or not np.all(artist.get_sizes() > 0) or not visible_colors
                        or (artist.get_alpha() is not None and artist.get_alpha() <= 0)):
                    raise ResultConsistencyError("A rendered scatter group has no visible geometry")
            if lower is not None:
                lows, highs = values(items, lower), values(items, upper)
                if np.any(lows > ys) or np.any(highs < ys):
                    raise ResultConsistencyError("Supplied bounds must contain each plotted value")
                differences = np.vstack([ys-lows, highs-ys])
                if not np.isfinite(differences).all():
                    raise ResultConsistencyError("Supplied bound differences exceed finite plotting coordinates")
                errors = axes.errorbar(xs, ys, yerr=differences, fmt="none")
                bars = errors.lines[2]
                expected = np.stack([np.column_stack([xs, lows]), np.column_stack([xs, highs])], axis=1)
                if len(bars) != 1 or not np.allclose(np.asarray(bars[0].get_segments()), expected, rtol=1e-12, atol=0):
                    raise ResultConsistencyError("Rendered error bars differ from the supplied bounds")
        axes.set_xlabel(x)
        axes.set_ylabel(y)
        if group is not None:
            legend = axes.legend(title=group)
            if [item.get_text() for item in legend.get_texts()] != list(grouped):
                raise ResultConsistencyError("Rendered groups do not match the result snapshot")
        figure.tight_layout()
        stream = io.BytesIO()
        figure.savefig(stream, format="png")
        content = stream.getvalue()
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ResultConsistencyError("The rendered output is not a PNG")
        _atomic_write(path, content)
        specification = {"table": table, "x": x, "y": y, "kind": kind,
                         "group": group, "series": len(grouped), "points": len(rows),
                         "lower": lower, "upper": upper,
                         "uncertainty": "supplied_bounds_not_statistically_verified" if lower is not None else "none"}
        return {**self._record(path, "png", content, specification), "plot": specification}

    def _inventory(self):
        return [{"format": item.kind, "name": item.path.name,
                 "specification": json.loads(item.specification)}
                for item in self._artifacts.values() if item.kind in {"csv", "png"}]

    def _facts(self, sections, inventory):
        data = self.snapshot()
        if (not sections or len(set(sections)) != len(sections)
                or any(type(key) is not str or key not in data for key in sections)):
            raise ResultConsistencyError("Report fact sections must select distinct existing result keys")
        # Keep internal identities/check contracts in verify(), not in the
        # user's report. These short captions describe the actual export.
        block = {key: data[key] for key in sections}
        descriptions = []
        for item in inventory:
            spec = item["specification"]
            if item["format"] == "png":
                bounds = ("; error bars show supplied lower/upper bounds"
                          if spec["lower"] is not None else "; no error bars")
                descriptions.append(f"Figure {item['name']}: {spec['kind']} plot; "
                                    f"{spec['series']} series, {spec['points']} plotted observations; "
                                    f"x = {spec['x']}, y = {spec['y']}{bounds}.")
            elif item["format"] == "csv":
                descriptions.append(f"Table {item['name']}: {spec['rows']} rows; "
                                    "columns: " + ", ".join(spec["columns"]) + ".")
        facts = "```json\n" + _json(block, pretty=True).decode("utf-8") + "\n```"
        if descriptions:
            facts += "\n\n" + "\n\n".join(descriptions)
        return "<!-- datalab-generated-facts:start -->\n" + facts + "\n<!-- datalab-generated-facts:end -->"

    def write_report(self, path, *, sections, narrative=""):
        """Write generated facts plus separately labelled, unchecked narrative.

        Save charts/tables first. verify() rejects changed facts/bytes and a stale
        export inventory. It does not claim to verify free-text interpretation.
        No unrequested manifest is generated as a separate delivery artifact.
        """
        if type(narrative) is not str or "<!-- datalab-generated-facts:" in narrative:
            raise ResultConsistencyError("Report narrative must be separate from generated facts")
        sections = tuple(sections)
        inventory = self._inventory()
        facts = self._facts(sections, inventory)
        text = "# Recorded analysis results\n\n" + facts + "\n"
        if narrative:
            text += "\n## Interpretation (not independently checked)\n\n" + narrative + "\n"
        content = text.encode("utf-8")
        path = self._destination(path, "report")
        _atomic_write(path, content)
        return self._record(path, "report", content,
                            {"sections": list(sections), "inventory": inventory})

    def verify(self):
        """Recheck every registered export immediately before handing it off.

        A successful receipt attests only export consistency with frozen values.
        It does not interpret or trust any input field named 'checks'/'passed'.
        """
        if not self._artifacts:
            raise ResultConsistencyError("No generated exports are available to verify")
        checks = []
        for artifact in self._artifacts.values():
            path = artifact.path
            if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
                raise ResultConsistencyError("A generated export is missing or redirected")
            content = path.read_bytes()
            if _hash(content) != artifact.digest:
                raise ResultConsistencyError("Generated export bytes changed after verification")
            spec = json.loads(artifact.specification)
            if artifact.kind == "json":
                if _json(json.loads(content)) != self._encoded:
                    raise ResultConsistencyError("JSON differs from the result snapshot")
            elif artifact.kind == "csv":
                self._verify_csv(path, spec["table"], spec["columns"])
            elif artifact.kind == "report":
                if spec["inventory"] != self._inventory():
                    raise ResultConsistencyError("Report export inventory is stale; regenerate the report")
                facts = self._facts(spec["sections"], spec["inventory"])
                if content.decode("utf-8").count(facts) != 1:
                    raise ResultConsistencyError("Generated report facts differ from the result snapshot")
            checks.append({"format": artifact.kind, "sha256": artifact.digest,
                           "scope": "rendered_data_and_unchanged_bytes" if artifact.kind == "png" else "snapshot_round_trip_and_unchanged_bytes"})
        return {"schema": "datalab-result-consistency/v1", "result_version": self.version,
                "checks": checks, "scientific_validity": "not_assessed", "free_text": "not_assessed"}
