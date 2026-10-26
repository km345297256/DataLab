"""Numerical contracts use independent formulas and deliberately wrong receipts."""
import json
import inspect
import numpy as np
import pandas as pd
import pytest
from scipy import stats
import statsmodels.api as sm
from statsmodels.formula.api import ols

from scripts.datalab_analysis import AnalysisInvariantError
from scripts.datalab_statistics import linear_contrast, nested_ols_f_test, residual_levene


@pytest.mark.parametrize("function,example,meaning", [
    (nested_ols_f_test, "result['F']", "FULL model's residual degrees of freedom"),
    (linear_contrast, "checked['estimate']", "None for normal inference"),
    (residual_levene, "checked['statistic']", "post-trimming"),
])
def test_api_documentation_exposes_dict_returns_and_statistical_meaning(function, example, meaning):
    documentation = inspect.getdoc(function)
    returns = documentation.split("Returns\n-------\ndict\n", 1)[1]
    assert "plain dict" in returns
    assert example in returns
    assert meaning in returns
    assert "nullable" in returns
    assert "row IDs only" in returns


@pytest.fixture
def fits():
    # Deliberately nonconsecutive IDs and a batch effect survive filtering.
    n = 24
    table = pd.DataFrame({"x": np.tile(np.arange(6), 4),
                          "z": np.repeat([-1., 0., 1., 2.], 6),
                          "group": np.tile(["a", "a", "a", "b", "b", "b"], 4)},
                         index=np.arange(n) * 2 + 3)
    noise = np.array([-.4, .2, .6, -1.3, .5, 1.1] * 4) * (1 + np.arange(n) % 3 / 4)
    table["y"] = 2 + .7 * table.x + 1.8 * table.z + .35 * table.x * table.z + noise
    return table, ols("y ~ x * z", data=table).fit(), ols("y ~ x + z", data=table).fit()


def independent_fit(result):
    x, y = result.model.exog, result.model.endog
    beta = np.linalg.solve(x.T @ x, x.T @ y)
    residual = y - x @ beta
    return beta, residual, float(residual @ residual)


def test_nested_result_uses_full_residual_df_not_the_library_third_return(fits):
    _, full, reduced = fits
    observed = nested_ols_f_test(full, reduced)
    _, _, s_full = independent_fit(full)
    _, _, s_reduced = independent_fit(reduced)
    expected = (s_reduced - s_full) / (s_full / 20)
    assert observed["F"] == pytest.approx(expected, rel=1e-11)
    assert observed["df_num"] == 1
    assert observed["df_den"] == 20
    assert observed["df_den"] != full.compare_f_test(reduced)[2]
    assert observed["p"] == pytest.approx(stats.f.sf(expected, 1, 20), rel=1e-11)
    assert observed["nobs"] == 24
    assert len(observed["sample_identity"]) == 64
    assert observed["sample_identity_scope"] == "ordered_row_ids_only"
    json.dumps(observed, allow_nan=False)


@pytest.mark.parametrize("change", ["reordered", "subset", "response", "non_nested"])
def test_nested_rejects_incompatible_samples_and_model_spaces(fits, change):
    table, full, _ = fits
    if change == "reordered":
        other = ols("y ~ x + z", data=table.iloc[::-1]).fit()
    elif change == "subset":
        other = ols("y ~ x + z", data=table.iloc[:-1]).fit()
    elif change == "response":
        other = ols("y ~ x + z", data=table.assign(y=table.y + 1)).fit()
    else:
        other = ols("y ~ I(x**2)", data=table).fit()
    with pytest.raises(AnalysisInvariantError):
        nested_ols_f_test(full, other)


def test_nested_rejects_rank_deficiency_and_robust_covariance(fits):
    table, full, reduced = fits
    singular = ols("y ~ x + z + I(2*x)", data=table).fit()
    with pytest.raises(AnalysisInvariantError, match="Rank-deficient"):
        nested_ols_f_test(singular, reduced)
    with pytest.raises(AnalysisInvariantError, match="nonrobust"):
        nested_ols_f_test(full.get_robustcov_results(cov_type="HC3"), reduced)
    with pytest.raises(AnalysisInvariantError, match="independent parameter"):
        nested_ols_f_test(full, full)


