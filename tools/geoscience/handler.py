"""Adapter for the geoscience plugin command line."""
from __future__ import annotations
import sys
import json
from pathlib import Path
from typing import Any

OPS = {
    "geoscience_collection_inspect": "collection-inspect",
    "geoscience_coordinate_normalize": "coordinate-normalize",
    "geoscience_grid_compare": "grid-compare",
    "geoscience_quality_check": "quality-check",
    "geoscience_unit_convert": "unit-convert",
    "geoscience_raster_stack": "raster-stack",
    "geoscience_raster_mosaic": "raster-mosaic",
    "geoscience_sample_raster": "sample-raster",
    "geoscience_qa_mask": "qa-mask",
    "geoscience_scene_composite": "scene-composite",
    "geoscience_climatology": "climatology",
    "geoscience_anomaly": "anomaly",
    "geoscience_trend": "trend",
    "geoscience_artifact_validate": "artifact-validate",
    "geoscience_vector_inspect": "vector-inspect",
    "geoscience_vector_visualize": "vector-visualize",
    "geoscience_vector_transform": "vector-transform",
    "geoscience_zonal_statistics": "zonal-statistics",
    "geoscience_rasterize_vector": "rasterize-vector",
    "geoscience_grid_align": "grid-align",
    "geoscience_remote_product_inspect": "remote-product-inspect",
    "geoscience_change_detection": "change-detection",
    "geoscience_spatial_join": "spatial-join",
    "geoscience_transect_profile": "transect-profile",
    "geoscience_raster_histogram_quantiles": "raster-histogram-quantiles",
    "geoscience_raster_area_statistics": "raster-area-statistics",
    "geoscience_raster_focal_statistics": "raster-focal-statistics",
    "geoscience_raster_cog_validate_convert": "raster-cog-validate-convert",
    "geoscience_raster_classification_compare": "raster-classification-compare",
    "vector_schema_profile": "vector-schema-profile",
    "vector_topology_validate": "vector-topology-validate",
    "vector_clip_overlay": "vector-clip-overlay",
    "vector_dissolve_aggregate": "vector-dissolve-aggregate",
    "vector_format_convert": "vector-format-convert",
    "raster_clip_by_vector": "raster-clip-by-vector",
    "raster_calculator": "raster-calculator",
    "raster_nodata_normalize": "raster-nodata-normalize",
    "raster_reclassify": "raster-reclassify",
    "raster_scale_dtype_convert": "raster-scale-dtype-convert",
    "netcdf_subset": "netcdf-subset",
    "netcdf_time_aggregate": "netcdf-time-aggregate",
    "netcdf_regrid": "netcdf-regrid",
    "netcdf_collection_diagnose": "netcdf-collection-diagnose",
    "raster_band_semantics": "raster-band-semantics",
    "raster_index": "raster-index",
    "raster_rgb_composite": "raster-rgb-composite",
    "shapefile_package_validate": "shapefile-package-validate",
    "vector_attribute_filter": "vector-attribute-filter",
    "vector_geometry_repair": "vector-geometry-repair",
}

