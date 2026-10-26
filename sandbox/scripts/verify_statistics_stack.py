"""Build-time numerical smoke test for the pinned statistical runtime."""
import numpy as np
import pandas as pd
import patsy
import statsmodels
from scipy import stats
from statsmodels.formula.api import ols
from statsmodels.stats.anova import anova_lm
from statsmodels.stats.multitest import multipletests


def verify():
    assert statsmodels.__version__ == "0.14.5"
    assert patsy.__version__ == "1.0.1"
    data = pd.DataFrame({"x": [0., 1., 2., 3., 4., 5.], "y": [1., 2., 4., 4., 6., 7.]})
    model = ols("y ~ x", data=data).fit()
    design = np.column_stack([np.ones(len(data)), data["x"].to_numpy()])
    coefficients = np.linalg.solve(design.T @ design, design.T @ data["y"].to_numpy())
    np.testing.assert_allclose(model.params, coefficients, rtol=1e-12, atol=1e-12)
    # Independent matrix expression catches binary/API drift in HC3 covariance.
    bread = np.linalg.inv(design.T @ design)
    leverage = np.einsum("ij,jk,ik->i", design, bread, design)
    residuals = data["y"].to_numpy() - design @ coefficients
    weights = (residuals / (1-leverage))**2
    expected_hc3 = bread @ (design.T @ (weights[:, None] * design)) @ bread
    np.testing.assert_allclose(model.get_robustcov_results(cov_type="HC3").cov_params(), expected_hc3,
                               rtol=1e-10, atol=1e-12)
    table = anova_lm(model, typ=2)
    assert np.isfinite(table.loc["x", "F"])
    np.testing.assert_allclose(multipletests([.01, .04, .03], method="holm")[1], [.03, .06, .06])
    assert np.isfinite(stats.levene([1., 2., 4.], [3., 5., 9.]).statistic)


if __name__ == "__main__":
    verify()
    print("Pinned statistical runtime verified: OLS, HC3, ANOVA, Holm, Levene")
