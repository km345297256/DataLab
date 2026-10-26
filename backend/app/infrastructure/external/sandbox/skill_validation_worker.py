"""Run generated helpers against synthetic data in disposable, isolated containers.

Only the selected helper is executable. Oracles remain in the API process as
declarative data; actual CSV/JSON files are read back through Docker and compared
here. No generated code, shell command, or assertion is executed in the backend.
"""
from __future__ import annotations

import asyncio
import csv
from decimal import Decimal, DecimalException
import hashlib
import io
import json
import math
import re
import socket
import tarfile
import threading
import time
import uuid
from pathlib import PurePosixPath

import docker

from app.core.config import get_settings
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_arguments import argument_reference
from app.domain.services.skills.skill_public_output_contract import (
    PublicOutputContract, PublicOutputContractError, check_public_output, failed_public_case,
    public_case_status, public_contract_receipt, public_output_path, public_rules_for_case,
)
from app.infrastructure.external.sandbox.helper_security import helper_security_options

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
CASE_SECONDS = 40
CONTAINER_SECONDS = 50
OUTPUT_ROOT = "/home/ubuntu/output"
RUNTIME_USER = "1000:1000"  # The existing sandbox image's unprivileged ubuntu user.
_COMPONENT = "skill-synthetic-validation"
_SCRIPT = re.compile(r"scripts/[a-z][a-z0-9_-]{0,47}\.py\Z")
_INPUT = re.compile(r"\{input:([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\}\Z")

# The fixed launcher reports an actual escaping builtin exception through a
# dedicated pipe. Generated stdout/stderr never provide this classification.
# The channel contains no exception message, traceback, locals or input values.
_CHILD_RUNNER = r'''
import dis, json, os, runpy, sys
channel, nonce, script = int(sys.argv[1]), sys.argv[2], sys.argv[3]
write, close, dumps, actual_type = os.write, os.close, json.dumps, type
opnames = tuple(dis.opname)
known = ((TypeError, "TypeError"), (NameError, "NameError"),
         (UnboundLocalError, "UnboundLocalError"), (AttributeError, "AttributeError"),
         (KeyError, "KeyError"), (IndexError, "IndexError"))
known_dependencies = ((ModuleNotFoundError, "ModuleNotFoundError"), (ImportError, "ImportError"))
sys.argv = [script, *sys.argv[4:]]
sys.path[0] = script.rpartition("/")[0]
kind = None
dependency = None
try:
    runpy.run_path(script, run_name="__main__")
except BaseException as error:
    kind = next((name for cls, name in known if actual_type(error) is cls), None)
    # A library can deliberately raise ImportError for business logic. Only
    # the interpreter's actual import instruction authorizes this diagnosis;
    # exception text, attributes such as name/path, and stderr are not used.
    dependency_kind = next((name for cls, name in known_dependencies if actual_type(error) is cls), None)
    if dependency_kind is not None:
        trace = error.__traceback__
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        if (trace is not None and 0 <= trace.tb_lasti < len(trace.tb_frame.f_code.co_code)
                and opnames[trace.tb_frame.f_code.co_code[trace.tb_lasti]] in {"IMPORT_NAME", "IMPORT_FROM"}):
            dependency = dependency_kind
    raise
finally:
    try:
        evidence = {"nonce": nonce, "exception_type": kind}
        if dependency is not None:
            evidence["dependency_exception"] = dependency
        data = (dumps(evidence) + "\n").encode()
        while data:
            data = data[write(channel, data):]
    finally:
        close(channel)
'''

_RUNTIME_BUILD_TYPES = frozenset({"TypeError", "NameError", "UnboundLocalError", "AttributeError", "KeyError", "IndexError"})
_DEPENDENCY_TYPES = frozenset({"ModuleNotFoundError", "ImportError"})

