#!/usr/bin/env python3
"""Bounded geoscience operators used by the extensible Tool plugins."""
from __future__ import annotations

import argparse, json, math, os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.mask import mask as raster_mask
from rasterio.merge import merge
from rasterio.warp import reproject
import xarray as xr
import geopandas as gpd
from rasterio.features import rasterize
from rasterio.mask import mask as clip_raster

MAX_VALUES = 20_000_000

class GeoScienceError(RuntimeError):
    pass

def json_value(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int)): return v
    if isinstance(v, (float, np.floating)): return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, np.integer): return int(v)
    if isinstance(v, (np.ndarray, list, tuple)): return [json_value(x) for x in v]
    if isinstance(v, dict): return {str(k): json_value(x) for k, x in v.items()}
    return str(v)

def result(operation: str, **payload: Any) -> dict[str, Any]:
    return json_value({"success": True, "operation": operation, **payload})

def output_path(raw: str) -> Path:
    root = Path(os.environ.get("AI_DATASEEK_OUTPUT_ROOT", "/home/ubuntu/output")).resolve()
    path = Path(raw).resolve()
    try: path.relative_to(root)
    except ValueError as exc: raise GeoScienceError(f"output path must be below {root}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def open_nc(path: Path) -> xr.Dataset:
    errors = []
    for engine in ("h5netcdf", "netcdf4", None):
        try:
            kw = {"decode_cf": True, "mask_and_scale": True}
            if engine: kw["engine"] = engine
            return xr.open_dataset(path, **kw)
        except Exception as exc: errors.append(f"{engine or 'default'}: {exc}")
    raise GeoScienceError("unable to open NetCDF: " + "; ".join(errors))

def choose_var(ds: xr.Dataset, name: str | None) -> str:
    if name:
        if name not in ds.data_vars: raise GeoScienceError(f"unknown variable: {name}")
        return name
    candidates = [n for n, v in ds.data_vars.items() if np.issubdtype(v.dtype, np.number) and v.ndim]
    if len(candidates) != 1: raise GeoScienceError("variable is required when multiple numeric variables exist")
    return candidates[0]

def role(name: str, v: xr.DataArray) -> str | None:
    n, std, axis = name.lower(), str(v.attrs.get("standard_name", "")).lower(), str(v.attrs.get("axis", "")).upper()
    if axis == "T" or std == "time" or n in {"time", "date", "datetime"}: return "time"
    if axis == "X" or std == "longitude" or n in {"lon", "longitude", "x"}: return "longitude"
    if axis == "Y" or std == "latitude" or n in {"lat", "latitude", "y"}: return "latitude"
    return None

def collection_inspect(args: argparse.Namespace) -> dict[str, Any]:
    paths = sorted(Path(args.input_dir).rglob("*") if args.input_dir else [Path(p) for p in args.input_paths])
    files = []
    for p in paths:
        if not p.is_file() or p.suffix.lower() not in {".nc", ".nc4", ".cdf", ".tif", ".tiff"}: continue
        item: dict[str, Any] = {"name": p.name, "suffix": p.suffix.lower(), "size_bytes": p.stat().st_size}
        if p.suffix.lower() in {".nc", ".nc4", ".cdf"}:
            with open_nc(p) as ds:
                item.update({"format": "netcdf", "dimensions": {k: int(v) for k, v in ds.sizes.items()}, "variables": list(ds.data_vars), "coordinates": list(ds.coords)})
                times = [n for n, v in ds.coords.items() if role(n, v) == "time"]
                if times: item["time_range"] = [str(ds[times[0]].values.min()), str(ds[times[0]].values.max())]
        else:
            with rasterio.open(p) as src: item.update({"format": "geotiff", "width": src.width, "height": src.height, "count": src.count, "crs": str(src.crs) if src.crs else None, "bounds": list(src.bounds), "resolution": list(src.res)})
        files.append(item)
    if not files: raise GeoScienceError("no supported NetCDF or GeoTIFF files found")
    return result("collection_inspect", file_count=len(files), files=files)

def coordinate_normalize(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path)
    with open_nc(path) as ds:
        rename = {}
        for n, v in ds.coords.items():
            r = role(n, v)
            if r and r not in ds.coords: rename[n] = r
        if rename: ds = ds.rename(rename)
        if "longitude" in ds.coords:
            lon = ds.longitude
            if float(lon.max()) > 180: ds = ds.assign_coords(longitude=((lon + 180) % 360) - 180).sortby("longitude")
        for r in ("latitude", "longitude"):
            if r in ds.coords and ds[r].ndim == 1 and ds[r].size > 1 and float(ds[r][1] - ds[r][0]) < 0: ds = ds.sortby(r)
        ds.to_netcdf(out)
        return result("coordinate_normalize", source=path.name, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}], coordinates=list(ds.coords))

def grid_signature(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in {".tif", ".tiff"}:
        with rasterio.open(path) as s: return {"format": "geotiff", "shape": [s.height, s.width], "crs": str(s.crs), "transform": list(s.transform), "bounds": list(s.bounds)}
    with open_nc(path) as ds:
        dims = {k: int(v) for k, v in ds.sizes.items()}
        coords = {}
        for n, v in ds.coords.items():
            if v.ndim == 1 and role(n, v) in {"latitude", "longitude"}: coords[role(n, v)] = {"name": n, "size": int(v.size), "first": json_value(v.values[0]), "last": json_value(v.values[-1])}
        return {"format": "netcdf", "dimensions": dims, "coordinates": coords}

def grid_compare(args: argparse.Namespace) -> dict[str, Any]:
    sigs = [grid_signature(Path(p)) for p in args.input_paths]
    compatible = all(s == sigs[0] for s in sigs[1:])
    return result("grid_compare", compatible=compatible, signatures=sigs, differences=[] if compatible else ["grid signatures differ"])

def quality_check(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path); checks: dict[str, Any] = {}
    if path.suffix.lower() in {".tif", ".tiff"}:
        with rasterio.open(path) as s:
            arr = s.read(1, masked=True); vals = arr.compressed(); checks.update({"shape": [s.height, s.width], "nodata_count": int(np.ma.getmaskarray(arr).sum()), "valid_count": int(vals.size), "minimum": json_value(np.min(vals)) if vals.size else None, "maximum": json_value(np.max(vals)) if vals.size else None})
    else:
        with open_nc(path) as ds:
            for n in ds.data_vars:
                v = ds[n]
                if not np.issubdtype(v.dtype, np.number): continue
                if v.size > MAX_VALUES: checks[n] = {"size": int(v.size), "sampled": True}; continue
                a = np.asarray(v.values); finite = a[np.isfinite(a)]
                checks[n] = {"size": int(v.size), "missing_count": int(np.isnan(a).sum()), "minimum": json_value(np.min(finite)) if finite.size else None, "maximum": json_value(np.max(finite)) if finite.size else None, "units": v.attrs.get("units")}
    return result("quality_check", source=path.name, checks=checks)

def unit_convert(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path)
    factors = {("kelvin", "celsius"): (1.0, -273.15), ("k", "c"): (1.0, -273.15), ("m", "mm"): (1000.0, 0.0), ("mm", "m"): (0.001, 0.0)}
    key = (args.from_unit.lower(), args.to_unit.lower())
    if key not in factors: raise GeoScienceError(f"unsupported conversion: {args.from_unit} to {args.to_unit}")
    factor, offset = factors[key]
    with open_nc(path) as ds:
        var = choose_var(ds, args.variable); ds[var] = ds[var] * factor + offset; ds[var].attrs["units"] = args.to_unit; ds.to_netcdf(out)
    return result("unit_convert", variable=var, from_unit=args.from_unit, to_unit=args.to_unit, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}])

