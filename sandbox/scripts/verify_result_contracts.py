"""Small production-image smoke using artificial data and temporary outputs."""
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
from statsmodels.formula.api import ols

from datalab_analysis import AnalysisInvariantError
from datalab_statistics import nested_ols_f_test, linear_contrast, residual_levene
from datalab_results import ResultBundle, ResultConsistencyError


def verify():
    frame = pd.DataFrame({
        "x": [-3., -1., 1., 3., -3., -1., 1., 3.],
        "z": [-1.] * 4 + [1.] * 4,
        "y": [-3., -1., 3., 9., 3., 5., 9., 15.],
    }, index=[10, 11, 13, 14, 20, 21, 23, 24])
    full = ols("y ~ x + z", frame).fit()
    reduced = ols("y ~ x", frame).fit()
    comparison = nested_ols_f_test(full, reduced)
    assert comparison["df_num"] == 1 and comparison["df_den"] == 5
    np.testing.assert_allclose(comparison["F"], 45., rtol=1e-12)
    contrast = linear_contrast(full, [0., 0., 1.])
    np.testing.assert_allclose(contrast["estimate"], 3., rtol=1e-12)
    # Use separate nondegenerate residual data for the variance diagnostic.
    diagnostic_frame = frame.assign(y=frame["y"] + np.array([0., .5, 0., -.5, .25, -.75, 1., -.25]))
    diagnostic_model = ols("y ~ x + z", diagnostic_frame).fit()
    labels = pd.Series(["A", "A", "B", "B", "A", "B", "A", "B"], index=frame.index)
    diagnostic = residual_levene(diagnostic_model, labels, expected_groups=["A", "B"])
    assert diagnostic["nobs"] == 8 and sum(diagnostic["group_sizes"]) == 8
    rejected = False
    try:
        linear_contrast(full, [1.])
    except AnalysisInvariantError:
        rejected = True
    assert rejected, "Incorrect contrast dimensions must fail"
    with tempfile.TemporaryDirectory(prefix="datalab-result-contract-") as temporary:
        root = Path(temporary)
        bundle = ResultBundle({"comparison": comparison, "rows": [
            {"x": 0., "value": 2., "lower": 1.5, "upper": 2.5},
            {"x": 1., "value": 4., "lower": 3.5, "upper": 4.5},
        ]})
        bundle.write_json(root / "result.json")
        bundle.write_csv(root / "result.csv", table="rows")
        bundle.plot(root / "result.png", table="rows", x="x", y="value", lower="lower", upper="upper")
        bundle.write_report(root / "report.md", sections=["comparison"])
        receipt = bundle.verify()
        assert len(receipt["checks"]) == 4 and receipt["scientific_validity"] == "not_assessed"
        (root / "result.csv").write_text("x,value\n0,999\n")
        rejected = False
        try:
            bundle.verify()
        except ResultConsistencyError:
            rejected = True
        assert rejected, "Changed exports must fail consistency verification"
    return {"nested_F_and_degrees": True, "contrast": True, "residual_groups": True,
            "wrong_shape_rejected": True, "four_exports_checked": True, "tampering_rejected": True}


if __name__ == "__main__":
    print(json.dumps(verify(), sort_keys=True))