def test_nested_rejects_numerically_perfect_fit():
    x = np.arange(12, dtype=float)
    data = pd.DataFrame({"x": x, "y": 2 + 3 * x + 4 * x ** 2})
    full = ols("y ~ x + I(x**2)", data=data).fit()
    reduced = ols("y ~ x", data=data).fit()
    with pytest.raises(AnalysisInvariantError, match="Residual sums of squares"):
        nested_ols_f_test(full, reduced)


@pytest.mark.parametrize("predictor_scale", [1e-12, 1., 1e12])
def test_non_nested_predictor_cannot_hide_below_fixed_absolute_tolerance(predictor_scale):
    rng = np.random.default_rng(113)
    x = np.linspace(-2, 2, 60)
    z = rng.normal(size=60)
    data = pd.DataFrame({"x": x, "z": z, "q": predictor_scale * x ** 2,
                         "y": 1 + 2 * x + .7 * z + .1 * rng.normal(size=60)})
    full, reduced = ols("y ~ x + z", data=data).fit(), ols("y ~ q", data=data).fit()
    with pytest.raises(AnalysisInvariantError, match="nesting"):
        nested_ols_f_test(full, reduced)


@pytest.mark.parametrize("response_scale", [1e-12, 1., 1e12])
def test_valid_ols_test_is_invariant_to_response_units(fits, response_scale):
    table, full, reduced = fits
    expected = nested_ols_f_test(full, reduced)
    scaled = table.assign(y=table.y * response_scale)
    actual = nested_ols_f_test(ols("y ~ x * z", data=scaled).fit(),
                               ols("y ~ x + z", data=scaled).fit())
    assert actual["F"] == pytest.approx(expected["F"], rel=1e-10)
    assert actual["p"] == pytest.approx(expected["p"], rel=1e-10)


@pytest.mark.parametrize("response_scale", [1e-12, 1., 1e12])
def test_wrong_residual_is_rejected_independently_of_response_units(fits, monkeypatch, response_scale):
    table, _, _ = fits
    fit = ols("y ~ x * z", data=table.assign(y=table.y * response_scale)).fit()
    monkeypatch.setattr(fit, "resid", fit.resid + response_scale)
    with pytest.raises(AnalysisInvariantError, match="Model response residuals"):
        residual_levene(fit, table.group)


def test_small_unit_wrong_coefficients_are_not_accepted_as_ols(fits, monkeypatch):
    table, _, _ = fits
    data = table.assign(y=table.y * 1e-12)
    full, reduced = ols("y ~ x * z", data=data).fit(), ols("y ~ x + z", data=data).fit()
    incorrect = full.params.copy()
    incorrect.iloc[1] += 1e-12
    monkeypatch.setattr(full, "params", incorrect)
    monkeypatch.setattr(full, "resid", full.model.endog - full.model.exog @ incorrect.to_numpy())
    with pytest.raises(AnalysisInvariantError, match="OLS coefficients"):
        nested_ols_f_test(full, reduced)


@pytest.mark.parametrize("position,replacement", [(0, 42.), (1, .5), (2, 20.)])
def test_nested_rejects_wrong_library_statistic_probability_or_df(fits, monkeypatch, position, replacement):
    _, full, reduced = fits
    receipt = list(full.compare_f_test(reduced))
    receipt[position] = replacement
    monkeypatch.setattr(full, "compare_f_test", lambda other: receipt)
    with pytest.raises(AnalysisInvariantError, match="independent calculation"):
        nested_ols_f_test(full, reduced)


def test_unlabeled_arrays_require_separately_declared_matching_row_ids(fits):
    _, full, reduced = fits
    raw_full = sm.OLS(full.model.endog, full.model.exog).fit()
    raw_reduced = sm.OLS(reduced.model.endog, reduced.model.exog).fit()
    ids = list(range(100, 124))
    with pytest.raises(AnalysisInvariantError, match="explicit original row_ids"):
        nested_ols_f_test(raw_full, raw_reduced)
    observed = nested_ols_f_test(raw_full, raw_reduced, row_ids=ids, restricted_row_ids=ids)
    assert observed["df_den"] == 20
    with pytest.raises(AnalysisInvariantError, match="same ordered sample"):
        nested_ols_f_test(raw_full, raw_reduced, row_ids=ids, restricted_row_ids=ids[::-1])