# A fixed supervisor, not generated test code. Child streams are drained under a
# cap; the nonce/returncode receipt carries no pass verdict. Keep tmpfs alive so
# the host can retrieve real files with the fixed Docker reader before removal.
_SUPERVISOR = r'''
import json, os, selectors, signal, subprocess, sys, time
request = json.loads(sys.stdin.buffer.read(32769))
env = {"PATH":"/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "HOME":"/home/ubuntu/output",
       "TMPDIR":"/tmp", "PYTHONDONTWRITEBYTECODE":"1", "OPENBLAS_NUM_THREADS":"1",
       "OMP_NUM_THREADS":"1", "MKL_NUM_THREADS":"1", "MPLCONFIGDIR":"/tmp/matplotlib",
       "PYTHONPATH":"/usr/local/lib/ai-dataseek:/app/.venv/lib/python3.10/site-packages:/usr/lib/python3/dist-packages"}
exception_read, exception_write = os.pipe()
child = subprocess.Popen(["/app/.venv/bin/python", "-B", "-c", __CHILD_RUNNER_SOURCE__,
    str(exception_write), request["nonce"], request["script"], *request["argv"]],
    cwd="/home/ubuntu/output", env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, start_new_session=True, pass_fds=(exception_write,))
os.close(exception_write)
selector = selectors.DefaultSelector()
for stream in (child.stdout, child.stderr):
    os.set_blocking(stream.fileno(), False)
    selector.register(stream, selectors.EVENT_READ)
os.set_blocking(exception_read, False)
selector.register(exception_read, selectors.EVENT_READ)
exception_data = bytearray()
deadline = time.monotonic() + 30
captured = 0
diagnostic = bytearray()
stderr_truncated = False
reason = None
while selector.get_map():
    if time.monotonic() > deadline:
        reason = "program_timeout"
        break
    for key, _ in selector.select(.1):
        chunk = os.read(key.fd, 8192)
        if not chunk:
            selector.unregister(key.fileobj)
            continue
        if key.fileobj == exception_read:
            exception_data.extend(chunk)
            if len(exception_data) > 1024:
                reason = "program_exception_evidence_invalid"
                break
            continue
        captured += len(chunk)
        if key.fileobj is child.stderr:
            diagnostic.extend(chunk)
            stderr_truncated = stderr_truncated or len(diagnostic) > 2048
            del diagnostic[:-2048]
        if captured > 512 * 1024:
            reason = "program_output_limit"
            break
    if reason:
        break
if reason or child.poll() is None:
    try:
        child.wait(timeout=max(.01, deadline-time.monotonic())) if not reason else None
    except subprocess.TimeoutExpired:
        reason = "program_timeout"
if reason:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
child.wait(timeout=2)
# Do not leave descendants writing outputs while the host compares them.
try:
    os.killpg(child.pid, signal.SIGKILL)
except ProcessLookupError:
    pass
# A helper may start a new session, escaping the original process group. This
# container owns a private PID namespace: stop every other live process except
# the fixed PID-1 timeout and this supervisor before exposing files to the host.
for attempt in range(20):
    descendants = []
    for name in os.listdir("/proc"):
        if name.isdecimal() and int(name) not in (1, os.getpid()):
            try:
                state = open("/proc/" + name + "/stat").read().rsplit(")", 1)[1].split()[0]
                if state != "Z":
                    descendants.append(int(name))
                    os.kill(int(name), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
    if not descendants:
        break
    time.sleep(.02)
else:
    reason = "program_cleanup_failed"
os.close(exception_read)
runtime_exception = None
dependency_exception = None
if exception_data:
    try:
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate exception metadata")
                result[key] = value
            return result
        evidence = json.loads(exception_data, object_pairs_hook=unique)
        if (set(evidence) not in ({"nonce", "exception_type"}, {"nonce", "exception_type", "dependency_exception"})
                or evidence["nonce"] != request["nonce"]
                or evidence["exception_type"] not in {None, "TypeError", "NameError", "UnboundLocalError", "AttributeError", "KeyError", "IndexError"}
                or "dependency_exception" in evidence and (evidence["dependency_exception"] not in {"ModuleNotFoundError", "ImportError"}
                    or evidence["exception_type"] is not None)):
            raise ValueError("invalid exception metadata")
        if child.returncode == 1 and reason is None:
            runtime_exception = evidence["exception_type"]
            dependency_exception = evidence.get("dependency_exception")
    except (ValueError, TypeError):
        reason = reason or "program_exception_evidence_invalid"
receipt = {"nonce":request["nonce"], "exit_code":child.returncode,
    "reason":reason, "stderr":diagnostic.decode("utf-8", "replace"), "stderr_truncated":stderr_truncated,
    "runtime_exception":runtime_exception}
if dependency_exception is not None:
    receipt["dependency_exception"] = dependency_exception
print(json.dumps(receipt), flush=True)
time.sleep(60)
'''.replace('__CHILD_RUNNER_SOURCE__', repr(_CHILD_RUNNER))

# Docker archive endpoints do not expose a running container's tmpfs on all
# supported runtimes. This fixed, isolated reader returns actual file bytes;
# generated helpers have exited, and scientific oracles still stay on the host.
_READ_FILE = r'''
import os, stat, sys
directory = None
try:
    parts = sys.argv[2].split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("output_unsafe")
    directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    for part in parts[:-1]:
        next_directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = next_directory
    descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("output_not_regular_file")
        if before.st_size > 2 * 1024 * 1024:
            raise ValueError("output_file_limit")
        data = stream.read(2 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("output_changed")
    sys.stdout.buffer.write(data)
except FileNotFoundError:
    print("output_missing", file=sys.stderr)
    sys.exit(2)
except (OSError, ValueError) as error:
    allowed = {"output_unsafe", "output_not_regular_file", "output_file_limit", "output_changed"}
    print(str(error) if str(error) in allowed else "output_unsafe", file=sys.stderr)
    sys.exit(2)
finally:
    if directory is not None:
        os.close(directory)
'''

