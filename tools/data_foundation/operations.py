#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np


def clean(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return clean(value.item())
        return [clean(v) for v in value.tolist()]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def result(operation: str, *, summary: dict[str, Any], warnings: list[str] | None = None, evidence: list[dict[str, Any]] | None = None, artifacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return clean({"success": True, "answer_ready": True, "operation": operation, "summary": summary, "evidence": evidence or [], "artifacts": artifacts or [], "warnings": warnings or [], "provenance": {"tool": operation, "version": "1.0.0"}, "recommended_next_tools": []})


def suffix_format(path: Path) -> str:
    name = path.name.lower()
    if path.is_dir() and (path / ".zgroup").exists() or path.is_dir() and (path / "zarr.json").exists(): return "zarr"
    for suffix, label in ((".nc", "netcdf"), (".nc4", "netcdf"), (".cdf", "netcdf"), (".hdf", "hdf"), (".h5", "hdf5"), (".hdf5", "hdf5"), (".grib", "grib"), (".grb", "grib"), (".grib2", "grib2"), (".tif", "geotiff"), (".tiff", "geotiff"), (".gpkg", "geopackage"), (".parquet", "geoparquet"), (".geojson", "geojson"), (".shp", "shapefile")):
        if name.endswith(suffix): return label
    if name.endswith(".safe"): return "safe"
    return "unknown"


def format_inspect(paths: list[str]) -> dict[str, Any]:
    items = []
    for raw in paths:
        path, fmt = Path(raw), suffix_format(Path(raw))
        item: dict[str, Any] = {"name": path.name, "format": fmt, "is_directory": path.is_dir()}
        if not path.exists(): item["error"] = "path does not exist"
        elif fmt in {"geotiff", "hdf", "hdf5"}:
            try:
                import rasterio
                with rasterio.open(path) as src:
                    item.update(driver=src.driver, subdatasets=list(src.subdatasets), band_count=src.count)
            except Exception as exc: item["open_warning"] = str(exc)
        elif fmt in {"geopackage", "geojson", "shapefile", "geoparquet"}:
            try:
                import pyogrio
                item["layers"] = [row[0] for row in pyogrio.list_layers(path)]
            except Exception as exc: item["open_warning"] = str(exc)
        items.append(item)
    warnings = [f"{i['name']}: {i.get('error') or i.get('open_warning')}" for i in items if i.get("error") or i.get("open_warning")]
    return result("data_format_inspect", summary={"file_count": len(items), "items": items}, warnings=warnings)


def store_kind(path: Path) -> str:
    if path.is_dir() and ((path / ".zgroup").exists() or (path / "zarr.json").exists()): return "zarr"
    if path.suffix.lower() in {".h5", ".hdf5", ".hdf", ".he5"}: return "hdf5"
    raise ValueError("hierarchical store must be HDF5 or Zarr")


def hierarchical_inspect(path: str, maximum: int) -> dict[str, Any]:
    source=Path(path); kind=store_kind(source); entries=[]; truncated=False
    if kind=="hdf5":
        import h5py
        with h5py.File(source,"r") as store:
            root_attributes={str(k):clean(v) for k,v in store.attrs.items()}
            def visitor(name: str, node: Any) -> None:
                nonlocal truncated
                if len(entries)>=maximum: truncated=True; return
                if isinstance(node,h5py.Dataset):
                    entries.append({"path":"/"+name,"type":"array","shape":list(node.shape),"dtype":str(node.dtype),"chunks":list(node.chunks) if node.chunks else None,"compression":node.compression,"attributes":{str(k):clean(v) for k,v in node.attrs.items()}})
                else: entries.append({"path":"/"+name,"type":"group","attributes":{str(k):clean(v) for k,v in node.attrs.items()}})
            store.visititems(visitor)
    else:
        import zarr
        store=zarr.open_group(str(source),mode="r"); root_attributes=clean(dict(store.attrs))
        for name,node in store.groups():
            if len(entries)>=maximum: truncated=True; break
            entries.append({"path":"/"+name,"type":"group","attributes":clean(dict(node.attrs))})
        if not truncated:
            for name,node in store.arrays():
                if len(entries)>=maximum: truncated=True; break
                entries.append({"path":"/"+name,"type":"array","shape":list(node.shape),"dtype":str(node.dtype),"chunks":list(node.chunks),"compression":str(node.compressor) if node.compressor else None,"attributes":clean(dict(node.attrs))})
        # arrays()/groups() above are shallow in Zarr v2; recurse deterministically.
        if hasattr(store,"visititems"):
            entries=[]
            def zvisitor(name: str, node: Any) -> None:
                nonlocal truncated
                if len(entries)>=maximum: truncated=True; return
                is_array=hasattr(node,"shape") and hasattr(node,"dtype")
                item={"path":"/"+name,"type":"array" if is_array else "group","attributes":clean(dict(node.attrs))}
                if is_array: item.update(shape=list(node.shape),dtype=str(node.dtype),chunks=list(node.chunks),compression=str(node.compressor) if node.compressor else None)
                entries.append(item)
            store.visititems(zvisitor)
    warnings=[f"entry list truncated at {maximum}"] if truncated else []
    return result("hierarchical_store_inspect",summary={"format":kind,"entry_count":len(entries),"truncated":truncated,"root_attributes":root_attributes,"entries":entries},warnings=warnings)


def parse_selection(values: list[Any], ndim: int) -> tuple[Any,...]:
    selection=[]
    for value in values:
        if isinstance(value,int): selection.append(value); continue
        if not isinstance(value,str) or not __import__("re").fullmatch(r"-?\d*:-?\d*(?::-?\d+)?",value): raise ValueError(f"invalid selection: {value}")
        parts=value.split(":"); selection.append(slice(*(int(part) if part else None for part in parts)))
    if len(selection)>ndim: raise ValueError("selection has more dimensions than the array")
    return tuple(selection+[slice(None)]*(ndim-len(selection)))


def selected_value_count(shape: tuple[int,...], selection: tuple[Any,...]) -> int:
    count=1
    for size,item in zip(shape,selection):
        if isinstance(item,int):
            if item < -size or item >= size: raise ValueError("selection index is outside the array")
            continue
        start,stop,step=item.indices(size); count*=len(range(start,stop,step))
    return count


def safe_output_path(raw: str) -> Path:
    root=Path(os.environ.get("AI_DATASEEK_OUTPUT_ROOT","/home/ubuntu/output")).resolve(); path=Path(raw).resolve()
    if root!=path and root not in path.parents: raise ValueError(f"output path must be below {root}")
    path.parent.mkdir(parents=True,exist_ok=True); return path


def hierarchical_extract(path: str, dataset_path: str, selection_values: list[Any], maximum: int, output: str) -> dict[str, Any]:
    source=Path(path); kind=store_kind(source); normalized=dataset_path.lstrip("/")
    if not normalized or ".." in Path(normalized).parts: raise ValueError("invalid dataset path")
    if kind=="hdf5":
        import h5py
        with h5py.File(source,"r") as store:
            if normalized not in store or not isinstance(store[normalized],h5py.Dataset): raise ValueError("HDF5 dataset was not found")
            node=store[normalized]; selection=parse_selection(selection_values,node.ndim); count=selected_value_count(node.shape,selection)
            if count>maximum: raise ValueError(f"selection contains {count} values, above max_values={maximum}")
            values=np.asarray(node[selection])
    else:
        import zarr
        store=zarr.open_group(str(source),mode="r")
        try: node=store[normalized]
        except KeyError as exc: raise ValueError("Zarr array was not found") from exc
        if not hasattr(node,"shape"): raise ValueError("selected Zarr path is not an array")
        selection=parse_selection(selection_values,len(node.shape)); count=selected_value_count(node.shape,selection)
        if count>maximum: raise ValueError(f"selection contains {count} values, above max_values={maximum}")
        values=np.asarray(node[selection])
    target=safe_output_path(output); suffix=target.suffix.lower()
    if suffix==".npy": np.save(target,values,allow_pickle=False); mime="application/x-npy"
    elif suffix==".csv":
        if values.ndim>2: raise ValueError("CSV output requires a scalar, vector or matrix selection")
        np.savetxt(target,np.atleast_2d(values),delimiter=",",fmt="%s"); mime="text/csv"
    elif suffix==".json": target.write_text(json.dumps(clean(values),ensure_ascii=False),encoding="utf-8"); mime="application/json"
    else: raise ValueError("hierarchical array output must be .npy, .csv or .json")
    return result("hierarchical_array_extract",summary={"format":kind,"dataset_path":"/"+normalized,"shape":list(values.shape),"dtype":str(values.dtype),"value_count":int(values.size)},artifacts=[{"path":str(target),"type":mime,"size_bytes":target.stat().st_size}])


def open_dataset(path: str):
    import xarray as xr
    p = Path(path)
    if suffix_format(p) == "zarr": return xr.open_zarr(p, consolidated=None, decode_cf=False)
    return xr.open_dataset(p, decode_cf=False)


def coordinate_role(name: str, var: Any) -> str | None:
    std, axis, lower = str(var.attrs.get("standard_name", "")).lower(), str(var.attrs.get("axis", "")).upper(), name.lower()
    if axis == "X" or std == "longitude" or lower in {"lon", "longitude"}: return "longitude"
    if axis == "Y" or std == "latitude" or lower in {"lat", "latitude"}: return "latitude"
    if axis == "T" or std == "time" or lower == "time": return "time"
    return None


def cf_validate(path: str) -> dict[str, Any]:
    warnings, checks = [], []
    with open_dataset(path) as ds:
        convention = str(ds.attrs.get("Conventions", ""))
        if "CF-" not in convention: warnings.append("global Conventions does not declare CF compliance")
        roles = {role: name for name, var in ds.coords.items() if (role := coordinate_role(name, var))}
        for role in ("latitude", "longitude"):
            if role not in roles: warnings.append(f"{role} coordinate was not identified")
        for name, var in ds.variables.items():
            attrs = var.attrs
            checks.append({"variable": name, "dimensions": list(var.dims), "units": attrs.get("units"), "fill_value": attrs.get("_FillValue"), "scale_factor": attrs.get("scale_factor"), "add_offset": attrs.get("add_offset"), "grid_mapping": attrs.get("grid_mapping")})
            if "scale_factor" in attrs and not np.issubdtype(var.dtype, np.number): warnings.append(f"{name}: scale_factor is declared on a non-numeric variable")
            if coordinate_role(name, var) == "time" and "units" not in attrs: warnings.append(f"{name}: time coordinate has no units")
        summary = {"conventions": convention or None, "dimensions": dict(ds.sizes), "coordinate_roles": roles, "variables": checks}
    return result("cf_semantics_validate", summary=summary, warnings=warnings)


def grid_diagnose(path: str) -> dict[str, Any]:
    p = Path(path)
    if suffix_format(p) == "geotiff":
        import rasterio
        with rasterio.open(p) as src:
            summary = {"grid_type":"projected_raster" if src.crs and not src.crs.is_geographic else "geographic_raster", "crs":str(src.crs) if src.crs else None, "shape":[src.height,src.width], "resolution":list(src.res), "bounds":list(src.bounds), "north_up":src.transform.b == 0 and src.transform.d == 0}
        return result("spatial_grid_diagnose", summary=summary, warnings=[] if summary["crs"] else ["raster has no CRS"])
    with open_dataset(path) as ds:
        roles = {role: ds[name] for name, var in ds.coords.items() if (role := coordinate_role(name, var))}
        lat, lon = roles.get("latitude"), roles.get("longitude")
        if lat is None or lon is None: return result("spatial_grid_diagnose", summary={"grid_type":"unknown"}, warnings=["latitude/longitude coordinates were not identified"])
        grid_type = "rectilinear" if lat.ndim == lon.ndim == 1 else "curvilinear" if lat.ndim == lon.ndim == 2 else "mixed"
        lon_values = np.asarray(lon.values, dtype=float)
        summary = {"grid_type":grid_type, "latitude_dimensions":list(lat.dims), "longitude_dimensions":list(lon.dims), "longitude_domain":"0_360" if np.nanmax(lon_values) > 180 else "-180_180", "crosses_antimeridian":bool(np.nanmax(lon_values)-np.nanmin(lon_values) > 300), "latitude_ascending":bool(lat.ndim == 1 and lat.size > 1 and lat.values[-1] > lat.values[0]), "longitude_ascending":bool(lon.ndim == 1 and lon.size > 1 and lon.values[-1] > lon.values[0])}
    return result("spatial_grid_diagnose", summary=summary)


def raster_compatibility(paths: list[str], operation: str) -> dict[str, Any]:
    import rasterio
    signatures = []
    for raw in paths:
        with rasterio.open(raw) as src:
            signatures.append({"name":Path(raw).name,"crs":str(src.crs),"shape":[src.height,src.width],"transform":list(src.transform),"resolution":list(src.res),"band_count":src.count,"dtype":list(src.dtypes),"nodata":src.nodata,"bounds":list(src.bounds)})
    first = signatures[0]
    strict = operation in {"stack", "difference", "overlay"}
    fields = ["crs", "shape", "transform", "resolution"] if strict else ["crs", "resolution"]
    differences = [
        {"file": s["name"], "fields": [field for field in fields if s[field] != first[field]]}
        for s in signatures[1:]
    ]
    differences = [d for d in differences if d["fields"]]
    return result("raster_compatibility_validate", summary={"operation":operation,"compatible":not differences,"signatures":signatures,"differences":differences}, warnings=[] if not differences else ["rasters require alignment before this operation"])


PRODUCTS = {
    "landsat": {"markers":["landsat","lc08","lc09","le07"],"quality_band":"QA_PIXEL","scale":"use collection metadata; Collection 2 surface reflectance commonly uses 0.0000275 and -0.2 offset"},
    "sentinel-2": {"markers":["sentinel-2","s2a_","s2b_","msil1c","msil2a"],"quality_band":"SCL or QA60","scale":"resolve from product metadata"},
    "modis": {"markers":["modis","mod09","myd09","mod13","myd13"],"quality_band":"product-specific QA layer","scale":"resolve from HDF-EOS metadata"},
    "viirs": {"markers":["viirs","vnp","vjn"],"quality_band":"product-specific QF layer","scale":"resolve from product metadata"},
}


def product_resolve(path: str) -> dict[str, Any]:
    p, probe, metadata = Path(path), Path(path).name.lower(), {}
    try:
        import rasterio
        with rasterio.open(p) as src: metadata = src.tags(); probe += " " + " ".join(f"{k}={v}" for k,v in metadata.items()).lower()
    except Exception: pass
    matches = [(name, model) for name, model in PRODUCTS.items() if any(marker in probe for marker in model["markers"])]
    if len(matches) != 1: return result("eo_product_resolve", summary={"product":None,"metadata":metadata}, warnings=["product could not be resolved unambiguously; do not infer band or QA semantics"])
    name, model = matches[0]
    return result("eo_product_resolve", summary={"product":name,"quality_band":model["quality_band"],"scale_rule":model["scale"],"metadata":metadata})


def artifact_validate(path: str) -> dict[str, Any]:
    p, warnings = Path(path), []
    if not p.is_file() or p.stat().st_size == 0: raise ValueError("artifact is missing or empty")
    fmt, summary = suffix_format(p), {"name":p.name,"format":suffix_format(p),"size_bytes":p.stat().st_size,"readable":True}
    if fmt == "geotiff":
        import rasterio
        with rasterio.open(p) as src:
            summary.update(shape=[src.height,src.width],band_count=src.count,crs=str(src.crs) if src.crs else None,nodata=src.nodata)
            sample = src.read(1, out_shape=(1,min(src.height,256),min(src.width,256)), masked=True)
            summary["valid_sample_count"] = int(sample.count())
            if not src.crs: warnings.append("raster has no CRS")
            if sample.count() == 0: warnings.append("raster sample contains no valid pixels")
    elif fmt == "netcdf":
        with open_dataset(path) as ds:
            summary.update(dimensions=dict(ds.sizes),variables=list(ds.data_vars))
            if not ds.data_vars: warnings.append("dataset has no data variables")
    elif p.suffix.lower() == ".png":
        from PIL import Image
        with Image.open(p) as image:
            arr = np.asarray(image.convert("RGB").resize((64,64)))
            summary.update(width=image.width,height=image.height,pixel_standard_deviation=float(arr.std()))
            if arr.std() < 1: warnings.append("image appears blank or constant")
    return result("artifact_scientific_validate", summary=summary, warnings=warnings)


def _time_name(ds: Any, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in ds.coords: raise ValueError(f"time coordinate was not found: {explicit}")
        return explicit
    for name, var in ds.coords.items():
        if coordinate_role(name, var) == "time": return name
    raise ValueError("time coordinate was not identified")


def _decoded_dataset(path: str):
    import xarray as xr
    p=Path(path)
    return xr.open_zarr(p,consolidated=None,decode_cf=True) if suffix_format(p)=="zarr" else xr.open_dataset(p,decode_cf=True)


def time_axis_normalize(path: str, explicit: str | None) -> dict[str, Any]:
    with _decoded_dataset(path) as ds:
        name=_time_name(ds,explicit); coord=ds[name]; values=list(coord.values)
        normalized=[str(getattr(v,"isoformat",lambda: str(v))()) for v in values]
        duplicates=len(normalized)-len(set(normalized)); gaps=[]
        if len(values)>2:
            deltas=np.diff(np.asarray(values))
            numeric=np.array([float(d / np.timedelta64(1,"s")) if isinstance(d,np.timedelta64) else float(getattr(d,"total_seconds",lambda:0)()) for d in deltas])
            positive=numeric[numeric>0]
            if positive.size:
                expected=float(np.median(positive)); gaps=[{"after_index":int(i),"seconds":float(v)} for i,v in enumerate(numeric) if v>expected*1.5]
        summary={"time_name":name,"count":len(values),"calendar":coord.encoding.get("calendar") or coord.attrs.get("calendar") or "standard","units":coord.encoding.get("units") or coord.attrs.get("units"),"first":normalized[0] if normalized else None,"last":normalized[-1] if normalized else None,"duplicate_count":duplicates,"gap_count":len(gaps),"gaps":gaps[:100],"timestamps":normalized[:1000],"truncated":len(normalized)>1000}
    return result("netcdf_time_axis_normalize",summary=summary,warnings=["timestamp list truncated at 1000"] if len(normalized)>1000 else [])


UNIT_RULES={
    ("k","degc"):(1.0,-273.15),("kelvin","degc"):(1.0,-273.15),("degc","k"):(1.0,273.15),("celsius","k"):(1.0,273.15),
    ("pa","hpa"):(0.01,0.0),("hpa","pa"):(100.0,0.0),("m/s","km/h"):(3.6,0.0),("km/h","m/s"):(1/3.6,0.0),
    ("mm","m"):(0.001,0.0),("m","mm"):(1000.0,0.0),
}


def unit_convert(path: str, variable: str, target_unit: str, output: str) -> dict[str, Any]:
    import xarray as xr
    target=safe_output_path(output)
    with _decoded_dataset(path) as ds:
        if variable not in ds.data_vars: raise ValueError(f"data variable was not found: {variable}")
        source=str(ds[variable].attrs.get("units","")).strip(); key=(source.lower(),target_unit.lower())
        if key not in UNIT_RULES: raise ValueError(f"unsupported or ambiguous unit conversion: {source} -> {target_unit}")
        scale,offset=UNIT_RULES[key]; out=ds[[variable]].copy(); out[variable]=ds[variable].astype("float64")*scale+offset; out[variable].attrs.update(ds[variable].attrs); out[variable].attrs["units"]=target_unit; out[variable].attrs["conversion_history"]=f"{source} -> {target_unit}; scale={scale}; offset={offset}"; out.to_netcdf(target)
    return result("netcdf_unit_convert",summary={"variable":variable,"source_unit":source,"target_unit":target_unit,"scale":scale,"offset":offset},artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])


def vertical_slice(path: str, variable: str, dimension: str, index: int | None, value: float | None, output: str) -> dict[str, Any]:
    target=safe_output_path(output)
    if (index is None)==(value is None): raise ValueError("provide exactly one of index or value")
    with _decoded_dataset(path) as ds:
        if variable not in ds.data_vars: raise ValueError(f"data variable was not found: {variable}")
        if dimension not in ds[variable].dims: raise ValueError(f"dimension is not used by {variable}: {dimension}")
        selected=ds[[variable]].isel({dimension:index}) if index is not None else ds[[variable]].sel({dimension:value},method="nearest")
        actual=clean(selected[dimension].values) if dimension in selected.coords else index; selected.to_netcdf(target)
    return result("netcdf_vertical_slice",summary={"variable":variable,"dimension":dimension,"requested_index":index,"requested_value":value,"selected_value":actual,"shape":list(selected[variable].shape)},artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])


def climatology(path: str, variable: str, frequency: str, output: str | None) -> dict[str, Any]:
    with _decoded_dataset(path) as ds:
        if variable not in ds.data_vars: raise ValueError(f"data variable was not found: {variable}")
        time=_time_name(ds); group=f"{time}.month" if frequency=="month" else f"{time}.season"; climate=ds[variable].groupby(group).mean(time,skipna=True); artifacts=[]
        if output:
            target=safe_output_path(output); climate.to_dataset(name=variable).to_netcdf(target); artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}]
        summary={"variable":variable,"frequency":frequency,"groups":clean(climate[frequency].values),"shape":list(climate.shape),"minimum":float(climate.min(skipna=True)),"maximum":float(climate.max(skipna=True)),"mean":float(climate.mean(skipna=True))}
    return result("netcdf_climatology",summary=summary,artifacts=artifacts)