@pytest.mark.parametrize("provided,expected_error", [
    ("restricted_row_ids", "Unlabeled full model requires explicit original row_ids"),
    ("row_ids", "Unlabeled restricted model requires explicit original restricted_row_ids"),
])
def test_missing_model_row_ids_identifies_the_side_without_inferring_or_echoing(fits, monkeypatch, provided, expected_error):
    _, full, reduced = fits
    raw_full = sm.OLS(full.model.endog, full.model.exog).fit()
    raw_reduced = sm.OLS(reduced.model.endog, reduced.model.exog).fit()
    def unexpected_test(_):
        raise AssertionError("Identity checks must precede the statistical test")
    monkeypatch.setattr(raw_full, "compare_f_test", unexpected_test)
    declared = [f"private-source-row-{index}" for index in range(24)]
    with pytest.raises(AnalysisInvariantError) as error:
        nested_ols_f_test(raw_full, raw_reduced, **{provided: declared})
    assert str(error.value) == expected_error
    assert "private-source" not in str(error.value)


@pytest.mark.parametrize("declared", [[1] * 24, list(range(24)), [None] + list(range(23))])
def test_declared_row_ids_cannot_relabel_model_observations(fits, declared):
    _, full, reduced = fits
    with pytest.raises(AnalysisInvariantError):
        nested_ols_f_test(full, reduced, row_ids=declared)


@pytest.mark.parametrize("robust,use_t", [(False, True), (True, True), (True, False)])
def test_linear_contrast_matches_independent_matrix_and_inference(fits, robust, use_t):
    _, base, _ = fits
    fit = base.get_robustcov_results(cov_type="HC3", use_t=use_t) if robust else base
    c = np.array([0., 1., 0., 2.])
    result = linear_contrast(fit, c, null=.25, alpha=.1)
    x = base.model.exog
    beta, residual, sse = independent_fit(base)
    bread = np.linalg.inv(x.T @ x)
    if robust:
        h = np.diag(x @ bread @ x.T)
        cov = bread @ (x.T @ (((residual / (1 - h)) ** 2)[:, None] * x)) @ bread
    else:
        cov = bread * sse / 20
    estimate = c @ beta
    se = np.sqrt(c @ cov @ c)
    statistic = (estimate - .25) / se
    law = stats.t(20) if use_t else stats.norm
    assert result["estimate"] == pytest.approx(estimate, rel=1e-11)
    assert result["standard_error"] == pytest.approx(se, rel=1e-11)
    assert result["statistic"] == pytest.approx(statistic, rel=1e-11)
    assert result["p"] == pytest.approx(2 * law.sf(abs(statistic)), rel=1e-10)
    assert result["ci_low"] == pytest.approx(estimate - law.ppf(.95) * se)
    assert result["ci_high"] == pytest.approx(estimate + law.ppf(.95) * se)
    assert result["distribution"] == ("t" if use_t else "normal")
    assert result["covariance_type"] == ("HC3" if robust else "nonrobust")
    assert result["df"] == (20 if use_t else None)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("contrast", [[1., 2.], [[1, 2], [3, 4]], [0., 1., np.nan, 2.], [0., 0., 0., 0.]])
def test_linear_contrast_rejects_wrong_dimensions_nonfinite_and_zero_variance(fits, contrast):
    with pytest.raises(AnalysisInvariantError):
        linear_contrast(fits[1], contrast)


@pytest.mark.parametrize("alpha", [0, 1, -1, float("nan")])
def test_contrast_confidence_level_cannot_be_invalid(fits, alpha):
    with pytest.raises(AnalysisInvariantError):
        linear_contrast(fits[1], [0, 1, 0, 0], alpha=alpha)


def test_linear_contrast_checks_returned_probabilities(fits, monkeypatch):
    _, fit, _ = fits
    original = fit.t_test
    def wrong(*args, **kwargs):
        value = original(*args, **kwargs)
        value.pvalue = .123
        return value
    monkeypatch.setattr(fit, "t_test", wrong)
    with pytest.raises(AnalysisInvariantError, match="Contrast probability"):
        linear_contrast(fit, [0, 1, 0, 0])


def test_tiny_covariance_does_not_hide_negative_variance_directions(fits, monkeypatch):
    table, _, _ = fits
    fit = ols("y ~ x * z", data=table.assign(y=table.y * 1e-12)).fit()
    covariance = np.asarray(fit.cov_params())
    covariance[0, 0] = -1e-20
    monkeypatch.setattr(fit, "cov_params", lambda: covariance)
    with pytest.raises(AnalysisInvariantError, match="positive semidefinite"):
        linear_contrast(fit, [0, 1, 0, 0])