# This fixed host-owned probe observes directory metadata only. It neither
# follows entries nor returns their names or contents, including for symlinks.
_OUTPUT_EMPTY = r'''
import os, sys
directory = None
try:
    directory = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    before = os.fstat(directory)
    with os.scandir(directory) as entries:
        empty = next(entries, None) is None
    after = os.fstat(directory)
    if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("output_changed")
    sys.stdout.buffer.write(b"empty\n" if empty else b"nonempty\n")
except (OSError, ValueError):
    print("output_presence_unconfirmed", file=sys.stderr)
    sys.exit(2)
finally:
    if directory is not None:
        os.close(directory)
'''


class _Unavailable(RuntimeError):
    pass


class _Failed(RuntimeError):
    pass


class _Cancelled(RuntimeError):
    pass


def _check(cancelled: threading.Event, deadline: float) -> None:
    if cancelled.is_set():
        raise _Cancelled()
    if time.monotonic() > deadline:
        raise _Failed("validation_timeout")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _materialize(case, root: str) -> dict[str, bytes]:
    def replace(value):
        if isinstance(value, str) and (match := _INPUT.fullmatch(value)):
            if match[1] not in names:
                raise ValueError("Unknown synthetic input reference")
            return root + "/" + match[1]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value

    names = {item.path for item in case.input_files}
    result = {}
    for fixture in case.input_files:
        if fixture.format == "csv":
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fixture.columns)
            writer.writeheader()
            writer.writerows(fixture.rows)
            result[fixture.path] = output.getvalue().encode()
        else:
            result[fixture.path] = _json(replace(fixture.data))
    return result


def _argv(case, root: str) -> list[str]:
    rendered = []
    for token in case.argv:
        reference = argument_reference(token)
        if reference is None:
            rendered.append(token)
        else:
            kind, suffix = reference
            rendered.append(root + "/" + suffix if kind == "input" else OUTPUT_ROOT + suffix)
    return rendered


def _tar(files: dict[str, bytes]) -> bytes:
    if any(len(data) > MAX_FILE_BYTES for data in files.values()) or sum(map(len, files.values())) > MAX_INPUT_BYTES:
        raise _Unavailable("synthetic_input_limit")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        directories = sorted({str(parent) for path in files for parent in PurePosixPath(path).parents if str(parent) != "."})
        for path in directories:
            info = tarfile.TarInfo(path)
            info.type = tarfile.DIRTYPE
            info.mode = 0o555
            archive.addfile(info)
        for path, data in files.items():
            if PurePosixPath(path).is_absolute() or any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValueError("Unsafe synthetic input path")
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), 0o444
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _owned_lookup(collection, name: str, token: str, *, volume=False):
    try:
        item = collection.get(name)
    except docker.errors.NotFound:
        return None
    labels = item.attrs.get("Labels", {}) if volume else item.attrs.get("Config", {}).get("Labels", {})
    return item if (labels or {}).get("datalab.validation.owner") == token else None


def _remove(container) -> None:
    try:
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception as error:
        raise _Unavailable("validation_container_cleanup_failed") from error


def _read_file(container, path: str, cancelled, deadline) -> bytes:
    _check(cancelled, deadline)
    root, relative = next(((root, path[len(root) + 1:]) for root in (OUTPUT_ROOT, "/validation")
                           if path.startswith(root + "/")), (None, None))
    if root is None:
        raise _Failed("output_unsafe")
    outcome = container.exec_run(["/app/.venv/bin/python", "-I", "-c", _READ_FILE, root, relative],
                                 user=RUNTIME_USER, stdout=True, stderr=True, demux=True)
    _check(cancelled, deadline)
    stdout, stderr = outcome.output
    if outcome.exit_code != 0:
        reason = (stderr or b"").decode("ascii", "replace").strip()
        if reason not in {"output_missing", "output_unsafe", "output_not_regular_file", "output_file_limit", "output_changed"}:
            reason = "output_read_failed"
        raise _Failed(reason + ":" + PurePosixPath(path).name)
    if stderr or not isinstance(stdout, (bytes, type(None))) or len(stdout or b"") > MAX_FILE_BYTES:
        raise _Failed("output_read_failed")
    return stdout or b""


