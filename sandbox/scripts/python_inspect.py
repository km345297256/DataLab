"""Bounded installed API observations in the analysis interpreter.

Run through the ordinary program executor, never inside the backend/API process.
Importing a package can run initialization code; this is not a read-only tool.
No selected data files, instance construction or target-function calls are needed.
Names are syntax-checked only, never compared with a library/API permission list.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import importlib.metadata
import inspect
import json
import os
import platform
import re
import sys


MAX_MEMBERS = 8
MAX_DOCUMENTATION_CHARS = 6000
MAX_SIGNATURE_CHARS = 1200
MAX_REPORT_BYTES = 28000
_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z", re.ASCII)


def _valid_name(value):
    return isinstance(value, str) and len(value) <= 256 and bool(_NAME.fullmatch(value))


def _bounded_text(value, limit):
    return value.encode("utf-8", errors="replace")[:max(0, limit)].decode("utf-8", errors="ignore")


def _documentation(value, limit=MAX_DOCUMENTATION_CHARS):
    """Retain return semantics as well as the beginning of a long docstring."""
    doc = (inspect.getdoc(value) or "").encode("utf-8", errors="replace").decode("utf-8")
    if len(doc.encode("utf-8")) <= limit:
        return {"text": doc, "truncated": False}
    lines = doc.splitlines()
    # NumPy/reST/Google headings are observations, not an API-name catalog.
    starts = [index for index, line in enumerate(lines)
              if line.strip().casefold().rstrip(":") in {"returns", "yields", "raises"}]
    head = _bounded_text(doc, limit // 2)
    selected = []
    remaining = limit - len(head.encode("utf-8")) - 100
    for index in starts[:3]:
        section = _bounded_text("\n".join(lines[index:]), min(remaining, 1800))
        if section:
            selected.append(section)
            remaining -= len(section.encode("utf-8")) + 25
        if remaining <= 0:
            break
    return {"text": head + "\n[... documentation omitted ...]\n" + "\n[...]\n".join(selected),
            "truncated": True}


def _signature(value):
    # A callable instance can expose executable __signature__/__wrapped__
    # properties. Describe its type without traversing instance descriptors.
    if not (inspect.isroutine(value) or inspect.isclass(value)):
        return {"state": "unavailable", "text": None, "truncated": False}
    try:
        signature = inspect.signature(value, eval_str=False)
        # Do not repr arbitrary default objects or evaluate annotations. Their
        # types/default presence are enough to prevent argument-shape guessing.
        parameters = []
        for item in signature.parameters.values():
            default = item.default
            if default is not inspect.Parameter.empty and type(default) not in {int, float, bool, type(None)}:
                default = "<default omitted>"
            parameters.append(item.replace(default=default, annotation=inspect.Parameter.empty))
        rendered = str(signature.replace(parameters=parameters, return_annotation=inspect.Signature.empty))
        return {"state": "observed", "text": _bounded_text(rendered, MAX_SIGNATURE_CHARS),
                "truncated": len(rendered.encode("utf-8", errors="replace")) > MAX_SIGNATURE_CHARS,
                "annotations_omitted": True, "non_scalar_defaults_omitted": True}
    except (TypeError, ValueError):
        return {"state": "unavailable", "text": None, "truncated": False}


def inspect_api(module_name, names):
    if (not _valid_name(module_name) or not isinstance(names, list) or len(names) > MAX_MEMBERS
            or any(not _valid_name(name) for name in names)):
        raise ValueError("Use a dotted module name and at most eight relative dotted attribute names")
    report = {"schema": "python-api-inspection/v1", "module": module_name,
              "python_version": platform.python_version(), "state": "unavailable",
              "distributions": [], "members": []}
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        # Exception strings may contain data, credentials or filesystem paths.
        report.update(state="import_failed", error_type=_bounded_text(type(error).__name__, 128))
        return report
    report["state"] = "observed"
    root = module_name.split(".")[0]
    try:
        candidates = sorted(importlib.metadata.packages_distributions().get(root, []))[:4]
        lookup = "module_mapping" if candidates else "same_name_distribution"
        # Some wheels omit top_level.txt and cannot appear in the module map
        # even though their distribution/version metadata exists.
        for distribution in candidates or [root]:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                continue
            report["distributions"].append({"name": _bounded_text(distribution, 256),
                                             "version": _bounded_text(version, 128), "lookup": lookup})
    except Exception:
        pass  # A local module can have useful APIs without distribution metadata.
    selected = list(dict.fromkeys(names or [module_name]))
    doc_budget = min(MAX_DOCUMENTATION_CHARS, 12000 // len(selected))
    for name in selected:
        item = {"name": name}
        try:
            value = module
            if names:
                for part in name.split("."):
                    # Modules may expose lazy APIs. Avoid invoking properties
                    # on classes/instances while traversing the remaining path.
                    value = getattr(value, part) if inspect.ismodule(value) else inspect.getattr_static(value, part)
                    if isinstance(value, (staticmethod, classmethod)):
                        value = value.__func__
            describes_api = inspect.ismodule(value) or inspect.isroutine(value) or inspect.isclass(value) or isinstance(value, property)
            item.update(state="observed", kind=_bounded_text(type.__getattribute__(type(value), "__name__"), 128),
                        signature=_signature(value),
                        documentation=_documentation(value, doc_budget) if describes_api else {"text": "", "truncated": False})
        except AttributeError:
            item.update(state="missing")
        except Exception as error:
            item.update(state="unavailable", error_type=_bounded_text(type(error).__name__, 128))
        report["members"].append(item)
    return report


def serialize_report(report):
    """Bound the final JSON bytes, including escaping, while retaining identity."""
    def encode():
        return json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

    encoded = encode()
    while len(encoded) > MAX_REPORT_BYTES:
        documents = [item.get("documentation") for item in report.get("members", [])]
        documents = [item for item in documents if isinstance(item, dict) and item.get("text")]
        if documents:
            largest = max(documents, key=lambda item: len(item["text"].encode("utf-8")))
            largest["text"] = _bounded_text(largest["text"], len(largest["text"].encode("utf-8")) // 2)
            largest["truncated"] = True
        else:
            signatures = [item.get("signature") for item in report.get("members", [])]
            signatures = [item for item in signatures if isinstance(item, dict) and item.get("text")]
            if not signatures:
                raise ValueError("API inspection metadata exceeds the output bound")
            largest = max(signatures, key=lambda item: len(item["text"].encode("utf-8")))
            largest["text"] = _bounded_text(largest["text"], len(largest["text"].encode("utf-8")) // 2)
            largest["truncated"] = True
        encoded = encode()
    return encoded


@contextmanager
def _quiet_package_output(*, restore=True):
    """Keep native/Python import output out of the structured observation."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    streams = sys.stdout, sys.stderr
    try:
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            try:
                yield
            finally:
                # Flush buffered import output while both descriptors still
                # point to /dev/null, even if the import raises SystemExit.
                for stream in (*streams, sys.stdout, sys.stderr):
                    try:
                        stream.flush()
                    except Exception:
                        pass
    finally:
        sys.stdout, sys.stderr = streams
        for target, descriptor in zip((1, 2), saved):
            if restore:
                os.dup2(descriptor, target)
            os.close(descriptor)


def main():
    request = json.loads(sys.argv[1])
    output = os.dup(1)
    try:
        # Keep package atexit handlers and delayed native output muted too.
        # Only our compact observation is written to the original descriptor.
        with _quiet_package_output(restore=False):
            result = inspect_api(request["module"], request.get("names", []))
            encoded = serialize_report(result) + b"\n"
            while encoded:
                encoded = encoded[os.write(output, encoded):]
    finally:
        os.close(output)


if __name__ == "__main__":
    main()