def missing_gap_detect(path: str, variable: str, explicit: str | None) -> dict[str, Any]:
    with _decoded_dataset(path) as ds:
        if variable not in ds.data_vars: raise ValueError(f"data variable was not found: {variable}")
        data=ds[variable]; total=int(data.size); missing=int(data.isnull().sum().compute() if hasattr(data.data,"compute") else data.isnull().sum())
        time_report=time_axis_normalize(path,explicit)["summary"] if any(coordinate_role(n,v)=="time" for n,v in ds.coords.items()) else None
    warnings=[]
    if missing: warnings.append(f"{missing} missing values detected")
    if time_report and time_report["gap_count"]: warnings.append(f"{time_report['gap_count']} temporal gaps detected")
    return result("netcdf_missing_gap_detect",summary={"variable":variable,"total_values":total,"missing_values":missing,"missing_fraction":missing/total if total else None,"time_axis":time_report},warnings=warnings)

def multi_file_concat(paths: list[str], dimension: str, variable: str | None, output: str) -> dict[str, Any]:
    import xarray as xr
    target=safe_output_path(output)
    datasets=[_decoded_dataset(path) for path in paths]
    try:
        merged=xr.concat([ds[[variable]] if variable else ds for ds in datasets],dim=dimension,data_vars="minimal",coords="minimal",compat="override")
        merged.to_netcdf(target); summary={"file_count":len(paths),"dimension":dimension,"sizes":dict(merged.sizes),"variable":variable}
    finally:
        for ds in datasets: ds.close()
    return result("netcdf_multi_file_concat",summary=summary,artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def multi_file_merge(paths: list[str], output: str) -> dict[str, Any]:
    import xarray as xr
    target=safe_output_path(output); datasets=[_decoded_dataset(path) for path in paths]
    try: merged=xr.merge(datasets,compat="no_conflicts",join="exact"); merged.to_netcdf(target); summary={"file_count":len(paths),"variables":list(merged.data_vars),"sizes":dict(merged.sizes)}
    finally:
        for ds in datasets: ds.close()
    return result("netcdf_multi_file_merge",summary=summary,artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def mask_by_vector(path: str, vector_path: str, variable: str | None, output: str) -> dict[str, Any]:
    import geopandas as gpd
    import xarray as xr
    target=safe_output_path(output)
    with _decoded_dataset(path) as ds:
        name=variable or (list(ds.data_vars)[0] if len(ds.data_vars)==1 else None)
        if not name or name not in ds.data_vars: raise ValueError("variable is required when the NetCDF has multiple variables")
        da=ds[name]; lat=next((n for n,v in ds.coords.items() if coordinate_role(n,v)=="latitude"),None); lon=next((n for n,v in ds.coords.items() if coordinate_role(n,v)=="longitude"),None)
        if not lat or not lon: raise ValueError("latitude and longitude coordinates are required")
        gdf=gpd.read_file(vector_path)
        if gdf.crs and str(gdf.crs)!="EPSG:4326": gdf=gdf.to_crs("EPSG:4326")
        from shapely.geometry import Point
        import numpy as np
        points=np.array([[Point(float(x),float(y)).within(gdf.unary_union) for x in ds[lon].values] for y in ds[lat].values])
        mask=xr.DataArray(points,dims=(lat,lon),coords={lat:ds[lat],lon:ds[lon]}); out=ds.copy(); out[name]=da.where(mask); out.to_netcdf(target)
    return result("netcdf_mask_by_vector",summary={"variable":name,"mask_shape":list(mask.shape),"valid_grid_points":int(mask.sum())},artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def encoding_optimize(path: str, variable: str | None, output: str, compression_level: int) -> dict[str, Any]:
    import xarray as xr
    target=safe_output_path(output)
    with _decoded_dataset(path) as ds:
        names=[variable] if variable else list(ds.data_vars); encoding={name:{"zlib":True,"complevel":compression_level,"shuffle":True} for name in names}; ds.to_netcdf(target,encoding=encoding)
        summary={"variables":names,"compression_level":compression_level,"sizes":dict(ds.sizes)}
    return result("netcdf_encoding_optimize",summary=summary,artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])

def dimension_normalize(path: str, variable: str | None, output: str) -> dict[str, Any]:
    import xarray as xr
    target=safe_output_path(output)
    with _decoded_dataset(path) as ds:
        names=[variable] if variable else list(ds.data_vars); out=ds.copy()
        for name in names:
            if name not in out.data_vars: raise ValueError(f"data variable was not found: {name}")
            da=out[name]; time=next((d for d in da.dims if coordinate_role(d,out[d])=="time"),None); lat=next((d for d in da.dims if coordinate_role(d,out[d])=="latitude"),None); lon=next((d for d in da.dims if coordinate_role(d,out[d])=="longitude"),None); ordered=[d for d in (time,lat,lon) if d]+[d for d in da.dims if d not in {time,lat,lon}]; out[name]=da.transpose(*ordered)
        out.to_netcdf(target); summary={"variables":names,"sizes":dict(out.sizes)}
    return result("netcdf_dimension_normalize",summary=summary,artifacts=[{"path":str(target),"type":"application/x-netcdf","size_bytes":target.stat().st_size}])


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("tool"); parser.add_argument("--input-path"); parser.add_argument("--input-paths-json"); parser.add_argument("--operation"); parser.add_argument("--max-entries",type=int,default=500); parser.add_argument("--dataset-path"); parser.add_argument("--selection-json"); parser.add_argument("--max-values",type=int,default=1000000); parser.add_argument("--output-path"); parser.add_argument("--variable"); parser.add_argument("--target-unit"); parser.add_argument("--time-name"); parser.add_argument("--dimension"); parser.add_argument("--index",type=int); parser.add_argument("--value",type=float); parser.add_argument("--frequency",default="month"); parser.add_argument("--compression-level",type=int,default=4); parser.add_argument("--vector-path")
    args = parser.parse_args()
    try:
        paths = json.loads(args.input_paths_json) if args.input_paths_json else []
        functions = {"data_format_inspect":lambda:format_inspect(paths),"hierarchical_store_inspect":lambda:hierarchical_inspect(args.input_path,args.max_entries),"hierarchical_array_extract":lambda:hierarchical_extract(args.input_path,args.dataset_path,json.loads(args.selection_json or "[]"),args.max_values,args.output_path),"cf_semantics_validate":lambda:cf_validate(args.input_path),"spatial_grid_diagnose":lambda:grid_diagnose(args.input_path),"raster_compatibility_validate":lambda:raster_compatibility(paths,args.operation),"eo_product_resolve":lambda:product_resolve(args.input_path),"artifact_scientific_validate":lambda:artifact_validate(args.input_path),"netcdf_time_axis_normalize":lambda:time_axis_normalize(args.input_path,args.time_name),"netcdf_unit_convert":lambda:unit_convert(args.input_path,args.variable,args.target_unit,args.output_path),"netcdf_vertical_slice":lambda:vertical_slice(args.input_path,args.variable,args.dimension,args.index,args.value,args.output_path),"netcdf_climatology":lambda:climatology(args.input_path,args.variable,args.frequency,args.output_path),"netcdf_missing_gap_detect":lambda:missing_gap_detect(args.input_path,args.variable,args.time_name),"netcdf_multi_file_concat":lambda:multi_file_concat(paths,args.dimension,args.variable,args.output_path),"netcdf_multi_file_merge":lambda:multi_file_merge(paths,args.output_path),"netcdf_mask_by_vector":lambda:mask_by_vector(args.input_path,args.vector_path,args.variable,args.output_path),"netcdf_encoding_optimize":lambda:encoding_optimize(args.input_path,args.variable,args.output_path,args.compression_level),"netcdf_dimension_normalize":lambda:dimension_normalize(args.input_path,args.variable,args.output_path)}
        output = functions[args.tool]()
    except Exception as exc:
        output = {"success":False,"answer_ready":True,"operation":args.tool,"error":f"{type(exc).__name__}: {exc}","warnings":[]}
    print(json.dumps(clean(output), ensure_ascii=False, allow_nan=False)); return 0 if output["success"] else 1


if __name__ == "__main__": raise SystemExit(main())