def _output_is_empty(container, cancelled, deadline) -> bool:
    """Read only fixed output-root metadata after the generated process exits."""
    _check(cancelled, deadline)
    outcome = container.exec_run(["/app/.venv/bin/python", "-I", "-c", _OUTPUT_EMPTY, OUTPUT_ROOT],
                                 user=RUNTIME_USER, stdout=True, stderr=True, demux=True)
    _check(cancelled, deadline)
    stdout, stderr = outcome.output
    if outcome.exit_code != 0 or stderr or stdout not in {b"empty\n", b"nonempty\n"}:
        raise _Failed("output_presence_unconfirmed")
    return stdout == b"empty\n"


def _capture_receipt(container, request, cancelled, deadline) -> dict:
    attached = container.attach_socket(params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1})
    try:
        wire = getattr(attached, "_sock", attached)
        wire.settimeout(.25)
        container.start()
        payload = _json(request)
        if len(payload) > 32768:
            raise _Unavailable("validation_arguments_limit")
        sent = 0
        while sent < len(payload):
            _check(cancelled, deadline)
            try:
                count = wire.send(payload[sent:])
            except socket.timeout:
                continue
            if not count:
                raise _Unavailable("validation_transport_closed")
            sent += count
        wire.shutdown(socket.SHUT_WR)

        def receive(length):
            value = bytearray()
            while len(value) < length:
                _check(cancelled, deadline)
                try:
                    chunk = wire.recv(min(length - len(value), 65536))
                except socket.timeout:
                    continue
                if not chunk:
                    raise _Failed("validation_runtime_stopped")
                value.extend(chunk)
            return value

        result = bytearray()
        captured = 0
        while b"\n" not in result:
            frame = receive(8)
            length = int.from_bytes(frame[4:8], "big")
            captured += length
            if frame[0] not in {1, 2} or frame[1:4] != b"\0\0\0" or captured > MAX_RECEIPT_BYTES:
                raise _Failed("validation_receipt_invalid")
            chunk = receive(length)
            if frame[0] == 1:
                result.extend(chunk)
        receipt = json.loads(result)
        if (set(receipt) not in ({"nonce", "exit_code", "reason", "stderr", "stderr_truncated"},
                                {"nonce", "exit_code", "reason", "stderr", "stderr_truncated", "runtime_exception"},
                                {"nonce", "exit_code", "reason", "stderr", "stderr_truncated", "runtime_exception", "dependency_exception"})
                or receipt["nonce"] != request["nonce"] or type(receipt["exit_code"]) is not int
                or receipt["reason"] not in {None, "program_timeout", "program_output_limit", "program_cleanup_failed", "program_exception_evidence_invalid"}
                or not isinstance(receipt["stderr"], str) or type(receipt["stderr_truncated"]) is not bool
                or receipt.get("runtime_exception") is not None and (
                    not isinstance(receipt["runtime_exception"], str) or receipt["runtime_exception"] not in _RUNTIME_BUILD_TYPES
                    or receipt["exit_code"] != 1 or receipt["reason"] is not None)
                or "dependency_exception" in receipt and (not isinstance(receipt["dependency_exception"], str)
                    or receipt["dependency_exception"] not in _DEPENDENCY_TYPES
                    or receipt.get("runtime_exception") is not None
                    or receipt["exit_code"] != 1 or receipt["reason"] is not None)):
            raise _Failed("validation_receipt_invalid")
        return receipt
    except (ValueError, TypeError) as error:
        raise _Failed("validation_receipt_invalid") from error
    finally:
        attached.close()


def _same_value(actual, expected, *, csv_value=False) -> bool:
    value = expected.value
    if value is None:
        return actual == "" if csv_value else actual is None
    if type(value) in {int, float}:
        if (actual is None or type(actual) is bool or (not csv_value and type(actual) not in {int, float})
                or (csv_value and not actual.strip())):
            return False
        try:
            number = Decimal(str(actual))
            target = Decimal(str(value))
            tolerance = Decimal(str(expected.atol)) + Decimal(str(expected.rtol)) * abs(target)
            return number.is_finite() and abs(number - target) <= tolerance
        except (ValueError, TypeError, OverflowError, DecimalException):
            return False
    if type(value) is bool:
        return actual == str(value) if csv_value else type(actual) is bool and actual is value
    return actual == value and (csv_value or type(actual) is str)


def _brief(value):
    if isinstance(value, str):
        return value[:160]
    if value is None or type(value) in {int, bool}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return "non_scalar_or_nonfinite"


def _diagnostic(stderr: str) -> str | None:
    """Keep a synthetic interpreter error useful for repair, without paths."""
    lines = stderr.strip().splitlines()
    if not lines:
        return None
    last = lines[-1][:600]
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*(?:Error|Exception):", last):
        return None
    return re.sub(r"(?:https?://|/)[^\s'\"]+", "[synthetic path]", last)


