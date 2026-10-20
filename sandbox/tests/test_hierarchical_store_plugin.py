import importlib.util
from pathlib import Path

import h5py
import numpy as np
import zarr

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location("data_foundation_operations",ROOT/"tools"/"data_foundation"/"operations.py")
OPS=importlib.util.module_from_spec(SPEC); assert SPEC.loader is not None; SPEC.loader.exec_module(OPS)


def test_hdf5_tree_and_bounded_extract(tmp_path, monkeypatch):
    source=tmp_path/"sample.h5"
    with h5py.File(source,"w") as f:
        f.attrs["labels"]=np.asarray([b"A",b"B"])
        f.create_group("observations").create_dataset("temperature",data=np.arange(12).reshape(3,4),chunks=(2,2))
    inspected=OPS.hierarchical_inspect(str(source),100)
    assert any(item["path"]=="/observations/temperature" for item in inspected["summary"]["entries"])
    assert inspected["summary"]["root_attributes"]["labels"] == ["A","B"]
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT",str(tmp_path))
    extracted=OPS.hierarchical_extract(str(source),"/observations/temperature",["1:3","0:2"],10,str(tmp_path/"slice.npy"))
    assert extracted["summary"]["shape"] == [2,2]
    assert Path(extracted["artifacts"][0]["path"]).exists()


def test_zarr_tree_and_value_bound(tmp_path, monkeypatch):
    source=tmp_path/"sample.zarr"; root=zarr.open_group(str(source),mode="w"); root.create_dataset("temperature",data=np.arange(6).reshape(2,3))
    inspected=OPS.hierarchical_inspect(str(source),100)
    assert inspected["summary"]["format"] == "zarr"
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT",str(tmp_path))
    try:
        OPS.hierarchical_extract(str(source),"temperature",[],2,str(tmp_path/"too-large.npy"))
    except ValueError as exc:
        assert "max_values" in str(exc)
    else:
        raise AssertionError("bounded extraction accepted too many values")