@pytest.mark.parametrize("center", ["median", "mean", "trimmed"])
def test_residual_levene_uses_fitted_residuals_and_independent_deviation_formula(fits, center):
    table, fit, _ = fits
    result = residual_levene(fit, table.group, expected_groups=["a", "b"], center=center)
    _, residual, _ = independent_fit(fit)
    groups = [residual[(table.group == label).to_numpy()] for label in ["a", "b"]]
    if center == "trimmed":
        groups = [np.sort(x)[int(.05 * len(x)):len(x) - int(.05 * len(x))] for x in groups]
    operation = np.median if center == "median" else np.mean
    deviations = [np.abs(x - operation(x)) for x in groups]
    means = [d.mean() for d in deviations]
    grand = np.concatenate(deviations).mean()
    between = sum(len(d) * (m - grand) ** 2 for d, m in zip(deviations, means))
    within = sum(np.sum((d - m) ** 2) for d, m in zip(deviations, means))
    df_den = sum(len(x) for x in groups) - 2
    expected = between / (within / df_den)
    assert result["statistic"] == pytest.approx(expected, rel=1e-10)
    assert result["p"] == pytest.approx(stats.f.sf(expected, 1, df_den), rel=1e-10)
    assert result["group_sizes"] == [12, 12]
    assert result["nobs"] == 24
    assert result["source"] == "fitted_model_response_residuals"
    # The historical substitution (observations minus group means) is different.
    wrong_groups = [(table.loc[table.group == label, "y"] - table.loc[table.group == label, "y"].mean()).to_numpy()
                    for label in ["a", "b"]]
    assert not np.isclose(result["statistic"], stats.levene(*wrong_groups, center=center).statistic)
    json.dumps(result, allow_nan=False)


def test_residual_levene_rejects_reset_or_reordered_labels(fits):
    table, fit, _ = fits
    for labels in [table.group.reset_index(drop=True), table.group.iloc[::-1]]:
        with pytest.raises(AnalysisInvariantError, match="index"):
            residual_levene(fit, labels)


def test_residual_levene_rejects_missing_declared_categorical_and_singleton_groups(fits):
    table, fit, _ = fits
    with pytest.raises(AnalysisInvariantError, match="missing or unexpected"):
        residual_levene(fit, table.group, expected_groups=["a", "b", "c"])
    categorical = table.group.astype(pd.CategoricalDtype(["a", "b", "c"]))
    with pytest.raises(AnalysisInvariantError, match="missing or unexpected"):
        residual_levene(fit, categorical)
    missing = table.group.copy(); missing.iloc[3] = None
    with pytest.raises(AnalysisInvariantError, match="Missing group"):
        residual_levene(fit, missing)
    singleton = table.group.copy(); singleton.iloc[0] = "c"
    with pytest.raises(AnalysisInvariantError, match="two observations"):
        residual_levene(fit, singleton)
    with pytest.raises(AnalysisInvariantError, match="two groups"):
        residual_levene(fit, ["a"] * len(table))


def test_model_residuals_cannot_be_replaced_by_group_demeaning(fits, monkeypatch):
    table, fit, _ = fits
    wrong = table.y - table.groupby("group").y.transform("mean")
    monkeypatch.setattr(fit, "resid", wrong)
    with pytest.raises(AnalysisInvariantError, match="Model response residuals"):
        residual_levene(fit, table.group)


def test_bad_numeric_receipt_does_not_echo_private_values(fits, monkeypatch):
    _, fit, _ = fits
    monkeypatch.setattr(fit, "params", np.array([1., 2., 3., float("nan")]))
    with pytest.raises(AnalysisInvariantError) as error:
        linear_contrast(fit, [0, 1, 0, 0])
    assert str(error.value) == "Model coefficients has an invalid shape or nonfinite values"


def test_invalid_conversion_does_not_expose_original_values_in_exception_chain(fits):
    import traceback
    with pytest.raises(AnalysisInvariantError) as error:
        linear_contrast(fits[1], [0, "private-patient-123", 0, 0])
    rendered = "".join(traceback.format_exception_only(type(error.value), error.value))
    assert "private-patient-123" not in rendered
    assert error.value.__suppress_context__ is True