def _exit_matches(expected, actual: int) -> bool:
    # Positive ordinary process rejection codes exclude POSIX signal results
    # and conventional timeout/launch failures. Exact contracts remain exact.
    return (0 < actual < 124) if expected == "nonzero" else actual == expected


def _interpreter_failure(stderr: str) -> bool:
    """A broken program is not evidence that invalid input was validated."""
    diagnostic = _diagnostic(stderr)
    return bool(diagnostic and diagnostic.split(":", 1)[0] in {
        "ModuleNotFoundError", "ImportError", "SyntaxError", "IndentationError", "TabError",
        "NameError", "UnboundLocalError", "AttributeError", "MemoryError", "RecursionError", "SystemError",
    })


def _repairable_runtime_error_type(case, exit_code, stderr):
    """Fixed build errors on expected-success cases, never semantic rejections.

    Environment failures, signals, timeouts and input-rejection cases do not
    authorize a reference implementation revision. The caller records this
    evidence only after read-only input hashes have actually been checked.
    """
    if case.expected_exit_code != 0 or not 0 < exit_code < 124:
        return None
    diagnostic = _diagnostic(stderr)
    kind = diagnostic.split(":", 1)[0] if diagnostic else None
    return kind if kind in {"TypeError", "NameError", "UnboundLocalError", "AttributeError"} else None


def _assertions(case, files: dict[str, bytes], *, output_empty=None, stderr=None) -> list[dict]:
    """Pure host comparison; missing != null and NaN/Inf never satisfy numbers."""
    checks = []
    for index, assertion in enumerate(case.assertions):
        prefix = f"assertion_{index + 1}"
        try:
            if assertion.type == "output_empty":
                if type(output_empty) is not bool:
                    raise ValueError("output_presence_unconfirmed")
                checks.append({"id": prefix, "passed": output_empty, "expected": True, "actual": output_empty})
                continue
            if assertion.type == "stderr_contains":
                if type(stderr) is not str:
                    raise ValueError("rejection_diagnostic_unconfirmed")
                matched = assertion.expected in stderr
                checks.append({"id": prefix, "passed": matched, "expected": assertion.expected, "actual": matched})
                continue
            raw = files[assertion.path].decode("utf-8-sig")
            if assertion.type == "csv_header":
                columns = next(csv.reader(io.StringIO(raw, newline=""), strict=True), [])
                if (not columns or len(columns) != len(set(columns))
                        or any(not name or "\x00" in name for name in columns)):
                    raise ValueError("missing_or_duplicate_column")
                checks.append({"id": prefix, "passed": columns == assertion.columns,
                               "expected": list(assertion.columns), "actual": columns})
            elif assertion.type == "csv_rows":
                reader = csv.DictReader(io.StringIO(raw, newline=""), strict=True)
                columns = reader.fieldnames or []
                if not columns or len(columns) != len(set(columns)) or not (set(assertion.match) | set(assertion.fields)) <= set(columns):
                    raise ValueError("missing_or_duplicate_column")
                rows = list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in rows):
                    raise ValueError("irregular_csv_row")
                def matched(row):
                    from app.domain.services.skills.skill_validation import ExpectedValue
                    return all(_same_value(row[key], ExpectedValue(value=value), csv_value=True)
                               for key, value in assertion.match.items())
                selected = [row for row in rows if matched(row)]
                checks.append({"id": prefix + ".count", "passed": len(selected) == assertion.count,
                               "expected": assertion.count, "actual": len(selected)})
                for row_index, row in enumerate(selected):
                    for column, expected in assertion.fields.items():
                        checks.append({"id": f"{prefix}.row_{row_index + 1}.{column}",
                                       "passed": _same_value(row[column], expected, csv_value=True),
                                       "expected": _brief(expected.value), "actual": _brief(row[column])})
            else:
                def pairs(values):
                    result = {}
                    for key, value in values:
                        if key in result:
                            raise ValueError("duplicate_json_key")
                        result[key] = value
                    return result
                def invalid(value):
                    raise ValueError("nonfinite_json")
                def finite_float(token):
                    number = float(token)
                    if not math.isfinite(number):
                        raise ValueError("nonfinite_json")
                    return number
                value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite_float)
                if assertion.pointer:
                    for token in assertion.pointer[1:].split("/"):
                        token = token.replace("~1", "/").replace("~0", "~")
                        if isinstance(value, list):
                            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                                raise ValueError("invalid_array_index")
                            value = value[int(token)]
                        elif isinstance(value, dict):
                            value = value[token]
                        else:
                            raise ValueError("missing_json_pointer")
                if assertion.type == "json_object_keys":
                    if not isinstance(value, dict):
                        raise ValueError("json_object_type_mismatch")
                    expected, actual = sorted(assertion.keys), sorted(value)
                    checks.append({"id": prefix, "passed": actual == expected,
                                   "expected": expected, "actual": actual})
                elif assertion.type == "json_array_length":
                    if not isinstance(value, list):
                        raise ValueError("json_array_type_mismatch")
                    checks.append({"id": prefix, "passed": len(value) == assertion.length,
                                   "expected": assertion.length, "actual": len(value)})
                else:
                    checks.append({"id": prefix, "passed": _same_value(value, assertion.expected),
                                   "expected": _brief(assertion.expected.value), "actual": _brief(value)})
        except (KeyError, IndexError, ValueError, UnicodeError, csv.Error, TypeError) as error:
            # Fixed structural codes let the author repair actual output,
            # without leaking arbitrary exception text or changing the oracle.
            reason = str(error)
            if reason not in {"nonfinite_json", "duplicate_json_key", "missing_json_pointer",
                              "invalid_array_index", "json_array_type_mismatch", "json_object_type_mismatch",
                              "missing_or_duplicate_column", "irregular_csv_row", "rejection_diagnostic_unconfirmed"}:
                reason = "missing_or_invalid_output"
            checks.append({"id": prefix, "passed": False, "reason": reason})
    return checks