def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    if tool_name not in OPS: raise ValueError(f"Unsupported geoscience tool: {tool_name}")
    script = Path("/usr/local/lib/ai-dataseek/geoscience_ops.py")
    command = [sys.executable, str(script), OPS[tool_name]]
    if tool_name == "geoscience_collection_inspect":
        if arguments.get("input_dir"): command += ["--input-dir", str(arguments["input_dir"])]
        if arguments.get("input_paths"): command += ["--input-paths", *map(str, arguments["input_paths"])]
        return command
    if tool_name in {"geoscience_grid_compare", "geoscience_raster_stack", "geoscience_raster_mosaic", "geoscience_scene_composite", "geoscience_climatology", "geoscience_anomaly", "geoscience_trend"}:
        command += [*map(str, arguments.get("input_paths", [])), str(arguments["output_path"])] if tool_name != "geoscience_grid_compare" else [*map(str, arguments.get("input_paths", []))]
        if arguments.get("variable"): command += ["--variable", str(arguments["variable"])]
        return command
    if tool_name == "geoscience_unit_convert":
        command += [str(arguments["input_path"]), str(arguments["output_path"]), str(arguments["from_unit"]), str(arguments["to_unit"])]
        if arguments.get("variable"): command += ["--variable", str(arguments["variable"])]
        return command
    if tool_name == "geoscience_sample_raster":
        import json
        return command + [str(arguments["input_path"]), json.dumps(arguments["points"], separators=(",", ":"))]
    if tool_name == "geoscience_qa_mask":
        return command + [str(arguments["input_path"]), str(arguments["output_path"]), str(arguments["bit"])]
    if tool_name == "geoscience_artifact_validate":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_vector_inspect":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_vector_visualize":
        command += [str(arguments["input_path"]), str(arguments["output_path"]), "--cmap", str(arguments.get("cmap", "viridis")), "--max-features", str(arguments.get("max_features", 50000))]
        if arguments.get("column"): command += ["--column", str(arguments["column"])]
        if arguments.get("title"): command += ["--title", str(arguments["title"])]
        return command
    if tool_name == "geoscience_vector_transform":
        command += [str(arguments["input_path"]), str(arguments["output_path"])]
        if arguments.get("target_crs"): command += ["--target-crs", str(arguments["target_crs"])]
        return command
    if tool_name == "geoscience_zonal_statistics":
        return command + [str(arguments["raster_path"]), str(arguments["vector_path"])]
    if tool_name == "geoscience_rasterize_vector":
        return command + [str(arguments["reference_raster"]), str(arguments["vector_path"]), str(arguments["output_path"])]
    if tool_name == "geoscience_grid_align":
        return command + [str(arguments["input_path"]), str(arguments["reference_path"]), str(arguments["output_path"]), "--resampling", str(arguments.get("resampling", "nearest"))]
    if tool_name == "geoscience_remote_product_inspect":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_change_detection":
        return command + [str(arguments["before_path"]), str(arguments["after_path"]), str(arguments["output_path"]), "--band", str(arguments.get("band", 1))]
    if tool_name == "geoscience_spatial_join":
        return command + [str(arguments["left_path"]), str(arguments["right_path"]), str(arguments["output_path"]), "--predicate", str(arguments.get("predicate", "intersects")), "--how", str(arguments.get("how", "left"))]
    if tool_name == "geoscience_transect_profile":
        import json
        return command + [str(arguments["input_path"]), json.dumps(arguments["points"], separators=(",", ":")), "--samples", str(arguments.get("samples", 100)), "--band", str(arguments.get("band", 1))]
    if tool_name == "geoscience_raster_histogram_quantiles":
        return command + [str(arguments["input_path"]), "--band", str(arguments.get("band", 1)), "--bins", str(arguments.get("bins", 32))]
    if tool_name == "geoscience_raster_area_statistics":
        return command + [str(arguments["input_path"]), "--band", str(arguments.get("band", 1))]
    if tool_name == "geoscience_raster_focal_statistics":
        return command + [str(arguments["input_path"]), "--method", str(arguments.get("method", "mean")), "--window", str(arguments.get("window", 3))]
    if tool_name == "geoscience_raster_cog_validate_convert":
        command += [str(arguments["input_path"])]
        if arguments.get("output_path"): command += ["--output-path", str(arguments["output_path"])]
        return command + ["--compression", str(arguments.get("compression", "deflate"))]
    if tool_name == "geoscience_raster_classification_compare":
        return command + [str(arguments["reference_path"]), str(arguments["prediction_path"]), "--reference-band", str(arguments.get("reference_band", 1)), "--prediction-band", str(arguments.get("prediction_band", 1))]
    if tool_name == "vector_schema_profile": return command + [str(arguments["input_path"]), "--max-categories", str(arguments.get("max_categories", 30))]
    if tool_name == "vector_topology_validate": return command + [str(arguments["input_path"])]
    if tool_name == "vector_clip_overlay": return command + [str(arguments["input_path"]),str(arguments["overlay_path"]),str(arguments["output_path"]),"--operation",str(arguments.get("operation","clip"))]
    if tool_name == "vector_dissolve_aggregate": return command + [str(arguments["input_path"]),str(arguments["output_path"]),str(arguments["field"]),"--aggregate-fields",*map(str,arguments.get("aggregate_fields",[])),"--method",str(arguments.get("method","sum"))]
    if tool_name == "vector_format_convert": return command + [str(arguments["input_path"]),str(arguments["output_path"]),"--encoding",str(arguments.get("encoding","UTF-8"))]
    if tool_name == "raster_clip_by_vector": return command + [str(arguments["input_path"]),str(arguments["vector_path"]),str(arguments["output_path"])] + (["--all-touched"] if arguments.get("all_touched") else [])
    if tool_name == "raster_calculator": return command + [*map(str,arguments["input_paths"]),str(arguments["output_path"]),"--operation",str(arguments["operation"]),"--band",str(arguments.get("band",1))]
    if tool_name == "raster_nodata_normalize":
        command += [str(arguments["input_path"]),str(arguments["output_path"]),"--nodata",str(arguments.get("nodata",-9999.0))]
        if arguments.get("minimum") is not None: command += ["--minimum",str(arguments["minimum"])]
        if arguments.get("maximum") is not None: command += ["--maximum",str(arguments["maximum"])]
        return command
    if tool_name == "raster_reclassify":
        import json
        return command + [str(arguments["input_path"]),str(arguments["output_path"]),json.dumps(arguments["rules"],separators=(",",":")),"--band",str(arguments.get("band",1)),"--default",str(arguments.get("default",0)),"--nodata",str(arguments.get("nodata",-9999))]
    if tool_name == "raster_scale_dtype_convert":
        command += [str(arguments["input_path"]),str(arguments["output_path"]),"--dtype",str(arguments["dtype"]),"--scale",str(arguments.get("scale",1.0)),"--offset",str(arguments.get("offset",0.0))]
        if arguments.get("nodata") is not None: command += ["--nodata",str(arguments["nodata"])]
        return command
    if tool_name == "netcdf_subset":
        command += [str(arguments["input_path"]), str(arguments["output_path"])]
        if arguments.get("variable"): command += ["--variable", str(arguments["variable"])]
        if arguments.get("slices"): command += ["--slices", json.dumps(arguments["slices"], separators=(",", ":"))]
        if arguments.get("bbox"): command += ["--bbox", json.dumps(arguments["bbox"], separators=(",", ":"))]
        return command
    if tool_name == "netcdf_time_aggregate":
        return command + [str(arguments["input_path"]), str(arguments["output_path"]), "--frequency", str(arguments.get("frequency", "MS")), "--method", str(arguments.get("method", "mean"))] + (["--variable", str(arguments["variable"])] if arguments.get("variable") else [])
    if tool_name == "netcdf_regrid":
        return command + [str(arguments["input_path"]), str(arguments["output_path"]), "--resolution", str(arguments["resolution"]), "--method", str(arguments.get("method", "linear"))] + (["--variable", str(arguments["variable"])] if arguments.get("variable") else [])
    if tool_name == "netcdf_collection_diagnose": return command + [*map(str, arguments["input_paths"])]
    if tool_name == "raster_band_semantics": return command + [str(arguments["input_path"])]
    if tool_name == "raster_index": return command + [str(arguments["input_path"]), str(arguments["output_path"]), "--band-a", str(arguments["band_a"]), "--band-b", str(arguments["band_b"]), "--index-name", str(arguments.get("index_name", "normalized_difference"))]
    if tool_name == "raster_rgb_composite": return command + [str(arguments["input_path"]), str(arguments["output_path"]), "--red", str(arguments["red"]), "--green", str(arguments["green"]), "--blue", str(arguments["blue"])]
    if tool_name == "shapefile_package_validate": return command + [str(arguments["input_path"])]
    if tool_name == "vector_attribute_filter": return command + [str(arguments["input_path"]), str(arguments["output_path"]), str(arguments["expression"])]
    if tool_name == "vector_geometry_repair": return command + [str(arguments["input_path"]), str(arguments["output_path"])]
    for name, value in arguments.items():
        if value is None or name == "timeout_seconds": continue
        flag = "--" + name.replace("_", "-") if name.startswith("input_") or name in {"variable", "from_unit", "to_unit", "bit", "input_dir", "input_paths"} else None
        if name == "input_paths":
            if tool_name == "geoscience_collection_inspect": command.extend(["--input-paths", *[str(x) for x in value]])
            else: command.extend([str(x) for x in value])
        elif name == "input_path": command.append(str(value))
        elif name == "output_path": command.append(str(value))
        elif flag: command.extend([flag, str(value)])
    return command
