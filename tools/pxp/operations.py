#!/usr/bin/env python3
"""Bounded Igor Pro Packed Experiment (PXP) operators."""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

MAX_BYTES = 512 * 1024 * 1024
MAX_POINTS = 2_000_000


def fail(message: str) -> None:
    print(json.dumps({"success": False, "error": message}, ensure_ascii=False))
    raise SystemExit(0)


def decode(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if isinstance(value, np.ndarray):
        if value.dtype.kind in {"S", "U"}:
            return "".join(decode(item) for item in value.flat).split("\0", 1)[0]
        if value.dtype.kind in {"i", "u"}:
            return bytes(int(item) for item in value.flat if int(item)).decode("utf-8", errors="replace")
    return str(value or "")


def jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<bounded>"
    if isinstance(value, dict):
        return {decode(k): jsonable(v, depth + 1) for k, v in list(value.items())[:500]}
    if isinstance(value, (list, tuple)):
        return [jsonable(item, depth + 1) for item in value[:500]]
    if isinstance(value, np.ndarray):
        return value.tolist() if value.size <= 500 else {"shape": list(value.shape), "dtype": str(value.dtype), "size": int(value.size)}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return decode(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_pxp(path_value: str):
    path = Path(path_value)
    if not path.is_file():
        fail("PXP 文件不存在")
    if path.suffix.casefold() != ".pxp":
        fail("输入文件不是 .pxp")
    if path.stat().st_size > MAX_BYTES:
        fail("PXP 文件超过 512 MB 安全限制")
    try:
        from igor2 import packed
        records, filesystem = packed.load(str(path), ignore_unknown=True)
    except Exception as exc:
        fail(f"无法解析 Igor Pro PXP 文件: {exc}")
    return path, records, filesystem


def is_wave(value: Any) -> bool:
    return hasattr(value, "wave") and isinstance(getattr(value, "wave", None), dict)


def walk(node: dict[str, Any], prefix: str = "root"):
    for raw_name, value in node.items():
        name = decode(raw_name)
        current = f"{prefix}:{name}" if prefix else name
        if is_wave(value):
            yield "wave", current, value
        elif isinstance(value, dict):
            yield "folder", current, value
            yield from walk(value, current)
        else:
            yield "variable", current, value


def wave_payload(record: Any):
    packed_wave = record.wave
    body = packed_wave.get("wave", packed_wave)
    header = body.get("wave_header", {})
    data = np.asarray(body.get("wData", []))
    return packed_wave, body, header, data


def igor_time(value: Any) -> str | None:
    try:
        seconds = int(value)
        if seconds <= 0:
            return None
        return (datetime(1904, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()
    except Exception:
        return None


def units(body: dict[str, Any], header: dict[str, Any]):
    data_unit = decode(body.get("data_units")) or decode(header.get("dataUnits"))
    extended = body.get("dimension_units")
    dimension_units: list[str] = []
    if isinstance(extended, (list, tuple, np.ndarray)):
        dimension_units = [decode(item) for item in extended]
    if not any(dimension_units):
        raw = header.get("dimUnits", [])
        if isinstance(raw, np.ndarray) and raw.ndim > 1:
            dimension_units = [decode(row) for row in raw]
        elif isinstance(raw, (list, tuple)):
            dimension_units = [decode(item) for item in raw]
    return data_unit, dimension_units


def describe_wave(path: str, record: Any) -> dict[str, Any]:
    packed_wave, body, header, data = wave_payload(record)
    sf_a = np.asarray(header.get("sfA", [1.0]), dtype=float).reshape(-1)
    sf_b = np.asarray(header.get("sfB", [0.0]), dtype=float).reshape(-1)
    data_unit, dimension_units = units(body, header)
    shape = list(data.shape)
    axis_count = shape[0] if shape else 0
    step = float(sf_a[0]) if sf_a.size and np.isfinite(sf_a[0]) else 1.0
    origin = float(sf_b[0]) if sf_b.size and np.isfinite(sf_b[0]) else 0.0
    return {
        "path": path,
        "name": decode(header.get("bname")) or path.rsplit(":", 1)[-1],
        "shape": shape,
        "point_count": int(data.size),
        "dtype": str(data.dtype),
        "numeric": bool(np.issubdtype(data.dtype, np.number)),
        "data_unit": data_unit,
        "dimension_units": dimension_units,
        "axis_origin": origin,
        "axis_step": step,
        "axis_start": origin,
        "axis_end": origin + step * max(axis_count - 1, 0),
        "creation_time": igor_time(header.get("creationDate")),
        "modified_time": igor_time(header.get("modDate")),
        "note": decode(body.get("note"))[:10000],
        "version": jsonable(packed_wave.get("version")),
    }


def all_waves(filesystem: dict[str, Any]):
    root = filesystem.get("root", filesystem)
    return [(path, value) for kind, path, value in walk(root) if kind == "wave"]


def select_wave(filesystem: dict[str, Any], args: dict[str, Any]):
    waves = all_waves(filesystem)
    if not waves:
        fail("PXP 中没有可解析的 wave")
    requested = args.get("wave_path")
    if requested:
        normalized = str(requested).casefold().strip(":")
        matches = [(path, record) for path, record in waves if path.casefold().strip(":") == normalized or path.rsplit(":", 1)[-1].casefold() == normalized]
        if len(matches) != 1:
            fail("wave_path 未唯一匹配，请先调用 pxp_list_waves 获取完整路径")
        return matches[0]
    index = int(args.get("wave_index", 0))
    if index < 0 or index >= len(waves):
        fail(f"wave_index 超出范围，当前共有 {len(waves)} 个 wave")
    return waves[index]


def series(filesystem: dict[str, Any], args: dict[str, Any]):
    path, record = select_wave(filesystem, args)
    meta = describe_wave(path, record)
    _, _, _, data = wave_payload(record)
    if not np.issubdtype(data.dtype, np.number):
        fail("选中的 wave 不是数值 wave")
    values = np.abs(data) if np.iscomplexobj(data) else data.astype(float, copy=False)
    values = np.squeeze(values)
    if values.ndim == 0:
        values = values.reshape(1)
    elif values.ndim > 1:
        channel = int(args.get("channel_index", 0))
        matrix = values.reshape(values.shape[0], -1)
        if channel >= matrix.shape[1]:
            fail(f"channel_index 超出范围，当前 wave 有 {matrix.shape[1]} 个扁平通道")
        values = matrix[:, channel]
        meta["selected_channel"] = channel
    if values.size > MAX_POINTS:
        fail("wave 超过 2,000,000 点处理限制，请先提取或重采样")
    x = meta["axis_origin"] + meta["axis_step"] * np.arange(values.size, dtype=float)
    return path, meta, x, values.astype(float, copy=False)


def emit(payload: dict[str, Any], output_path: str | None = None):
    result = {"success": True, **payload}
    write_json(output_path, result)
    print(json.dumps(jsonable(result), ensure_ascii=False))


def records_payload(x: np.ndarray, y: np.ndarray, limit: int = 200000):
    count = min(len(x), limit)
    return [{"x": float(x[i]), "value": None if not np.isfinite(y[i]) else float(y[i])} for i in range(count)]


def baseline_als(y: np.ndarray, lam: float, p: float, iterations: int = 10):
    from scipy import sparse
    from scipy.sparse.linalg import spsolve
    length = len(y)
    if length < 3:
        return np.zeros_like(y)
    differences = sparse.diags(
        [1.0, -2.0, 1.0],
        [0, 1, 2],
        shape=(length - 2, length),
        format="csc",
    )
    weights = np.ones(length)
    finite_y = np.nan_to_num(y, nan=float(np.nanmedian(y)))
    for _ in range(iterations):
        weight_matrix = sparse.spdiags(weights, 0, length, length)
        system = (weight_matrix + lam * differences.T @ differences).tocsc()
        baseline = spsolve(system, weights * finite_y)
        weights = p * (finite_y > baseline) + (1 - p) * (finite_y < baseline)
    return baseline


def detect_peaks(x: np.ndarray, y: np.ndarray, args: dict[str, Any]):
    from scipy.signal import find_peaks, peak_widths
    finite = np.nan_to_num(y, nan=float(np.nanmedian(y)))
    prominence = args.get("prominence")
    if prominence is None:
        prominence = max(float(np.nanstd(finite)) * 0.5, np.finfo(float).eps)
    indices, props = find_peaks(finite, prominence=float(prominence), distance=int(args.get("min_distance", 3)))
    widths = peak_widths(finite, indices, rel_height=0.5)[0] if len(indices) else []
    step = float(np.nanmedian(np.abs(np.diff(x)))) if len(x) > 1 else 1.0
    peaks = [{
        "index": int(index), "x": float(x[index]), "value": float(y[index]),
        "prominence": float(props["prominences"][offset]), "fwhm": float(widths[offset] * step),
    } for offset, index in enumerate(indices)]
    return peaks, float(prominence)


def safe_name(value: str):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "wave"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_note(note: str) -> dict[str, str]:
    """Parse common Igor note key/value conventions without inventing semantics."""
    values: dict[str, str] = {}
    for fragment in re.split(r"[\r\n;]+", note or ""):
        fragment = fragment.strip()
        if not fragment:
            continue
        match = re.match(r"^([^:=\t]{1,100})\s*[:=\t]\s*(.{0,2000})$", fragment)
        if match:
            values[match.group(1).strip()] = match.group(2).strip()
    return values


def variable_inventory(filesystem: dict[str, Any]) -> list[dict[str, Any]]:
    root = filesystem.get("root", filesystem)
    return [
        {"path": item_path, "value": jsonable(value)}
        for kind, item_path, value in walk(root)
        if kind == "variable"
    ]


def selected_series(filesystem: dict[str, Any], paths: list[str]):
    return [series(filesystem, {"wave_path": item}) for item in paths]


def aligned_series(items):
    if len(items) < 2:
        fail("至少需要两个 wave")
    low = max(float(min(item[2][0], item[2][-1])) for item in items)
    high = min(float(max(item[2][0], item[2][-1])) for item in items)
    if low >= high:
        fail("所选 wave 没有公共坐标范围")
    step = max(abs(float(item[1]["axis_step"])) for item in items)
    if step <= 0:
        fail("wave 坐标步长无效")
    grid = np.arange(low, high + step * 0.5, step)
    if len(grid) > MAX_POINTS:
        fail("对齐结果超过 2,000,000 点限制")
    aligned = []
    for wave_path, meta, x, y in items:
        order = np.argsort(x)
        aligned.append((wave_path, meta, np.interp(grid, x[order], y[order])))
    return grid, aligned


def transformed_series(y: np.ndarray, x: np.ndarray, args: dict[str, Any]):
    result = y.astype(float, copy=True)
    stages = [{"name": "original", "values": result.copy()}]
    if args.get("baseline_correct", False):
        result = result - baseline_als(
            result,
            float(args.get("lambda", 100000)),
            float(args.get("asymmetry", 0.01)),
        )
        stages.append({"name": "baseline_corrected", "values": result.copy()})
    window = args.get("smooth_window")
    if window:
        from scipy.signal import savgol_filter
        size = int(window)
        size = min(size if size % 2 else size + 1, len(result) if len(result) % 2 else len(result) - 1)
        poly = int(args.get("polyorder", 3))
        if size < 3 or size <= poly:
            fail("wave 点数不足或平滑窗口无效")
        result = savgol_filter(result, size, poly)
        stages.append({"name": "smoothed", "values": result.copy()})
    method = args.get("normalize")
    if method:
        if method == "max":
            divisor = float(np.nanmax(np.abs(result)))
        elif method == "area":
            divisor = float(abs(np.trapz(np.nan_to_num(result), x)))
        elif method == "zscore":
            mean, divisor = float(np.nanmean(result)), float(np.nanstd(result))
            result = result - mean
        else:
            fail("不支持的归一化方法")
        if divisor:
            result = result / divisor
        stages.append({"name": f"normalized_{method}", "values": result.copy()})
    return result, stages


def spectral_quality(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    finite = np.isfinite(y)
    valid = y[finite]
    if len(valid) < 5:
        return {"score": 0.0, "grade": "invalid", "issues": ["有效点不足"]}
    differences = np.diff(valid)
    noise = float(np.median(np.abs(differences - np.median(differences))) / 0.6745 / math.sqrt(2)) if len(differences) else 0.0
    span = float(np.nanpercentile(valid, 99) - np.nanpercentile(valid, 1))
    snr = span / max(noise, np.finfo(float).eps)
    saturation = float(np.mean((valid == np.nanmin(valid)) | (valid == np.nanmax(valid))))
    spikes = int(np.sum(np.abs(differences - np.median(differences)) > max(8 * noise, np.finfo(float).eps)))
    missing = 1.0 - float(finite.mean())
    score = max(0.0, 100.0 - missing * 100 - min(saturation * 100, 25) - min(spikes / len(valid) * 500, 25) - (20 if snr < 5 else 0))
    issues = []
    if missing: issues.append("存在缺失值")
    if saturation > 0.05: issues.append("疑似信号饱和或截断")
    if spikes: issues.append("存在尖峰候选")
    if snr < 5: issues.append("信噪比较低")
    return {"score": round(score, 2), "grade": "good" if score >= 80 else "review" if score >= 60 else "poor", "valid_fraction": float(finite.mean()), "noise_estimate": noise, "signal_span": span, "snr_estimate": snr, "saturation_fraction": saturation, "spike_candidates": spikes, "issues": issues}


def peak_model(name: str):
    if name == "gaussian":
        return lambda axis, amplitude, center, width: amplitude * np.exp(-0.5 * ((axis - center) / width) ** 2)
    if name == "lorentzian":
        return lambda axis, amplitude, center, width: amplitude / (1 + ((axis - center) / width) ** 2)
    if name == "voigt":
        from scipy.special import voigt_profile
        return lambda axis, amplitude, center, width: amplitude * voigt_profile(axis - center, width, width)
    fail("峰模型必须为 gaussian、lorentzian 或 voigt")


def render_multi_plot(target: Path, grid: np.ndarray, curves, *, mode: str, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    target.parent.mkdir(parents=True, exist_ok=True)
    fig, axis = plt.subplots(figsize=(11, 7))
    if mode == "heatmap":
        matrix = np.vstack([item[2] for item in curves])
        image = axis.imshow(matrix, aspect="auto", origin="lower", extent=[grid[0], grid[-1], 0, len(curves)], cmap="viridis")
        axis.set_ylabel("Wave index"); fig.colorbar(image, ax=axis, label="Intensity")
    else:
        offset = 0.0
        if mode == "waterfall":
            spans = [np.nanpercentile(item[2], 95) - np.nanpercentile(item[2], 5) for item in curves]
            offset = max(spans) * 0.8 if spans else 0.0
        for index, (name, _, values) in enumerate(curves):
            axis.plot(grid, values + index * offset, linewidth=1.1, label=name)
        axis.legend(fontsize=8, loc="best")
    axis.set_xlabel("Coordinate"); axis.set_title(title); axis.grid(alpha=0.2); fig.tight_layout(); fig.savefig(target, dpi=160); plt.close(fig)
    if not target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        fail("未生成有效 PNG")


def main():
    if len(sys.argv) != 3:
        fail("operation and arguments are required")
    operation = sys.argv[1]
    try:
        args = json.loads(base64.urlsafe_b64decode(sys.argv[2] + "=" * (-len(sys.argv[2]) % 4)).decode())
    except Exception:
        fail("invalid arguments")
    path, records, filesystem = load_pxp(args.get("input_path", ""))
    waves = all_waves(filesystem)

    if operation == "pxp_inspect":
        items = list(walk(filesystem.get("root", filesystem)))
        emit({"file": path.name, "size_bytes": path.stat().st_size, "record_count": len(records), "folder_count": sum(k == "folder" for k, _, _ in items), "wave_count": len(waves), "variable_count": sum(k == "variable" for k, _, _ in items), "waves": [describe_wave(p, r) for p, r in waves[:200]]})
        return
    if operation == "pxp_validate":
        issues = []
        descriptions = []
        for wave_path, record in waves:
            try:
                description = describe_wave(wave_path, record)
                descriptions.append(description)
                if not description["point_count"]:
                    issues.append(f"空 wave: {wave_path}")
                if not description["numeric"]:
                    issues.append(f"非数值 wave: {wave_path}")
            except Exception as exc:
                issues.append(f"wave 解析失败 {wave_path}: {exc}")
        emit({"valid": bool(waves) and not issues, "record_count": len(records), "wave_count": len(waves), "issues": issues[:500], "waves_checked": len(descriptions)}, args.get("output_path"))
        return
    if operation == "pxp_folder_tree":
        entries = [{"type": kind, "path": item_path, "value": jsonable(value) if kind == "variable" else None} for kind, item_path, value in walk(filesystem.get("root", filesystem))]
        emit({"entries": entries}, args.get("output_path")); return
    if operation == "pxp_list_waves":
        emit({"wave_count": len(waves), "waves": [describe_wave(p, r) for p, r in waves]}, args.get("output_path")); return
    if operation == "pxp_extract_metadata":
        wave_path, record = select_wave(filesystem, args)
        emit({"wave": describe_wave(wave_path, record)}, args.get("output_path")); return
    if operation == "pxp_batch_export":
        output_dir = Path(args["output_dir"]); output_dir.mkdir(parents=True, exist_ok=True)
        exported = []
        for wave_index, (wave_path, _) in enumerate(waves[:int(args.get("max_waves", 100))]):
            try:
                _, meta, x, y = series(filesystem, {"wave_index": wave_index})
            except SystemExit:
                continue
            target = output_dir / f"{wave_index:04d}-{safe_name(wave_path)}.csv"
            with target.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream); writer.writerow([f"x [{(meta['dimension_units'] or [''])[0]}]", f"value [{meta['data_unit']}]"]); writer.writerows(zip(x, y))
            exported.append({"wave_path": wave_path, "file": target.name, "points": len(y)})
        manifest = {"success": True, "source": path.name, "exported_count": len(exported), "files": exported}
        write_json(str(output_dir / "pxp_export_manifest.json"), manifest); print(json.dumps(manifest, ensure_ascii=False)); return
    if operation == "pxp_extract_experiment_conditions":
        variables = variable_inventory(filesystem)
        notes = [{"wave_path": p, "fields": parse_note(describe_wave(p, r)["note"])} for p, r in waves]
        emit({"file": path.name, "variables": variables, "wave_notes": [item for item in notes if item["fields"]]}, args.get("output_path")); return
    if operation == "pxp_parse_wave_notes":
        parsed = [{"wave_path": p, "note": describe_wave(p, r)["note"], "fields": parse_note(describe_wave(p, r)["note"])} for p, r in waves]
        emit({"wave_count": len(parsed), "waves": parsed}, args.get("output_path")); return
    if operation == "pxp_associate_waves":
        groups: dict[str, list[dict[str, Any]]] = {}
        roles = {"x": "coordinate", "time": "coordinate", "background": "background", "bg": "background", "reference": "reference", "ref": "reference", "fit": "fit", "error": "uncertainty", "std": "uncertainty"}
        for wave_item, record in waves:
            meta = describe_wave(wave_item, record); stem = re.sub(r"(?i)(?:[_-]?(x|y|bg|background|ref|reference|fit|error|std))$", "", meta["name"])
            lowered = meta["name"].casefold(); role = next((value for key, value in roles.items() if re.search(rf"(?:^|[_-]){key}(?:$|[_-])", lowered)), "signal")
            folder = wave_item.rsplit(":", 1)[0]; key = f"{folder}:{stem.casefold()}"
            groups.setdefault(key, []).append({"wave_path": wave_item, "role": role, "shape": meta["shape"], "unit": meta["data_unit"]})
        emit({"group_count": len(groups), "groups": [{"group": key, "members": value} for key, value in groups.items()]}, args.get("output_path")); return
    if operation == "pxp_batch_process":
        output_dir = Path(args["output_dir"]); output_dir.mkdir(parents=True, exist_ok=True); exported = []
        requested = args.get("wave_paths") or [item[0] for item in waves[:int(args.get("max_waves", 100))]]
        for wave_item in requested:
            selected_path, meta, x, y = series(filesystem, {"wave_path": wave_item})
            processed, _ = transformed_series(y, x, args); target = output_dir / f"{safe_name(selected_path)}-processed.csv"
            with target.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream); writer.writerow(["x", "processed"]); writer.writerows(zip(x, processed))
            exported.append({"wave_path": selected_path, "file": target.name, "point_count": len(processed)})
        manifest = {"success": True, "operation": "pxp_batch_process", "parameters": {key: args.get(key) for key in ("baseline_correct", "lambda", "asymmetry", "smooth_window", "polyorder", "normalize")}, "files": exported}
        write_json(str(output_dir / "batch_process_manifest.json"), manifest); print(json.dumps(manifest, ensure_ascii=False)); return
    if operation in {"pxp_background_subtract", "pxp_internal_standard_normalize", "pxp_peak_drift", "pxp_replicate_statistics", "pxp_overlay_visualize", "pxp_waterfall_heatmap"}:
        requested = args.get("wave_paths") or [args.get("sample_wave_path"), args.get("background_wave_path")]
        requested = [item for item in requested if item]
        grid, aligned = aligned_series(selected_series(filesystem, requested))
        output_path = args.get("output_path")
        if operation == "pxp_background_subtract":
            scale = float(args.get("scale", 1.0)); result = aligned[0][2] - scale * aligned[1][2]
            emit({"sample_wave_path": aligned[0][0], "background_wave_path": aligned[1][0], "scale": scale, "data": records_payload(grid, result)}, output_path); return
        if operation == "pxp_internal_standard_normalize":
            low, high = sorted(map(float, args["standard_region"])); mask = (grid >= low) & (grid <= high)
            if mask.sum() < 2: fail("内标区间没有足够的数据点")
            standard = float(abs(np.trapz(aligned[1][2][mask], grid[mask]))) if args.get("method", "area") == "area" else float(np.nanmax(abs(aligned[1][2][mask])))
            if not standard: fail("内标响应为零，无法归一化")
            emit({"sample_wave_path": aligned[0][0], "standard_wave_path": aligned[1][0], "standard_region": [low, high], "standard_response": standard, "data": records_payload(grid, aligned[0][2] / standard)}, output_path); return
        if operation == "pxp_peak_drift":
            reference_peaks, _ = detect_peaks(grid, aligned[0][2], args); results = []
            tolerance = float(args.get("tolerance", max(abs(grid[1] - grid[0]) * 10, 1e-12)))
            for name, _, values in aligned[1:]:
                peaks, _ = detect_peaks(grid, values, args); matches = []
                for reference in reference_peaks:
                    candidates = [peak for peak in peaks if abs(peak["x"] - reference["x"]) <= tolerance]
                    if candidates:
                        match = min(candidates, key=lambda peak: abs(peak["x"] - reference["x"])); matches.append({"reference_x": reference["x"], "observed_x": match["x"], "shift": match["x"] - reference["x"]})
                results.append({"wave_path": name, "matches": matches})
            emit({"reference_wave_path": aligned[0][0], "tolerance": tolerance, "comparisons": results}, output_path); return
        if operation == "pxp_replicate_statistics":
            matrix = np.vstack([item[2] for item in aligned]); mean = np.nanmean(matrix, axis=0); std = np.nanstd(matrix, axis=0, ddof=1 if len(matrix) > 1 else 0); rsd = np.divide(std, np.abs(mean), out=np.full_like(std, np.nan), where=np.abs(mean) > np.finfo(float).eps) * 100
            emit({"wave_paths": [item[0] for item in aligned], "replicate_count": len(aligned), "data": [{"x": float(a), "mean": float(b), "std": float(c), "rsd_percent": None if not np.isfinite(d) else float(d)} for a, b, c, d in zip(grid, mean, std, rsd)]}, output_path); return
        mode = "overlay" if operation == "pxp_overlay_visualize" else args.get("mode", "waterfall"); target = Path(args["output_path"]); render_multi_plot(target, grid, aligned, mode=mode, title=args.get("title") or "PXP multi-wave visualization")
        emit({"mode": mode, "wave_count": len(aligned), "output_path": target.name, "size_bytes": target.stat().st_size}); return

    wave_path, meta, x, y = series(filesystem, args)
    output_path = args.get("output_path")
    if operation == "pxp_extract_wave":
        limit = min(int(args.get("max_points", 200000)), MAX_POINTS)
        emit({"wave": meta, "returned_points": min(len(y), limit), "truncated": len(y) > limit, "data": records_payload(x, y, limit)}, output_path); return
    if operation == "pxp_export_csv":
        target = Path(args["output_path"]); target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream); writer.writerow([f"x [{(meta['dimension_units'] or [''])[0]}]", f"value [{meta['data_unit']}]"]); writer.writerows(zip(x, y))
        emit({"wave_path": wave_path, "output_path": target.name, "point_count": len(y)}); return
    if operation == "pxp_wave_statistics":
        finite = y[np.isfinite(y)]
        stats = {"count": len(y), "valid_count": len(finite), "missing_count": int(len(y) - len(finite))}
        if len(finite):
            stats.update({"min": float(np.min(finite)), "max": float(np.max(finite)), "mean": float(np.mean(finite)), "std": float(np.std(finite)), "median": float(np.median(finite)), "p05": float(np.percentile(finite, 5)), "p95": float(np.percentile(finite, 95))})
        emit({"wave_path": wave_path, "statistics": stats, "units": meta["data_unit"]}, output_path); return
    if operation == "pxp_spectral_quality":
        emit({"wave_path": wave_path, "quality": spectral_quality(x, y)}, output_path); return
    if operation == "pxp_peak_ratio":
        regions = []
        for start, end in args["regions"]:
            low, high = sorted((float(start), float(end))); mask = (x >= low) & (x <= high)
            if mask.sum() < 2: fail("峰区间没有足够的数据点")
            response = float(abs(np.trapz(y[mask], x[mask]))) if args.get("method", "area") == "area" else float(np.nanmax(y[mask]))
            regions.append({"start": low, "end": high, "response": response})
        denominator = regions[int(args.get("denominator_index", 1))]["response"]
        if not denominator: fail("分母峰响应为零")
        emit({"wave_path": wave_path, "method": args.get("method", "area"), "regions": regions, "ratios_to_denominator": [item["response"] / denominator for item in regions]}, output_path); return
    if operation == "pxp_multipeak_deconvolution":
        from scipy.optimize import curve_fit
        model_name = args.get("model", "voigt"); component = peak_model(model_name); peaks, threshold = detect_peaks(x, y, args); centers = [float(value) for value in args.get("centers", [])] or [item["x"] for item in sorted(peaks, key=lambda item: item["prominence"], reverse=True)[:int(args.get("max_peaks", 8))]]
        if not centers: fail("没有检测到可拟合的峰")
        width = max(abs(float(meta["axis_step"])) * 3, (float(x[-1]) - float(x[0])) / max(len(x), 1))
        def combined(axis, *parameters):
            result = np.full_like(axis, parameters[-1], dtype=float)
            for index in range(len(centers)):
                result += component(axis, *parameters[index * 3:index * 3 + 3])
            return result
        initial = []
        baseline = float(np.nanpercentile(y, 5))
        for center in centers:
            nearest = int(np.argmin(abs(x - center))); initial.extend([max(float(y[nearest] - baseline), np.finfo(float).eps), center, width])
        initial.append(baseline); parameters, _ = curve_fit(combined, x, y, p0=initial, maxfev=50000); fitted = combined(x, *parameters); residual = y - fitted
        components = [{"amplitude": float(parameters[i * 3]), "center": float(parameters[i * 3 + 1]), "width": abs(float(parameters[i * 3 + 2]))} for i in range(len(centers))]
        emit({"wave_path": wave_path, "model": model_name, "prominence_threshold": threshold, "components": components, "rmse": float(np.sqrt(np.nanmean(residual ** 2))), "data": [{"x": float(a), "observed": float(b), "fitted": float(c), "residual": float(d)} for a, b, c, d in zip(x, y, fitted, residual)]}, output_path); return
    if operation == "pxp_before_after_visualize":
        processed, stages = transformed_series(y, x, args); target = Path(args["output_path"]); curves = [(stage["name"], meta, stage["values"]) for stage in stages]; render_multi_plot(target, x, curves, mode="overlay", title=args.get("title") or f"{meta['name']} processing comparison")
        emit({"wave_path": wave_path, "stage_count": len(stages), "output_path": target.name, "size_bytes": target.stat().st_size}); return
    if operation == "pxp_reproducible_package":
        target = Path(args["output_path"]); target.parent.mkdir(parents=True, exist_ok=True); staging = target.parent / f".{target.stem}-staging"
        if staging.exists(): shutil.rmtree(staging)
        staging.mkdir(); data_file = staging / "wave.csv"
        with data_file.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream); writer.writerow(["x", "value"]); writer.writerows(zip(x, y))
        source_hash = sha256_file(path); manifest = {"schema": "ai-dataseek/pxp-reproducible-package/v1", "source": {"filename": path.name, "sha256": source_hash, "size_bytes": path.stat().st_size}, "wave": meta, "parameters": args.get("processing_parameters", {}), "files": [{"path": "wave.csv", "sha256": sha256_file(data_file)}]}
        write_json(str(staging / "manifest.json"), manifest)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in staging.iterdir(): archive.write(item, item.name)
        shutil.rmtree(staging); emit({"wave_path": wave_path, "output_path": target.name, "size_bytes": target.stat().st_size, "source_sha256": source_hash}); return
    if operation == "pxp_baseline_correct":
        lam = float(args.get("lambda", 100000)); asymmetry = float(args.get("asymmetry", 0.01)); base = baseline_als(y, lam, asymmetry); corrected = y - base
        emit({"wave_path": wave_path, "method": "asymmetric_least_squares", "parameters": {"lambda": lam, "asymmetry": asymmetry}, "data": [{"x": float(a), "original": float(b), "baseline": float(c), "corrected": float(d)} for a, b, c, d in zip(x, y, base, corrected)]}, output_path); return
    if operation == "pxp_smooth":
        from scipy.signal import savgol_filter
        window = int(args.get("window", 11)); window = min(window if window % 2 else window + 1, len(y) if len(y) % 2 else len(y) - 1); poly = int(args.get("polyorder", 3))
        if window < 3 or window <= poly: fail("wave 点数不足或平滑窗口无效")
        result = savgol_filter(y, window, poly)
        emit({"wave_path": wave_path, "window": window, "polyorder": poly, "data": records_payload(x, result)}, output_path); return
    if operation == "pxp_normalize":
        method = args.get("method", "max")
        if method == "max": divisor = float(np.nanmax(np.abs(y))); result = y / divisor if divisor else y.copy()
        elif method == "area": divisor = float(abs(np.trapz(np.nan_to_num(y), x))); result = y / divisor if divisor else y.copy()
        else:
            mean = float(np.nanmean(y)); std = float(np.nanstd(y)); result = (y - mean) / std if std else y - mean
        emit({"wave_path": wave_path, "method": method, "data": records_payload(x, result)}, output_path); return
    if operation == "pxp_resample":
        step = float(args["step"]); start, end = sorted((float(x[0]), float(x[-1]))); new_x = np.arange(start, end + step * 0.5, step)
        if len(new_x) > MAX_POINTS: fail("重采样结果超过 2,000,000 点限制")
        order = np.argsort(x); new_y = np.interp(new_x, x[order], y[order])
        emit({"wave_path": wave_path, "step": step, "data": records_payload(new_x, new_y)}, output_path); return
    if operation == "pxp_derivative":
        order = int(args.get("order", 1)); result = y.copy()
        for _ in range(order): result = np.gradient(result, x)
        emit({"wave_path": wave_path, "order": order, "data": records_payload(x, result)}, output_path); return
    if operation == "pxp_peak_detect":
        peaks, prominence = detect_peaks(x, y, args)
        emit({"wave_path": wave_path, "peak_count": len(peaks), "prominence_threshold": prominence, "peaks": peaks}, output_path); return
    if operation == "pxp_peak_fit":
        from scipy.optimize import curve_fit
        peaks, prominence = detect_peaks(x, y, args); fitted = []
        def gaussian(axis, amplitude, center, sigma, offset): return offset + amplitude * np.exp(-0.5 * ((axis - center) / sigma) ** 2)
        for peak in peaks[:int(args.get("max_peaks", 50))]:
            index = peak["index"]; half_window = max(4, int(max(peak["fwhm"] / max(abs(meta["axis_step"]), 1e-12), 4)))
            left, right = max(0, index - half_window), min(len(y), index + half_window + 1); xx, yy = x[left:right], y[left:right]
            try:
                initial = [float(y[index] - np.nanmin(yy)), float(x[index]), max(float(peak["fwhm"]) / 2.355, abs(meta["axis_step"])), float(np.nanmin(yy))]
                parameters, _ = curve_fit(gaussian, xx, yy, p0=initial, maxfev=10000)
                predicted = gaussian(xx, *parameters); ss_res = float(np.sum((yy - predicted) ** 2)); ss_tot = float(np.sum((yy - np.mean(yy)) ** 2)); r2 = 1 - ss_res / ss_tot if ss_tot else None
                fitted.append({"center": float(parameters[1]), "amplitude": float(parameters[0]), "sigma": abs(float(parameters[2])), "fwhm": abs(float(parameters[2])) * 2.35482, "offset": float(parameters[3]), "r_squared": r2})
            except Exception as exc:
                fitted.append({"center_candidate": peak["x"], "fit_error": str(exc)})
        emit({"wave_path": wave_path, "model": "gaussian_plus_constant", "prominence_threshold": prominence, "fits": fitted}, output_path); return
    if operation == "pxp_integrate_regions":
        results = []
        for start, end in args["regions"]:
            low, high = sorted((float(start), float(end))); mask = (x >= low) & (x <= high); xx, yy = x[mask], y[mask]
            results.append({"start": low, "end": high, "point_count": int(mask.sum()), "area": float(np.trapz(yy, xx)) if len(xx) > 1 else None, "maximum": float(np.nanmax(yy)) if len(yy) else None})
        emit({"wave_path": wave_path, "regions": results}, output_path); return
    if operation == "pxp_compare_waves":
        selected = []
        for requested in args["wave_paths"]:
            _, selected_meta, selected_x, selected_y = series(filesystem, {"wave_path": requested})
            selected.append((requested, selected_meta, selected_x, selected_y))
        low = max(min(item[2][0], item[2][-1]) for item in selected); high = min(max(item[2][0], item[2][-1]) for item in selected)
        if low >= high: fail("所选 wave 没有公共坐标范围")
        step = max(abs(float(item[1]["axis_step"])) for item in selected); grid = np.arange(low, high + step * 0.5, step)
        aligned = []
        for name, _, xx, yy in selected:
            order = np.argsort(xx); aligned.append((name, np.interp(grid, xx[order], yy[order])))
        correlations = []
        for i in range(len(aligned)):
            for j in range(i + 1, len(aligned)):
                left, right = aligned[i], aligned[j]; delta = left[1] - right[1]
                correlations.append({"left": left[0], "right": right[0], "correlation": float(np.corrcoef(left[1], right[1])[0, 1]), "rmse": float(np.sqrt(np.mean(delta ** 2)))})
        emit({"common_range": [low, high], "step": step, "point_count": len(grid), "comparisons": correlations}, output_path); return
    if operation == "pxp_visualize":
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        target = Path(args["output_path"]); target.parent.mkdir(parents=True, exist_ok=True)
        fig, axis = plt.subplots(figsize=(10, 6)); axis.plot(x, y, linewidth=1.2, label=wave_path)
        if args.get("show_peaks"):
            peaks, _ = detect_peaks(x, y, args); axis.scatter([p["x"] for p in peaks], [p["value"] for p in peaks], s=18, color="#dc2626", label="峰候选")
        axis.set_xlabel((meta["dimension_units"] or ["坐标"])[0] or "坐标"); axis.set_ylabel(meta["data_unit"] or "数值"); axis.set_title(meta["name"]); axis.grid(alpha=0.25); axis.legend(); fig.tight_layout(); fig.savefig(target, dpi=160); plt.close(fig)
        if not target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"): fail("可视化未生成有效 PNG")
        emit({"wave_path": wave_path, "output_path": target.name, "size_bytes": target.stat().st_size}); return
    if operation == "pxp_export_report":
        finite = y[np.isfinite(y)]; peaks, prominence = detect_peaks(x, y, args)
        emit({"file": path.name, "container": {"record_count": len(records), "wave_count": len(waves)}, "wave": meta, "quality": {"point_count": len(y), "finite_count": len(finite), "missing_count": int(len(y) - len(finite))}, "statistics": {"min": float(np.min(finite)), "max": float(np.max(finite)), "mean": float(np.mean(finite)), "std": float(np.std(finite))} if len(finite) else {}, "peak_detection": {"prominence_threshold": prominence, "peak_count": len(peaks), "peaks": peaks[:200]}}, output_path); return
    fail(f"unknown operation: {operation}")


if __name__ == "__main__":
    main()