def _run(scripts, plan, image, cancelled, public_output_contract=None):
    token = uuid.uuid4().hex
    labels = {"datalab.component": _COMPONENT, "datalab.validation.owner": token}
    volume_name = "datalab-skill-validation-" + token
    client = volume = seed = None
    results = []
    public_results = []
    status = "passed"
    failure = None
    try:
        client = docker.from_env(timeout=5)
        # Empty inherited image variables too; the helper receives no proxy,
        # application settings or credentials from its parent/backend process.
        inherited = client.images.get(image).attrs.get("Config", {}).get("Env", []) or []
        environment = {entry.split("=", 1)[0]: "" for entry in inherited}
        environment.update({"PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "HOME": OUTPUT_ROOT,
                            "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1",
                            "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
        common = dict(image=image, user=RUNTIME_USER, network_mode="none", read_only=True,
                      cap_drop=["ALL"], security_opt=helper_security_options(), mem_limit="512m",
                      memswap_limit="512m", nano_cpus=1_000_000_000, pids_limit=48,
                      environment=environment, labels=labels, log_config=docker.types.LogConfig(type="none"))
        files = {path: source.encode() for path, source in scripts.items()}
        fixtures = {}
        for script_index, script in enumerate(plan.scripts):
            for case_index, case in enumerate(script.cases):
                root = f"cases/s{script_index}-c{case_index}"
                fixtures[(script_index, case_index)] = _materialize(case, "/validation/" + root)
                files.update({root + "/" + path: data for path, data in fixtures[(script_index, case_index)].items()})
        archive = _tar(files)
        _check(cancelled, time.monotonic() + CASE_SECONDS)
        try:
            volume = client.volumes.create(name=volume_name, labels=labels)
        except Exception:
            volume = _owned_lookup(client.volumes, volume_name, token, volume=True)
            raise
        seed_name = volume_name + "-seed"
        try:
            seed = client.containers.create(name=seed_name, entrypoint=["/bin/true"],
                mounts=[docker.types.Mount(target="/validation", source=volume_name, type="volume", read_only=False)],
                **common)
            if seed.put_archive("/validation", archive) is not True:
                raise _Unavailable("synthetic_inputs_unavailable")
        except Exception:
            if seed is None:
                seed = _owned_lookup(client.containers, seed_name, token)
            raise
        finally:
            if seed is not None:
                _remove(seed)
                seed = None
        for script_index, script in enumerate(plan.scripts):
            if script.support == "unsupported":
                results.append({"script_path": script.script_path, "id": "not_run", "kind": "unsupported",
                                "status": "unsupported", "reason": "unsupported_validation_contract"})
                continue
            for case_index, case in enumerate(script.cases):
                _check(cancelled, time.monotonic() + CASE_SECONDS)
                result = {"script_path": script.script_path, "id": case.id, "kind": case.kind,
                          "status": "failed", "reason": "validation_not_completed"}
                public_rules = (public_rules_for_case(public_output_contract, script.script_path, case)
                                if public_output_contract is not None else [])
                public_checks = failed_public_case(public_rules, reason="public_execution_unconfirmed")
                container = None
                name = volume_name + f"-s{script_index}-c{case_index}"
                deadline = time.monotonic() + CASE_SECONDS
                try:
                    container = client.containers.create(name=name, entrypoint=["/usr/bin/timeout"],
                        command=["--signal=KILL", f"{CONTAINER_SECONDS}s", "/app/.venv/bin/python", "-I", "-c", _SUPERVISOR],
                        working_dir=OUTPUT_ROOT, stdin_open=True, tty=False,
                        mounts=[docker.types.Mount(target="/validation", source=volume_name, type="volume", read_only=True)],
                        tmpfs={OUTPUT_ROOT: "rw,noexec,nosuid,nodev,size=64m,mode=1777", "/tmp": "rw,noexec,nosuid,nodev,size=64m,mode=1777"},
                        **common)
                    root = f"/validation/cases/s{script_index}-c{case_index}"
                    receipt = _capture_receipt(container, {"nonce": uuid.uuid4().hex,
                        "script": "/validation/" + script.script_path, "argv": _argv(case, root)}, cancelled, deadline)
                    if receipt["reason"]:
                        raise _Failed(receipt["reason"])
                    result["expected_exit_code"] = case.expected_exit_code
                    result["actual_exit_code"] = receipt["exit_code"]
                    observes_diagnostic = any(item.type == "stderr_contains" for item in case.assertions)
                    exit_matches = _exit_matches(case.expected_exit_code, receipt["exit_code"])
                    observed_type = (receipt.get("runtime_exception") if case.expected_exit_code == 0
                                     and receipt["exit_code"] == 1 and receipt.get("runtime_exception") in _RUNTIME_BUILD_TYPES else None)
                    dependency_type = (receipt.get("dependency_exception") if receipt["exit_code"] == 1
                                       and receipt.get("dependency_exception") in _DEPENDENCY_TYPES else None)
                    repairable_type = observed_type or _repairable_runtime_error_type(case, receipt["exit_code"], receipt["stderr"])
                    runtime_failure = receipt.get("runtime_exception") in _RUNTIME_BUILD_TYPES or repairable_type is not None or (
                        receipt["exit_code"] != 0 and _interpreter_failure(receipt["stderr"]))
                    if observes_diagnostic and "Traceback (most recent call last):" in receipt["stderr"]:
                        runtime_failure = True
                    if not observes_diagnostic and dependency_type is None and (not exit_matches or runtime_failure) and (diagnostic := _diagnostic(receipt["stderr"])):
                        result["diagnostic"] = diagnostic
                    # Fail closed if readback changes, even though the mounted
                    # input is also enforced read-only by the Docker daemon.
                    for path, expected in fixtures[(script_index, case_index)].items():
                        if _digest(_read_file(container, root + "/" + path, cancelled, deadline)) != _digest(expected):
                            raise _Failed("synthetic_input_changed")
                    result["input_unchanged"] = True
                    if dependency_type is not None:
                        result["runtime_dependency_evidence"] = {"version": 1, "origin": "host_import_exception",
                                                                 "exception_type": dependency_type}
                        raise _Failed("dependency_unavailable")
                    if repairable_type is not None:
                        result["runtime_error_type"] = repairable_type
                    if observed_type is not None:
                        result["runtime_build_evidence"] = {"version": 1, "origin": "host_uncaught_exception",
                                                            "exception_type": observed_type}
                    if runtime_failure:
                        raise _Failed("program_runtime_failure")
                    if case.expected_exit_code != 0 and not exit_matches:
                        # A success, signal or wrong exit code is not the
                        # ordinary rejection required by a safety postcondition.
                        raise _Failed("exit_code_mismatch")
                    if observes_diagnostic and receipt.get("stderr_truncated", False):
                        # A truncated stream cannot establish a clean rejection:
                        # its missing prefix might contain an unhandled traceback.
                        raise _Failed("rejection_diagnostic_unconfirmed")
                    output_empty = (_output_is_empty(container, cancelled, deadline)
                                    if any(item.type == "output_empty" for item in case.assertions) else None)
                    output = {}
                    for path in dict.fromkeys(assertion.path for assertion in case.assertions
                                              if assertion.type not in {"output_empty", "stderr_contains"}):
                        output[path] = _read_file(container, OUTPUT_ROOT + "/" + path, cancelled, deadline)
                        if sum(map(len, output.values())) > MAX_OUTPUT_BYTES:
                            raise _Failed("validation_output_limit")
                    if public_rules:
                        public_checks = []
                        for rule in public_rules:
                            if receipt["exit_code"] != 0:
                                public_checks.extend(failed_public_case([rule], reason="public_execution_failed"))
                                continue
                            try:
                                path = public_output_path(rule, case)
                            except PublicOutputContractError:
                                public_checks.extend(failed_public_case([rule], reason="public_output_binding_unknown"))
                                continue
                            try:
                                if path not in output:
                                    output[path] = _read_file(container, OUTPUT_ROOT + "/" + path, cancelled, deadline)
                            except _Failed:
                                public_checks.extend(failed_public_case([rule], reason="public_output_unavailable"))
                                continue
                            if sum(map(len, output.values())) > MAX_OUTPUT_BYTES:
                                public_checks.extend(failed_public_case([rule], reason="public_output_unavailable"))
                                raise _Failed("validation_output_limit")
                            public_checks.append(check_public_output(rule, output[path]))
                    checks = _assertions(case, output, output_empty=output_empty, stderr=receipt["stderr"])
                    result["checks"] = checks
                    result["output_sha256"] = {path: _digest(data) for path, data in output.items()}
                    public_passed = all(item["status"] != "failed" for item in public_checks)
                    passed = exit_matches and all(item["passed"] for item in checks) and public_passed
                    result.update(status="passed" if passed else "failed",
                                  reason="expectations_matched" if passed else "exit_code_mismatch" if not exit_matches
                                  else "public_output_contract_mismatch" if not public_passed else "assertion_mismatch")
                except _Failed as error:
                    result["reason"] = str(error)
                except _Cancelled:
                    raise
                except Exception as error:
                    if container is None:
                        container = _owned_lookup(client.containers, name, token)
                    raise _Unavailable("validation_runtime_unavailable") from error
                finally:
                    if container is not None:
                        _remove(container)
                results.append(result)
                if public_rules:
                    public_results.append({"script_path": script.script_path, "id": case.id,
                        "status": public_case_status(public_checks), "checks": public_checks})
    except _Cancelled:
        raise
    except Exception as error:
        status, failure = "unavailable", str(error) if isinstance(error, _Unavailable) else "validation_runtime_unavailable"
    finally:
        try:
            if seed is not None:
                _remove(seed)
            if volume is not None:
                volume.remove()
        except Exception:
            status, failure = "unavailable", "validation_cleanup_failed"
        finally:
            if client is not None:
                client.close()
    if status != "unavailable":
        status = "failed" if any(item["status"] == "failed" for item in results) else "unsupported" if any(item["status"] == "unsupported" for item in results) else "passed"
    result = {"status": status, "scope": "synthetic", "cases": results}
    if public_output_contract is not None:
        public_receipt = public_contract_receipt(public_output_contract, plan, public_results)
        result["public_output_verification"] = public_receipt
        if status != "unavailable" and public_receipt["status"] == "failed":
            result["status"] = "failed"
    if failure:
        result["reason"] = failure
    return result


async def validate_skill_scripts(scripts: dict[str, str], plan, settings=None, *, public_output_contract=None) -> dict:
    """Return host-owned execution evidence; cancellation waits for cleanup."""
    plan = SyntheticValidationPlan.model_validate(plan.model_dump() if hasattr(plan, "model_dump") else plan)
    if (len({item.script_path for item in plan.scripts}) != len(plan.scripts)
            or set(scripts) != {item.script_path for item in plan.scripts}
            or any(not _SCRIPT.fullmatch(path) or not isinstance(source, str) for path, source in scripts.items())):
        raise ValueError("Each generated helper needs exactly one validation plan")
    metadata = {"source_sha256": {path: _digest(source.encode()) for path, source in scripts.items()},
                "plan_sha256": _digest(_json(plan.model_dump()))}
    if public_output_contract is not None:
        public_output_contract = PublicOutputContract.model_validate(public_output_contract.model_dump()
            if hasattr(public_output_contract, "model_dump") else public_output_contract)
        if not {rule.script_path for rule in public_output_contract.rules} <= set(scripts):
            raise ValueError("Public output rules must belong to declared scripts")
    if not scripts or all(item.support == "unsupported" for item in plan.scripts):
        return {"status": "unsupported", "scope": "synthetic", "cases": [],
                "reason": "no_supported_scripts", **metadata,
                **({"public_output_verification": public_contract_receipt(public_output_contract, plan, [])}
                   if public_output_contract is not None else {})}
    image = getattr(settings or get_settings(), "sandbox_image", None)
    if not image:
        return {"status": "unavailable", "scope": "synthetic", "cases": [],
                "reason": "sandbox_image_not_configured", **metadata}
    cancelled = threading.Event()
    arguments = (scripts, plan, image, cancelled)
    if public_output_contract is not None:
        arguments += (public_output_contract,)
    task = asyncio.create_task(asyncio.to_thread(_run, *arguments))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated stop requests must not detach cleanup from the
                # owned worker thread or let it create another case later.
                continue
            except _Cancelled:
                break
        if task.done() and not task.cancelled():
            try:
                task.result()
            except _Cancelled:
                pass
        raise
    return {**result, **metadata}