def raster_stack(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with rasterio.open(paths[0]) as first:
        profile = first.profile.copy(); profile.update(count=len(paths), compress="deflate")
        with rasterio.open(out, "w", **profile) as dst:
            for i, p in enumerate(paths, 1):
                with rasterio.open(p) as src: dst.write(src.read(1), i); dst.set_band_description(i, p.stem)
    return result("raster_stack", input_count=len(paths), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def raster_mosaic(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]
    with rasterio.open(paths[0]) as first:
        data, transform = merge([rasterio.open(p) for p in paths]); profile = first.profile.copy(); profile.update(height=data.shape[1], width=data.shape[2], transform=transform, count=data.shape[0], compress="deflate")
    out = output_path(args.output_path)
    with rasterio.open(out, "w", **profile) as dst: dst.write(data)
    return result("raster_mosaic", input_count=len(paths), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def sample_raster(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        points = json.loads(args.points); values = [{"x": p[0], "y": p[1], "values": [json_value(v) for v in next(src.sample([p]))]} for p in points]
    return result("sample_raster", samples=values, crs=str(src.crs) if src.crs else None)

def qa_mask(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path); bit = int(args.bit)
    with rasterio.open(path) as src:
        data = src.read(); qa = data[0]; mask = ((qa.astype("uint64") >> bit) & 1) == 0; profile = src.profile.copy(); profile.update(count=data.shape[0], nodata=0)
        with rasterio.open(out, "w", **profile) as dst: dst.write(np.where(mask, data, 0).astype(data.dtype))
    return result("qa_mask", bit=bit, valid_fraction=float(mask.mean()), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def scene_composite(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with rasterio.open(paths[0]) as first:
        arrays = []
        for p in paths:
            with rasterio.open(p) as src: arrays.append(src.read(1, masked=True))
        data = np.ma.median(np.ma.stack(arrays), axis=0).filled(first.nodata if first.nodata is not None else 0); profile = first.profile.copy(); profile.update(count=1, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst: dst.write(data.astype(profile["dtype"]), 1)
    return result("scene_composite", input_count=len(paths), method="median", artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def temporal_stat(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with xr.open_mfdataset(paths, combine="by_coords", engine="h5netcdf") as ds:
        var = choose_var(ds, args.variable); arr = ds[var]
        if "time" not in arr.dims: raise GeoScienceError("time dimension is required")
        if mode == "climatology": value = arr.groupby("time.month").mean("time", skipna=True)
        elif mode == "anomaly": value = arr - arr.groupby("time.month").mean("time", skipna=True)
        else:
            t = np.arange(arr.sizes["time"], dtype=float); value = xr.DataArray(np.polyfit(t, arr.mean(dim=[d for d in arr.dims if d != "time"], skipna=True), 1)[0], name="trend")
        value.to_netcdf(out)
    return result(mode, variable=var, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}])

def artifact_validate(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path); checks = {"exists": path.is_file(), "size_bytes": path.stat().st_size if path.is_file() else 0}
    if not path.is_file() or path.stat().st_size == 0: raise GeoScienceError("artifact is missing or empty")
    try:
        if path.suffix.lower() in {".tif", ".tiff"}:
            with rasterio.open(path) as s: checks.update({"readable": True, "format": "geotiff", "shape": [s.height, s.width]})
        elif path.suffix.lower() in {".nc", ".nc4", ".cdf"}:
            with open_nc(path) as ds: checks.update({"readable": True, "format": "netcdf", "variables": list(ds.data_vars)})
        else: checks.update({"readable": True, "format": path.suffix.lower()})
    except Exception as exc: raise GeoScienceError(f"artifact cannot be read: {exc}") from exc
    return result("artifact_validate", artifact=str(path), checks=checks)

def vector_inspect(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.input_path)
    return result("vector_inspect", feature_count=len(gdf), crs=str(gdf.crs) if gdf.crs else None, geometry_types=sorted(set(gdf.geometry.geom_type.dropna())), bounds=list(gdf.total_bounds), columns=list(gdf.columns))

def vector_visualize(args: argparse.Namespace) -> dict[str, Any]:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    gdf = gpd.read_file(args.input_path)
    if len(gdf) > args.max_features:
        step = max(1, math.ceil(len(gdf) / args.max_features)); plotted = gdf.iloc[::step].head(args.max_features).copy(); sampled = True
    else: plotted = gdf; sampled = False
    if args.column and args.column not in plotted.columns: raise GeoScienceError(f"unknown vector attribute: {args.column}")
    if plotted.empty: raise GeoScienceError("vector layer has no features to render")
    out = output_path(args.output_path); fig, ax = plt.subplots(figsize=(10, 7), constrained_layout=True)
    geometry_types=set(plotted.geometry.geom_type.dropna()); point_only=bool(geometry_types) and all(name in {"Point","MultiPoint"} for name in geometry_types)
    kwargs: dict[str, Any] = {"ax": ax, "alpha": .85}
    if args.column: kwargs.update(column=args.column, cmap=args.cmap, legend=True)
    elif point_only: kwargs.update(color="#2563eb")
    else: kwargs.update(facecolor="#60a5fa", edgecolor="#1e3a8a", linewidth=.5)
    if point_only: kwargs.update(markersize=12)
    plotted.plot(**kwargs); ax.set_title(args.title or Path(args.input_path).stem); ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.grid(True, alpha=.2); ax.set_aspect("equal", adjustable="datalim")
    fig.savefig(out, dpi=160); plt.close(fig)
    return result("vector_visualize", feature_count=len(gdf), rendered_features=len(plotted), sampled=sampled, column=args.column, crs=str(gdf.crs) if gdf.crs else None, artifacts=[{"path":str(out),"type":"image/png","size_bytes":out.stat().st_size}])

def vector_transform(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.input_path)
    if args.target_crs: gdf = gdf.to_crs(args.target_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    out = output_path(args.output_path); gdf.to_file(out, driver="GeoJSON")
    return result("vector_transform", feature_count=len(gdf), crs=str(gdf.crs) if gdf.crs else None, artifacts=[{"path": str(out), "type": "application/geo+json", "size_bytes": out.stat().st_size}])

def zonal_statistics(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.vector_path)
    rows = []
    with rasterio.open(args.raster_path) as src:
        if gdf.crs and src.crs and gdf.crs != src.crs: gdf = gdf.to_crs(src.crs)
        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty: rows.append({"feature": idx, "count": 0}); continue
            data, _ = raster_mask(src, [geom.__geo_interface__], crop=False, filled=False)
            values = data[0].compressed()
            rows.append({"feature": idx, "count": int(values.size), "mean": json_value(np.mean(values)) if values.size else None, "minimum": json_value(np.min(values)) if values.size else None, "maximum": json_value(np.max(values)) if values.size else None})
    return result("zonal_statistics", zones=rows, zone_count=len(rows), raster=Path(args.raster_path).name)

def rasterize_vector(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.vector_path)
    with rasterio.open(args.reference_raster) as ref:
        shapes = [(geom, 1) for geom in gdf.geometry if geom is not None and not geom.is_empty]
        arr = rasterize(shapes, out_shape=(ref.height, ref.width), transform=ref.transform, fill=0, dtype="uint8")
        profile = ref.profile.copy(); profile.update(count=1, dtype="uint8", nodata=0, compress="deflate")
    out = output_path(args.output_path)
    with rasterio.open(out, "w", **profile) as dst: dst.write(arr, 1)
    return result("rasterize_vector", feature_count=len(shapes), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def grid_align(args: argparse.Namespace) -> dict[str, Any]:
    out = output_path(args.output_path)
    methods = {"nearest": Resampling.nearest, "bilinear": Resampling.bilinear, "cubic": Resampling.cubic, "average": Resampling.average}
    with rasterio.open(args.reference_path) as ref, rasterio.open(args.input_path) as src:
        profile = src.profile.copy(); profile.update(crs=ref.crs, transform=ref.transform, width=ref.width, height=ref.height, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(rasterio.band(src, band), rasterio.band(dst, band), src_transform=src.transform, src_crs=src.crs, dst_transform=ref.transform, dst_crs=ref.crs, resampling=methods[args.resampling])
    return result("grid_align", reference=Path(args.reference_path).name, resampling=args.resampling, artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def remote_sensing_product_inspect(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path)
    with rasterio.open(path) as src:
        metadata = {**src.tags()}; descriptions = list(src.descriptions); units = list(src.units)
        probe = (path.name + " " + " ".join(f"{k}={v}" for k, v in metadata.items())).lower()
        platform = next((p for p in ("sentinel-2", "sentinel-1", "landsat", "modis", "viirs") if p.replace("-", "") in probe.replace("-", "")), None)
        bands = [{"index": i, "description": descriptions[i-1], "unit": units[i-1]} for i in range(1, src.count + 1)]
    return result("remote_sensing_product_inspect", source=path.name, platform=platform, bands=bands, metadata=metadata, warnings=[] if platform else ["platform could not be identified from embedded metadata"])

def change_detection(args: argparse.Namespace) -> dict[str, Any]:
    out = output_path(args.output_path)
    with rasterio.open(args.before_path) as before, rasterio.open(args.after_path) as after:
        if (before.crs, before.transform, before.shape) != (after.crs, after.transform, after.shape): raise GeoScienceError("input rasters must be grid-aligned before change detection")
        a, b = before.read(args.band, masked=True).astype("float64"), after.read(args.band, masked=True).astype("float64")
        diff = b - a; profile = before.profile.copy(); profile.update(count=1, dtype="float32", nodata=-9999.0, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst: dst.write(diff.filled(-9999.0).astype("float32"), 1)
    vals = diff.compressed()
    return result("change_detection", mean_change=json_value(np.mean(vals)) if vals.size else None, minimum_change=json_value(np.min(vals)) if vals.size else None, maximum_change=json_value(np.max(vals)) if vals.size else None, artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def spatial_join(args: argparse.Namespace) -> dict[str, Any]:
    left, right = gpd.read_file(args.left_path), gpd.read_file(args.right_path)
    if left.crs and right.crs and left.crs != right.crs: right = right.to_crs(left.crs)
    joined = gpd.sjoin(left, right, how=args.how, predicate=args.predicate)
    out = output_path(args.output_path); joined.to_file(out, driver="GeoJSON")
    return result("spatial_join", feature_count=len(joined), predicate=args.predicate, artifacts=[{"path": str(out), "type": "application/geo+json", "size_bytes": out.stat().st_size}])

def transect_profile(args: argparse.Namespace) -> dict[str, Any]:
    points = json.loads(args.points)
    if len(points) < 2: raise GeoScienceError("transect requires at least two points")
    distances = np.linspace(0.0, 1.0, args.samples); segments = np.asarray(points, dtype=float)
    lengths = np.sqrt(((segments[1:] - segments[:-1]) ** 2).sum(axis=1)); cumulative = np.concatenate([[0.0], np.cumsum(lengths)]); total = cumulative[-1]
    coords = []
    for fraction in distances:
        target = fraction * total; index = min(int(np.searchsorted(cumulative, target, side="right") - 1), len(lengths)-1); local = 0.0 if lengths[index] == 0 else (target-cumulative[index])/lengths[index]; coords.append((segments[index] + local*(segments[index+1]-segments[index])).tolist())
    with rasterio.open(args.input_path) as src: values = [json_value(v[args.band-1]) for v in src.sample(coords)]
    return result("transect_profile", distances=json_value(distances * total), coordinates=coords, values=values, band=args.band)

def raster_histogram_quantiles(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        arr=src.read(args.band, masked=True).compressed()
    if not arr.size: raise GeoScienceError("raster contains no valid pixels")
    qs=np.percentile(arr, [1,5,25,50,75,95,99]); hist, edges=np.histogram(arr, bins=args.bins)
    return result("raster_histogram_quantiles", count=int(arr.size), minimum=json_value(arr.min()), maximum=json_value(arr.max()), mean=json_value(arr.mean()), standard_deviation=json_value(arr.std()), quantiles={str(q):json_value(v) for q,v in zip([1,5,25,50,75,95,99],qs)}, histogram_counts=hist.tolist(), histogram_edges=edges.tolist())

def raster_area_statistics(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        if not src.crs: raise GeoScienceError("CRS is required for area statistics")
        valid=~src.read(args.band, masked=True).mask; pixel_area=None
        if src.crs.is_projected: pixel_area=abs(src.transform.a*src.transform.e)
        else:
            # spherical cell area approximation for geographic rasters
            radius=6371008.8; ys=np.arange(src.height)+.5; lats=src.transform.f + ys*src.transform.e
            lat1=np.deg2rad(lats-src.transform.e/2); lat2=np.deg2rad(lats+src.transform.e/2); pixel_area=(radius**2)*np.deg2rad(abs(src.transform.a))*(np.sin(lat2)-np.sin(lat1))
        count=int(valid.sum()); area=float(np.sum(pixel_area*valid)) if np.ndim(pixel_area) else float(count*pixel_area)
    return result("raster_area_statistics", valid_pixels=count, area_square_units=area, crs=str(src.crs), method="projected_pixel_area" if np.ndim(pixel_area)==0 else "spherical_geographic_pixel_area")

def raster_focal_statistics(args: argparse.Namespace) -> dict[str, Any]:
    from scipy.ndimage import generic_filter
    with rasterio.open(args.input_path) as src:
        data=src.read(args.band, masked=True).filled(np.nan).astype(float)
    size=args.window
    funcs={"mean":np.nanmean,"median":np.nanmedian,"std":np.nanstd,"min":np.nanmin,"max":np.nanmax}
    if args.method not in funcs: raise GeoScienceError("unsupported focal method")
    out=generic_filter(data, funcs[args.method], size=size, mode="nearest")
    return result("raster_focal_statistics", method=args.method, window=size, minimum=json_value(np.nanmin(out)), maximum=json_value(np.nanmax(out)), mean=json_value(np.nanmean(out)))

def raster_cog_validate_convert(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        block=src.profile.get("tiled",False); overviews=src.overviews(1); compression=str(src.profile.get("compress"))
        valid=bool(block and overviews and compression not in {"None", "none"})
        if args.output_path:
            out=output_path(args.output_path); profile=src.profile.copy(); profile.update(tiled=True, compress=args.compression, BIGTIFF="IF_SAFER")
            with rasterio.open(out,"w",**profile) as dst:
                for b in range(1,src.count+1): dst.write(src.read(b),b)
                factors=[2,4,8,16]
                dst.build_overviews([f for f in factors if f < max(src.width,src.height)], Resampling.nearest); dst.update_tags(ns="rio_overview", resampling="nearest")
            return result("raster_cog_validate_convert", cog_before=valid, artifacts=[{"path":str(out),"type":"image/tiff","size_bytes":out.stat().st_size}])
    return result("raster_cog_validate_convert", cog_compliant=valid, tiled=block, overview_levels=overviews, compression=compression)

def raster_classification_compare(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.reference_path) as ref, rasterio.open(args.prediction_path) as pred:
        if (ref.crs, ref.transform, ref.shape) != (pred.crs, pred.transform, pred.shape): raise GeoScienceError("classification rasters must be grid-aligned")
        a=ref.read(args.reference_band, masked=True); b=pred.read(args.prediction_band, masked=True); valid=~(np.ma.getmaskarray(a)|np.ma.getmaskarray(b)); y=a.data[valid].astype(int); p=b.data[valid].astype(int)
    labels=sorted(set(y.tolist())|set(p.tolist())); matrix=np.zeros((len(labels),len(labels)),dtype=int); lookup={v:i for i,v in enumerate(labels)}
    for actual, predicted in zip(y,p): matrix[lookup[actual],lookup[predicted]] += 1
    total=int(matrix.sum()); accuracy=float(np.trace(matrix)/total) if total else None; ious=[]
    for i in range(len(labels)):
        union=matrix[i,:].sum()+matrix[:,i].sum()-matrix[i,i]; ious.append(float(matrix[i,i]/union) if union else None)
    return result("raster_classification_compare", labels=labels, confusion_matrix=matrix.tolist(), valid_pixels=total, overall_accuracy=accuracy, mean_iou=float(np.nanmean([x for x in ious if x is not None])) if any(x is not None for x in ious) else None, per_class_iou=ious)

def vector_schema_profile(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path); fields={}
    for name in gdf.columns:
        if name==gdf.geometry.name: continue
        series=gdf[name]; item={"dtype":str(series.dtype),"null_count":int(series.isna().sum()),"unique_count":int(series.nunique(dropna=True))}
        if pd.api.types.is_numeric_dtype(series): item.update(minimum=json_value(series.min()),maximum=json_value(series.max()),mean=json_value(series.mean()))
        elif item["unique_count"]<=args.max_categories: item["categories"]={str(k):int(v) for k,v in series.fillna("<NULL>").value_counts().head(args.max_categories).items()}
        fields[name]=item
    return result("vector_schema_profile",feature_count=len(gdf),fields=fields)

def vector_topology_validate(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path); geom=gdf.geometry
    invalid=~geom.is_valid; empty=geom.isna()|geom.is_empty; duplicates=geom.duplicated(keep=False)
    return result("vector_topology_validate",feature_count=len(gdf),valid=not bool((invalid|empty|duplicates).any()),invalid_geometry_count=int(invalid.sum()),empty_geometry_count=int(empty.sum()),duplicate_geometry_count=int(duplicates.sum()),invalid_indices=[json_value(v) for v in gdf.index[invalid].tolist()[:100]])

def vector_clip_overlay(args: argparse.Namespace) -> dict[str, Any]:
    left=gpd.read_file(args.input_path); right=gpd.read_file(args.overlay_path)
    if left.crs and right.crs and left.crs!=right.crs: right=right.to_crs(left.crs)
    if args.operation=="clip": output=gpd.clip(left,right)
    elif args.operation=="erase": output=gpd.overlay(left,right,how="difference",keep_geom_type=False)
    else: output=gpd.overlay(left,right,how=args.operation,keep_geom_type=False)
    target=output_path(args.output_path); output.to_file(target,driver="GeoJSON")
    return result("vector_clip_overlay",operation=args.operation,feature_count=len(output),artifacts=[{"path":str(target),"type":"application/geo+json","size_bytes":target.stat().st_size}])

def vector_dissolve_aggregate(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path)
    if args.field not in gdf.columns: raise GeoScienceError(f"unknown dissolve field: {args.field}")
    aggregations={name:args.method for name in args.aggregate_fields if name in gdf.columns}
    output=gdf.dissolve(by=args.field,aggfunc=aggregations or "first",as_index=False)
    target=output_path(args.output_path); output.to_file(target,driver="GeoJSON")
    return result("vector_dissolve_aggregate",feature_count=len(output),group_field=args.field,artifacts=[{"path":str(target),"type":"application/geo+json","size_bytes":target.stat().st_size}])

def vector_format_convert(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path); target=output_path(args.output_path); suffix=target.suffix.lower()
    drivers={".geojson":"GeoJSON",".gpkg":"GPKG",".shp":"ESRI Shapefile",".parquet":"Parquet"}
    if suffix not in drivers: raise GeoScienceError("output must be .geojson, .gpkg, .shp or .parquet")
    if suffix==".parquet": gdf.to_parquet(target,index=False)
    else: gdf.to_file(target,driver=drivers[suffix],encoding=args.encoding)
    return result("vector_format_convert",feature_count=len(gdf),format=drivers[suffix],crs=str(gdf.crs) if gdf.crs else None,artifacts=[{"path":str(target),"type":"application/octet-stream","size_bytes":target.stat().st_size}])

def raster_clip_by_vector(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.vector_path); target=output_path(args.output_path)
    with rasterio.open(args.input_path) as src:
        if gdf.crs and src.crs and gdf.crs!=src.crs: gdf=gdf.to_crs(src.crs)
        shapes=[g.__geo_interface__ for g in gdf.geometry if g is not None and not g.is_empty]
        data,transform=clip_raster(src,shapes,crop=True,all_touched=args.all_touched); profile=src.profile.copy(); profile.update(height=data.shape[1],width=data.shape[2],transform=transform,compress="deflate")
        with rasterio.open(target,"w",**profile) as dst: dst.write(data)
    return result("raster_clip_by_vector",shape=list(data.shape),artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def raster_calculator(args: argparse.Namespace) -> dict[str, Any]:
    paths=[Path(p) for p in args.input_paths]; target=output_path(args.output_path); arrays=[]
    with rasterio.open(paths[0]) as first:
        profile=first.profile.copy(); signature=(first.crs,first.transform,first.shape); arrays.append(first.read(args.band,masked=True).astype("float64"))
    for path in paths[1:]:
        with rasterio.open(path) as src:
            if (src.crs,src.transform,src.shape)!=signature: raise GeoScienceError("calculator inputs must be grid-aligned")
            arrays.append(src.read(args.band,masked=True).astype("float64"))
    value=arrays[0]
    for other in arrays[1:]:
        if args.operation=="add": value=value+other
        elif args.operation=="subtract": value=value-other
        elif args.operation=="multiply": value=value*other
        elif args.operation=="divide": value=np.ma.masked_invalid(value/other)
        elif args.operation=="minimum": value=np.ma.minimum(value,other)
        else: value=np.ma.maximum(value,other)
    nodata=-9999.0; profile.update(count=1,dtype="float32",nodata=nodata,compress="deflate")
    with rasterio.open(target,"w",**profile) as dst: dst.write(value.filled(nodata).astype("float32"),1)
    return result("raster_calculator",calculation=args.operation,input_count=len(paths),artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def raster_nodata_normalize(args: argparse.Namespace) -> dict[str, Any]:
    target=output_path(args.output_path)
    with rasterio.open(args.input_path) as src:
        data=src.read(masked=True).astype("float64"); invalid=np.ma.getmaskarray(data)|~np.isfinite(data.data)
        if args.minimum is not None: invalid|=data.data<args.minimum
        if args.maximum is not None: invalid|=data.data>args.maximum
        profile=src.profile.copy(); profile.update(dtype="float32",nodata=args.nodata,compress="deflate")
        with rasterio.open(target,"w",**profile) as dst: dst.write(np.where(invalid,args.nodata,data.data).astype("float32"))
    return result("raster_nodata_normalize",invalid_pixels=int(invalid.sum()),nodata=args.nodata,artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def raster_reclassify(args: argparse.Namespace) -> dict[str, Any]:
    rules=json.loads(args.rules); target=output_path(args.output_path)
    with rasterio.open(args.input_path) as src:
        source=src.read(args.band,masked=True); classified=np.full(source.shape,args.default,dtype="int32")
        for rule in rules:
            low,high,value=rule["min"],rule["max"],rule["value"]; classified[(source.data>=low)&(source.data<high)&~np.ma.getmaskarray(source)]=value
        profile=src.profile.copy(); profile.update(count=1,dtype="int32",nodata=args.nodata,compress="deflate")
        classified[np.ma.getmaskarray(source)]=args.nodata
        with rasterio.open(target,"w",**profile) as dst: dst.write(classified,1)
    return result("raster_reclassify",rule_count=len(rules),classes=sorted(np.unique(classified[classified!=args.nodata]).tolist()),artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def raster_scale_dtype_convert(args: argparse.Namespace) -> dict[str, Any]:
    target=output_path(args.output_path); dtype=np.dtype(args.dtype)
    with rasterio.open(args.input_path) as src:
        data=src.read(masked=True).astype("float64")*args.scale+args.offset; info=np.iinfo(dtype) if np.issubdtype(dtype,np.integer) else None
        if info: data=np.ma.clip(data,info.min,info.max)
        nodata=args.nodata if args.nodata is not None else (info.min if info else -9999.0); profile=src.profile.copy(); profile.update(dtype=str(dtype),nodata=nodata,compress="deflate")
        with rasterio.open(target,"w",**profile) as dst: dst.write(data.filled(nodata).astype(dtype))
    return result("raster_scale_dtype_convert",dtype=str(dtype),scale=args.scale,offset=args.offset,nodata=nodata,artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def netcdf_subset(args: argparse.Namespace) -> dict[str, Any]:
    """Subset by explicit dimension slices; bbox/time are optional and never guessed."""
    with open_nc(Path(args.input_path)) as ds:
        var = choose_var(ds, args.variable)
        dims = json.loads(args.slices) if args.slices else {}
        for dim, spec in dims.items():
            if dim not in ds.dims: raise GeoScienceError(f"unknown dimension: {dim}")
            if not isinstance(spec, list) or len(spec) not in (1, 2): raise GeoScienceError(f"slice for {dim} must be [value] or [start,end]")
            ds = ds.sel({dim: spec[0] if len(spec) == 1 else slice(spec[0], spec[1])})
        if args.bbox:
            west, south, east, north = json.loads(args.bbox)
            lon = next((n for n,v in ds.coords.items() if role(n,v)=="longitude"), None)
            lat = next((n for n,v in ds.coords.items() if role(n,v)=="latitude"), None)
            if not lon or not lat: raise GeoScienceError("unable to identify latitude/longitude coordinates")
            ds = ds.sel({lon: slice(west, east), lat: slice(south, north)})
        target = output_path(args.output_path); ds.to_netcdf(target)
        return result("netcdf_subset", variable=var, dimensions={k:int(v) for k,v in ds.sizes.items()}, artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def netcdf_time_aggregate(args: argparse.Namespace) -> dict[str, Any]:
    with open_nc(Path(args.input_path)) as ds:
        var = choose_var(ds, args.variable); time = next((n for n,v in ds.coords.items() if role(n,v)=="time"), None)
        if not time: raise GeoScienceError("time coordinate not found")
        grouped = getattr(ds[var].resample({time: args.frequency}), args.method)()
        target = output_path(args.output_path); grouped.to_dataset(name=var).to_netcdf(target)
        return result("netcdf_time_aggregate", variable=var, time_dimension=time, frequency=args.frequency, method=args.method, artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def netcdf_regrid(args: argparse.Namespace) -> dict[str, Any]:
    with open_nc(Path(args.input_path)) as ds:
        var = choose_var(ds, args.variable); lon = next((n for n,v in ds.coords.items() if role(n,v)=="longitude"), None); lat = next((n for n,v in ds.coords.items() if role(n,v)=="latitude"), None)
        if not lon or not lat: raise GeoScienceError("unable to identify latitude/longitude coordinates")
        lons = np.arange(float(ds[lon].min()), float(ds[lon].max()) + args.resolution/2, args.resolution); lats = np.arange(float(ds[lat].min()), float(ds[lat].max()) + args.resolution/2, args.resolution)
        out = ds.interp({lon:lons, lat:lats}, method=args.method); target=output_path(args.output_path); out.to_netcdf(target)
        return result("netcdf_regrid", variable=var, longitude=lon, latitude=lat, resolution=args.resolution, method=args.method, artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def netcdf_collection_diagnose(args: argparse.Namespace) -> dict[str, Any]:
    paths=[Path(p) for p in args.input_paths]; report=[]; seen=[]
    for p in paths:
        with open_nc(p) as ds:
            times=next((n for n,v in ds.coords.items() if role(n,v)=="time"),None); values=ds[times].values.tolist() if times else []
            report.append({"file":p.name,"dimensions":{k:int(v) for k,v in ds.sizes.items()},"variables":list(ds.data_vars),"time_count":len(values),"time_start":str(values[0]) if values else None,"time_end":str(values[-1]) if values else None}); seen.extend([str(v) for v in values])
    duplicates=len(seen)-len(set(seen)); return result("netcdf_collection_diagnose", files=report, duplicate_time_count=duplicates, total_time_count=len(seen))

def raster_band_semantics(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        bands=[]
        for i in range(1,src.count+1):
            desc=src.descriptions[i-1] or ""; tags=src.tags(i); text=" ".join([desc, str(tags)]).lower(); role_name=None
            for key, names in {"blue":["blue","b1","band 1"],"green":["green","b2","band 2"],"red":["red","b3","band 3"],"nir":["nir","near infrared","b4","band 4"],"swir1":["swir1","swir-1"],"swir2":["swir2","swir-2"]}.items():
                if any(n in text for n in names): role_name=key; break
            bands.append({"band":i,"description":desc,"tags":tags,"semantic_role":role_name})
        return result("raster_band_semantics",count=src.count,bands=bands,crs=str(src.crs) if src.crs else None)

def raster_index(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        if args.band_a<1 or args.band_b<1 or args.band_a>src.count or args.band_b>src.count: raise GeoScienceError("band out of range")
        a=src.read(args.band_a,masked=True).astype("float64"); b=src.read(args.band_b,masked=True).astype("float64"); denom=a+b; out=np.ma.masked_where(denom==0,(a-b)/denom); target=output_path(args.output_path); profile=src.profile.copy(); profile.update(count=1,dtype="float32",nodata=-9999.0,compress="deflate")
        with rasterio.open(target,"w",**profile) as dst: dst.write(out.filled(-9999.0).astype("float32"),1); dst.set_band_description(1,args.index_name)
        return result("raster_index",index=args.index_name,band_a=args.band_a,band_b=args.band_b,valid_pixels=int((~np.ma.getmaskarray(out)).sum()),artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def raster_rgb_composite(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        indexes=[args.red,args.green,args.blue]
        if any(i<1 or i>src.count for i in indexes): raise GeoScienceError("RGB band out of range")
        arr=np.stack([src.read(i,masked=True).filled(0).astype("float32") for i in indexes]); lo=np.percentile(arr,2,axis=(1,2),keepdims=True); hi=np.percentile(arr,98,axis=(1,2),keepdims=True); rgb=np.clip((arr-lo)/(hi-lo+1e-12)*255,0,255).astype("uint8"); target=output_path(args.output_path); profile=src.profile.copy(); profile.update(count=3,dtype="uint8",nodata=0,compress="deflate")
        with rasterio.open(target,"w",**profile) as dst: dst.write(rgb)
        return result("raster_rgb_composite",bands=indexes,stretch=[2,98],artifacts=[{"path":str(target),"type":"image/tiff","size_bytes":target.stat().st_size}])

def shapefile_package_validate(args: argparse.Namespace) -> dict[str, Any]:
    path=Path(args.input_path); base=path.with_suffix(""); required=[base.with_suffix(s) for s in (".shp",".shx",".dbf")]; optional=[base.with_suffix(s) for s in (".prj",".cpg")]; missing=[p.suffix for p in required if not p.exists()]
    if missing: raise GeoScienceError("missing required Shapefile components: "+", ".join(missing))
    gdf=gpd.read_file(path); return result("shapefile_package_validate",base=base.name,required_components={p.suffix:p.stat().st_size for p in required},optional_components={p.suffix:p.stat().st_size for p in optional if p.exists()},crs=str(gdf.crs) if gdf.crs else None,feature_count=len(gdf),encoding=(optional[1].read_text(errors="ignore").strip() if optional[1].exists() else None))

def vector_attribute_filter(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path); expression=args.expression
    try: selected=gdf.query(expression)
    except Exception as exc: raise GeoScienceError(f"invalid attribute expression: {exc}") from exc
    target=output_path(args.output_path); suffix=target.suffix.lower(); driver="ESRI Shapefile" if suffix==".shp" else "GeoJSON"; selected.to_file(target,driver=driver)
    return result("vector_attribute_filter",expression=expression,input_features=len(gdf),output_features=len(selected),artifacts=[{"path":str(target),"type":"application/geo+json" if driver=="GeoJSON" else "application/octet-stream","size_bytes":target.stat().st_size}])

def vector_geometry_repair(args: argparse.Namespace) -> dict[str, Any]:
    gdf=gpd.read_file(args.input_path); before=int((~gdf.geometry.is_valid).sum()); gdf=gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy(); gdf.geometry=gdf.geometry.make_valid(); target=output_path(args.output_path); gdf.to_file(target,driver="GeoJSON"); return result("vector_geometry_repair",invalid_before=before,feature_count=len(gdf),artifacts=[{"path":str(target),"type":"application/geo+json","size_bytes":target.stat().st_size}])

def main() -> int:
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="op", required=True)
    def common(name: str, input_paths=True):
        c = sub.add_parser(name)
        if input_paths: c.add_argument("input_paths", nargs="+")
        return c
    c = sub.add_parser("collection-inspect"); c.add_argument("--input-dir"); c.add_argument("--input-paths", nargs="*", default=[])
    c = sub.add_parser("coordinate-normalize"); c.add_argument("input_path"); c.add_argument("output_path")
    common("grid-compare"); common("raster-stack").add_argument("output_path"); common("raster-mosaic").add_argument("output_path")
    c = sub.add_parser("quality-check"); c.add_argument("input_path")
    c = sub.add_parser("unit-convert"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("from_unit"); c.add_argument("to_unit"); c.add_argument("--variable")
    c = sub.add_parser("sample-raster"); c.add_argument("input_path"); c.add_argument("points")
    c = sub.add_parser("qa-mask"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("bit", type=int)
    common("scene-composite").add_argument("output_path")
    for name in ("climatology", "anomaly", "trend"):
        c = common(name); c.add_argument("output_path"); c.add_argument("--variable")
    c = sub.add_parser("artifact-validate"); c.add_argument("input_path")
    c = sub.add_parser("vector-inspect"); c.add_argument("input_path")
    c = sub.add_parser("vector-visualize"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--column"); c.add_argument("--title"); c.add_argument("--cmap",default="viridis"); c.add_argument("--max-features",type=int,default=50000)
    c = sub.add_parser("vector-transform"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--target-crs")
    c = sub.add_parser("zonal-statistics"); c.add_argument("raster_path"); c.add_argument("vector_path")
    c = sub.add_parser("rasterize-vector"); c.add_argument("reference_raster"); c.add_argument("vector_path"); c.add_argument("output_path")
    c = sub.add_parser("grid-align"); c.add_argument("input_path"); c.add_argument("reference_path"); c.add_argument("output_path"); c.add_argument("--resampling", choices=("nearest","bilinear","cubic","average"), default="nearest")
    c = sub.add_parser("remote-product-inspect"); c.add_argument("input_path")
    c = sub.add_parser("change-detection"); c.add_argument("before_path"); c.add_argument("after_path"); c.add_argument("output_path"); c.add_argument("--band", type=int, default=1)
    c = sub.add_parser("spatial-join"); c.add_argument("left_path"); c.add_argument("right_path"); c.add_argument("output_path"); c.add_argument("--predicate", choices=("intersects","within","contains","touches"), default="intersects"); c.add_argument("--how", choices=("left","inner"), default="left")
    c = sub.add_parser("transect-profile"); c.add_argument("input_path"); c.add_argument("points"); c.add_argument("--samples", type=int, default=100); c.add_argument("--band", type=int, default=1)
    c = sub.add_parser("raster-histogram-quantiles"); c.add_argument("input_path"); c.add_argument("--band",type=int,default=1); c.add_argument("--bins",type=int,default=32)
    c = sub.add_parser("raster-area-statistics"); c.add_argument("input_path"); c.add_argument("--band",type=int,default=1)
    c = sub.add_parser("raster-focal-statistics"); c.add_argument("input_path"); c.add_argument("--method",choices=("mean","median","std","min","max"),default="mean"); c.add_argument("--window",type=int,default=3)
    c = sub.add_parser("raster-cog-validate-convert"); c.add_argument("input_path"); c.add_argument("--output-path"); c.add_argument("--compression",default="deflate")
    c = sub.add_parser("raster-classification-compare"); c.add_argument("reference_path"); c.add_argument("prediction_path"); c.add_argument("--reference-band",type=int,default=1); c.add_argument("--prediction-band",type=int,default=1)
    c = sub.add_parser("vector-schema-profile"); c.add_argument("input_path"); c.add_argument("--max-categories",type=int,default=30)
    c = sub.add_parser("vector-topology-validate"); c.add_argument("input_path")
    c = sub.add_parser("vector-clip-overlay"); c.add_argument("input_path"); c.add_argument("overlay_path"); c.add_argument("output_path"); c.add_argument("--operation",choices=("clip","intersection","union","erase","symmetric_difference"),default="clip")
    c = sub.add_parser("vector-dissolve-aggregate"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("field"); c.add_argument("--aggregate-fields",nargs="*",default=[]); c.add_argument("--method",choices=("sum","mean","min","max","first"),default="sum")
    c = sub.add_parser("vector-format-convert"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--encoding",default="UTF-8")
    c = sub.add_parser("raster-clip-by-vector"); c.add_argument("input_path"); c.add_argument("vector_path"); c.add_argument("output_path"); c.add_argument("--all-touched",action="store_true")
    c = sub.add_parser("raster-calculator"); c.add_argument("input_paths",nargs="+"); c.add_argument("output_path"); c.add_argument("--operation",choices=("add","subtract","multiply","divide","minimum","maximum"),required=True); c.add_argument("--band",type=int,default=1)
    c = sub.add_parser("raster-nodata-normalize"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--nodata",type=float,default=-9999.0); c.add_argument("--minimum",type=float); c.add_argument("--maximum",type=float)
    c = sub.add_parser("raster-reclassify"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("rules"); c.add_argument("--band",type=int,default=1); c.add_argument("--default",type=int,default=0); c.add_argument("--nodata",type=int,default=-9999)
    c = sub.add_parser("raster-scale-dtype-convert"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--dtype",choices=("uint8","uint16","int16","int32","float32","float64"),required=True); c.add_argument("--scale",type=float,default=1.0); c.add_argument("--offset",type=float,default=0.0); c.add_argument("--nodata",type=float)
    c = sub.add_parser("netcdf-subset"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--variable"); c.add_argument("--slices"); c.add_argument("--bbox")
    c = sub.add_parser("netcdf-time-aggregate"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--variable"); c.add_argument("--frequency",default="MS"); c.add_argument("--method",choices=("mean","sum","min","max","median"),default="mean")
    c = sub.add_parser("netcdf-regrid"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--variable"); c.add_argument("--resolution",type=float,required=True); c.add_argument("--method",choices=("nearest","linear"),default="linear")
    c = sub.add_parser("netcdf-collection-diagnose"); c.add_argument("input_paths",nargs="+")
    c = sub.add_parser("raster-band-semantics"); c.add_argument("input_path")
    c = sub.add_parser("raster-index"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--band-a",type=int,required=True); c.add_argument("--band-b",type=int,required=True); c.add_argument("--index-name",default="normalized_difference")
    c = sub.add_parser("raster-rgb-composite"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--red",type=int,required=True); c.add_argument("--green",type=int,required=True); c.add_argument("--blue",type=int,required=True)
    c = sub.add_parser("shapefile-package-validate"); c.add_argument("input_path")
    c = sub.add_parser("vector-attribute-filter"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("expression")
    c = sub.add_parser("vector-geometry-repair"); c.add_argument("input_path"); c.add_argument("output_path")
    a = p.parse_args()
    try:
        if a.op == "collection-inspect": r = collection_inspect(a)
        elif a.op == "coordinate-normalize": r = coordinate_normalize(a)
        elif a.op == "grid-compare": r = grid_compare(a)
        elif a.op == "quality-check": r = quality_check(a)
        elif a.op == "unit-convert": r = unit_convert(a)
        elif a.op == "raster-stack": r = raster_stack(a)
        elif a.op == "raster-mosaic": r = raster_mosaic(a)
        elif a.op == "sample-raster": r = sample_raster(a)
        elif a.op == "qa-mask": r = qa_mask(a)
        elif a.op == "scene-composite": r = scene_composite(a)
        elif a.op in {"climatology", "anomaly", "trend"}: r = temporal_stat(a, a.op)
        elif a.op == "vector-inspect": r = vector_inspect(a)
        elif a.op == "vector-visualize": r = vector_visualize(a)
        elif a.op == "vector-transform": r = vector_transform(a)
        elif a.op == "zonal-statistics": r = zonal_statistics(a)
        elif a.op == "rasterize-vector": r = rasterize_vector(a)
        elif a.op == "grid-align": r = grid_align(a)
        elif a.op == "remote-product-inspect": r = remote_sensing_product_inspect(a)
        elif a.op == "change-detection": r = change_detection(a)
        elif a.op == "spatial-join": r = spatial_join(a)
        elif a.op == "transect-profile": r = transect_profile(a)
        elif a.op == "raster-histogram-quantiles": r = raster_histogram_quantiles(a)
        elif a.op == "raster-area-statistics": r = raster_area_statistics(a)
        elif a.op == "raster-focal-statistics": r = raster_focal_statistics(a)
        elif a.op == "raster-cog-validate-convert": r = raster_cog_validate_convert(a)
        elif a.op == "raster-classification-compare": r = raster_classification_compare(a)
        elif a.op == "vector-schema-profile": r = vector_schema_profile(a)
        elif a.op == "vector-topology-validate": r = vector_topology_validate(a)
        elif a.op == "vector-clip-overlay": r = vector_clip_overlay(a)
        elif a.op == "vector-dissolve-aggregate": r = vector_dissolve_aggregate(a)
        elif a.op == "vector-format-convert": r = vector_format_convert(a)
        elif a.op == "raster-clip-by-vector": r = raster_clip_by_vector(a)
        elif a.op == "raster-calculator": r = raster_calculator(a)
        elif a.op == "raster-nodata-normalize": r = raster_nodata_normalize(a)
        elif a.op == "raster-reclassify": r = raster_reclassify(a)
        elif a.op == "raster-scale-dtype-convert": r = raster_scale_dtype_convert(a)
        elif a.op == "netcdf-subset": r = netcdf_subset(a)
        elif a.op == "netcdf-time-aggregate": r = netcdf_time_aggregate(a)
        elif a.op == "netcdf-regrid": r = netcdf_regrid(a)
        elif a.op == "netcdf-collection-diagnose": r = netcdf_collection_diagnose(a)
        elif a.op == "raster-band-semantics": r = raster_band_semantics(a)
        elif a.op == "raster-index": r = raster_index(a)
        elif a.op == "raster-rgb-composite": r = raster_rgb_composite(a)
        elif a.op == "shapefile-package-validate": r = shapefile_package_validate(a)
        elif a.op == "vector-attribute-filter": r = vector_attribute_filter(a)
        elif a.op == "vector-geometry-repair": r = vector_geometry_repair(a)
        else: r = artifact_validate(a)
    except Exception as exc:
        r = {"success": False, "operation": a.op, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(r, ensure_ascii=False, allow_nan=False)); return 0 if r["success"] else 1

if __name__ == "__main__": raise SystemExit(main())
