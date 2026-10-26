"""Checked linear-model calculations with explicit, JSON-compatible results.

These optional helpers check the named numerical and sample-identity conditions;
they do not establish experimental design, scientific assumptions or causality.
No source files are read or copied. Model row labels are retained exactly: supply
explicit identities when fitting from unlabeled arrays, never invent an index
after filtering. An unsupported model/method raises rather than substituting a
different analysis. Use ordinary library APIs for methods outside this scope.
"""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math

import numpy as np
import pandas as pd
from scipy import stats

if __package__:
    from .datalab_analysis import AnalysisInvariantError, RowPartition, aligned_groups
else:
    from datalab_analysis import AnalysisInvariantError, RowPartition, aligned_groups


def _number(value, label):
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError):
        raise AnalysisInvariantError(f"{label} must be one finite numeric value") from None
    if array.size != 1 or not np.isfinite(array).all():
        raise AnalysisInvariantError(f"{label} must be one finite numeric value")
    return float(array.reshape(-1)[0])


def _array(value, label, dimensions):
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError):
        raise AnalysisInvariantError(f"{label} must be a finite numeric array") from None
    if array.ndim != dimensions or not np.isfinite(array).all():
        raise AnalysisInvariantError(f"{label} has an invalid shape or nonfinite values")
    return array


def _close(actual, expected, label, *, probability=False, rounding_scale=None):
    actual, expected = np.asarray(actual), np.asarray(expected)
    # Numerical units must not determine whether a wrong result passes. The
    # absolute allowance is floating-point rounding at an explicit operation
    # scale, never a fixed 1e-10 in the caller's physical units. Probabilities
    # need a relative comparison even when extremely small or underflowed.
    scale = np.max(np.abs(expected)) if rounding_scale is None else rounding_scale
    rounding = 0 if probability else 64 * np.finfo(float).eps * np.asarray(scale)
    if (not np.isfinite(actual).all() or not np.isfinite(expected).all()
            or not np.isfinite(np.asarray(scale)).all() or np.any(np.asarray(scale) < 0)
            or not np.all(np.abs(actual - expected) <= 1e-7 * np.abs(expected) + rounding)):
        raise AnalysisInvariantError(f"{label} does not match the independent calculation")


def _identity_value(value):
    if isinstance(value, np.generic):
        value = value.item()
    if type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if type(value) is float and math.isfinite(value):
        return ["float", value.hex()]
    if isinstance(value, (datetime, date)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, tuple):
        return ["tuple", [_identity_value(item) for item in value]]
    raise AnalysisInvariantError("Row identities need stable scalar or tuple representations")


def _rows(result, declared, count):
    observed = getattr(getattr(result.model, "data", None), "row_labels", None)
    if observed is None and declared is None:
        raise AnalysisInvariantError("Unlabeled model data requires explicit original row_ids")
    ids = tuple(observed if declared is None else declared)
    RowPartition(ids).verify(ids)
    if len(ids) != count:
        raise AnalysisInvariantError("Row identity count does not match the fitted sample")
    if observed is not None and ids != tuple(observed):
        raise AnalysisInvariantError("Declared row identities differ from the fitted row order")
    for name in ("resid", "fittedvalues"):
        value = getattr(result, name, None)
        if isinstance(value, pd.Series) and tuple(value.index) != ids:
            raise AnalysisInvariantError(f"Model {name} index differs from the fitted row order")
    encoded = json.dumps([_identity_value(item) for item in ids], ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    return ids, hashlib.sha256(encoded).hexdigest()


def _model(result, declared=None, *, ordinary=False):
    try:
        model = result.model
        x = _array(model.exog, "Model design", 2)
        y = _array(model.endog, "Model response", 1)
        params = _array(result.params, "Model coefficients", 1)
        residuals = _array(result.resid, "Model residuals", 1)
    except AttributeError:
        raise AnalysisInvariantError("A fitted linear model with design, response and residuals is required") from None
    n, p = x.shape
    if not p or n <= p or y.shape != (n,) or params.shape != (p,) or residuals.shape != (n,):
        raise AnalysisInvariantError("Model dimensions or residual degrees of freedom are invalid")
    # Normalize each design column before rank/nesting checks: a small-unit
    # predictor is not a zero column, and an intercept must not set its tolerance.
    column_scale = np.max(np.abs(x), axis=0)
    if np.any(column_scale == 0):
        raise AnalysisInvariantError("Rank-deficient model design requires an explicit method outside this helper")
    normalized = x / column_scale
    singular_values = np.linalg.svd(normalized, compute_uv=False)
    rank = int(np.linalg.matrix_rank(normalized))
    if rank != p:
        raise AnalysisInvariantError("Rank-deficient model design requires an explicit method outside this helper")
    condition = float(singular_values[0] / singular_values[-1])
    if condition > 1 / np.sqrt(np.finfo(float).eps):
        raise AnalysisInvariantError("Model design is too ill-conditioned for this numerical contract")
    _close(_number(result.nobs, "Model nobs"), n, "Model nobs")
    df = _number(result.df_resid, "Residual degrees of freedom")
    _close(df, n - rank, "Residual degrees of freedom")
    operation_scale = np.abs(y) + np.abs(x) @ np.abs(params)
    _close(residuals, y - x @ params, "Model response residuals", rounding_scale=operation_scale)
    ids, identity = _rows(result, declared, n)
    if ordinary:
        if getattr(result, "cov_type", None) != "nonrobust":
            raise AnalysisInvariantError("Classical nested OLS F test requires nonrobust covariance; do not replace a robust test")
        for name, expected in (("wexog", x), ("wendog", y)):
            transformed = getattr(model, name, None)
            if transformed is None:
                raise AnalysisInvariantError("Ordinary unweighted model coordinates could not be verified")
            transformed = _array(transformed, "Model transformed coordinates", expected.ndim)
            if transformed.shape != expected.shape:
                raise AnalysisInvariantError("Ordinary unweighted model coordinates differ")
            if not np.array_equal(transformed, expected):
                raise AnalysisInvariantError("Ordinary unweighted model coordinates differ")
        coefficients = np.linalg.lstsq(normalized, y, rcond=None)[0]
        _close(params * column_scale, coefficients, "OLS coefficients",
               rounding_scale=max(n, p) * condition * np.max(np.abs(y)))
    return {"x": x, "y": y, "params": params, "residuals": residuals,
            "n": n, "p": p, "df": df, "ids": ids, "identity": identity,
            "normalized_design": normalized, "condition": condition}


def nested_ols_f_test(full, restricted, *, row_ids=None, restricted_row_ids=None) -> dict:
    """Return named F, p, df_num and df_den for classical nested OLS models.

    Checks the identical ordered sample/response, full-rank ordinary fits, nested
    design spaces, residual degrees of freedom and F/p numerical identities.
    ``df_num`` is the difference in model ranks; ``df_den`` belongs to the full
    model, NOT the third value returned by ``compare_f_test``. Both fits must use
    nonrobust covariance. Independence/homoscedasticity and scientific model
    selection remain the caller's responsibility.

    Prefer fitting from DataFrame/Series inputs that preserve the original
    retained row index. With unlabeled arrays, separately declare both models'
    row identities using ``row_ids`` and ``restricted_row_ids``. They may refer
    to the same retained IDs, but neither argument is inferred from the other:
    ``nested_ols_f_test(full, restricted, row_ids=retained_ids,
    restricted_row_ids=retained_ids)``. Preserve the actual fitting order.

    Returns
    -------
    dict
        A plain dict, not an attribute-access result object. Use ``result['F']``
        and ``result['df_den']``, not ``result.F`` or ``result.df_den``.
        ``F`` (float) is the nonnegative test statistic; ``p`` (float) is its
        upper-tail probability. ``df_num`` (int) is the added parameter count;
        ``df_den`` (float) is the FULL model's residual degrees of freedom.
        ``nobs`` (int) is the fitted sample size. ``method`` (str) identifies
        the classical nested OLS test. ``sample_identity`` (str) hashes ordered
        row IDs only; ``sample_identity_scope`` (str) records that limitation,
        not an input-file version. ``checks`` (list[str]) names the performed
        numerical/structural checks, not a certificate of scientific validity.
        No returned field is nullable.
    """
    for result, declared, side, argument in (
        (full, row_ids, "full", "row_ids"),
        (restricted, restricted_row_ids, "restricted", "restricted_row_ids"),
    ):
        model = getattr(result, "model", None)
        if (model is not None and declared is None
                and getattr(getattr(model, "data", None), "row_labels", None) is None):
            raise AnalysisInvariantError(f"Unlabeled {side} model requires explicit original {argument}")
    fitted = _model(full, row_ids, ordinary=True)
    reduced = _model(restricted, restricted_row_ids, ordinary=True)
    if fitted["ids"] != reduced["ids"] or not np.array_equal(fitted["y"], reduced["y"]):
        raise AnalysisInvariantError("Nested models must use the same ordered sample and response")
    df_num = fitted["p"] - reduced["p"]
    if df_num <= 0:
        raise AnalysisInvariantError("Full model must add at least one independent parameter")
    basis, _ = np.linalg.qr(fitted["normalized_design"], mode="reduced")
    restricted_unit = reduced["normalized_design"]
    projection = basis @ (basis.T @ restricted_unit)
    residual_norms = np.linalg.norm(restricted_unit - projection, axis=0)
    column_norms = np.linalg.norm(restricted_unit, axis=0)
    # QR gives an orthonormal basis after a separately checked conditioning
    # bound. Check each restricted column's relative projection error; do not
    # let a large intercept conceal a small-unit nonnested predictor.
    tolerance = 128 * np.finfo(float).eps * max(fitted["n"], fitted["p"])
    if np.any(residual_norms > tolerance * column_norms):
        raise AnalysisInvariantError("Restricted design nesting does not match the fitted model subspace")
    ss_full = float(fitted["residuals"] @ fitted["residuals"])
    ss_restricted = float(reduced["residuals"] @ reduced["residuals"])
    precision_scale = np.linalg.norm(fitted["y"]) + np.linalg.norm(fitted["x"]) * np.linalg.norm(fitted["params"])
    unresolved_residual = np.linalg.norm(fitted["residuals"]) <= 32 * np.finfo(float).eps * precision_scale
    if (not math.isfinite(ss_full) or not math.isfinite(ss_restricted)
            or ss_full <= 0 or unresolved_residual or ss_restricted < ss_full):
        raise AnalysisInvariantError("Residual sums of squares do not support a finite nested-model test")
    expected_f = ((ss_restricted - ss_full) / df_num) / (ss_full / fitted["df"])
    try:
        actual_f, actual_p, actual_difference = full.compare_f_test(restricted)
    except (AttributeError, TypeError, ValueError):
        raise AnalysisInvariantError("The fitted model could not perform its nested F test") from None
    f = _number(actual_f, "F statistic")
    p = _number(actual_p, "F probability")
    difference = _number(actual_difference, "Model degrees-of-freedom difference")
    _close(difference, df_num, "Model degrees-of-freedom difference")
    _close(f, expected_f, "F statistic")
    expected_p = float(stats.f.sf(expected_f, df_num, fitted["df"]))
    _close(p, expected_p, "F probability", probability=True)
    if f < 0 or not 0 <= p <= 1:
        raise AnalysisInvariantError("F statistic or probability is outside its valid range")
    return {"method": "classical_nested_ols_f", "F": f, "p": p,
            "df_num": df_num, "df_den": fitted["df"], "nobs": fitted["n"],
            "sample_identity": fitted["identity"],
            "sample_identity_scope": "ordered_row_ids_only",
            "checks": ["same_ordered_sample", "same_response", "ordinary_full_rank_fits",
                       "nested_design", "residual_degrees_of_freedom", "F_identity", "F_probability"]}


def linear_contrast(result, contrast, *, null=0.0, alpha=0.05, row_ids=None) -> dict:
    """Check and compute one linear contrast, preserving fitted covariance.

    ``contrast`` is a vector (or one-row matrix) in the fitted coefficient order.
    Uses the fitted result's ``t_test`` and t/normal convention without changing
    its covariance method. Independently checks estimate, covariance propagation,
    statistic, probability and confidence interval. It does not justify the
    selected contrast, covariance assumptions or multiple-testing policy.

    Returns
    -------
    dict
        A plain dict; read ``checked['estimate']`` and ``checked['p']`` using
        dictionary indexing, not attributes. ``estimate``, ``null``,
        ``standard_error``, ``statistic`` and ``p`` are floats for the contrast,
        tested null, propagated standard error, test statistic and two-sided
        probability. ``distribution`` (str) is 't' or 'normal'; ``df`` is a
        positive float for t inference and None for normal inference.
        ``confidence_level``, ``ci_low`` and ``ci_high`` are floats describing
        the interval for the estimate. ``covariance_type`` (str) preserves the
        fitted covariance method; ``method`` (str) is 'linear_contrast'.
        ``nobs`` (int) is the sample size. ``sample_identity`` and
        ``sample_identity_scope`` (str) describe ordered row IDs only, not an
        input-file version. ``checks`` (list[str]) lists performed checks;
        it does not establish scientific validity. Only ``df`` is nullable.
    """
    fitted = _model(result, row_ids)
    try:
        weights = np.asarray(contrast, dtype=float)
    except (TypeError, ValueError, OverflowError):
        raise AnalysisInvariantError("Contrast must be a finite coefficient vector") from None
    if weights.shape == (1, fitted["p"]):
        weights = weights[0]
    if weights.shape != (fitted["p"],) or not np.isfinite(weights).all():
        raise AnalysisInvariantError("Contrast dimension must match the fitted coefficients")
    null = _number(null, "Contrast null")
    alpha = _number(alpha, "Confidence alpha")
    if not 0 < alpha < 1:
        raise AnalysisInvariantError("Confidence alpha must lie strictly between zero and one")
    try:
        covariance = _array(result.cov_params(), "Coefficient covariance", 2)
    except (AttributeError, TypeError, ValueError):
        raise AnalysisInvariantError("Coefficient covariance is required") from None
    if covariance.shape != (fitted["p"], fitted["p"]):
        raise AnalysisInvariantError("Coefficient covariance dimension does not match the model")
    _close(covariance, covariance.T, "Coefficient covariance symmetry")
    covariance_scale = float(np.max(np.abs(covariance)))
    scaled_covariance = covariance / covariance_scale if covariance_scale else covariance
    eigenvalues = np.linalg.eigvalsh((scaled_covariance + scaled_covariance.T) / 2)
    if eigenvalues.min() < -64 * np.finfo(float).eps * fitted["p"] * float(np.max(np.abs(eigenvalues))):
        raise AnalysisInvariantError("Coefficient covariance is not positive semidefinite")
    variance = float(weights @ covariance @ weights)
    if not math.isfinite(variance) or variance <= 0:
        raise AnalysisInvariantError("Contrast variance must be finite and positive")
    estimate = float(weights @ fitted["params"])
    standard_error = math.sqrt(variance)
    statistic = (estimate - null) / standard_error
    use_t = getattr(result, "use_t", None)
    if not isinstance(use_t, (bool, np.bool_)):
        raise AnalysisInvariantError("The fitted t-versus-normal inference convention is unavailable")
    df = None
    if use_t:
        df = _number(getattr(result, "df_resid_inference", result.df_resid), "Inference degrees of freedom")
        if df <= 0:
            raise AnalysisInvariantError("Inference degrees of freedom must be positive")
        distribution = stats.t(df)
    else:
        distribution = stats.norm
    expected_p = float(2 * distribution.sf(abs(statistic)))
    critical = float(distribution.ppf(1 - alpha / 2))
    expected_interval = np.array([estimate - critical * standard_error, estimate + critical * standard_error])
    try:
        observed = result.t_test((weights.reshape(1, -1), np.array([null])))
        interval = np.asarray(observed.conf_int(alpha=alpha), dtype=float)
    except (AttributeError, TypeError, ValueError):
        raise AnalysisInvariantError("The fitted model could not perform its linear contrast") from None
    _close(_number(observed.effect, "Contrast estimate"), estimate, "Contrast estimate")
    _close(_number(observed.sd, "Contrast standard error"), standard_error, "Contrast standard error")
    _close(_number(observed.statistic, "Contrast statistic"), statistic, "Contrast statistic")
    p = _number(observed.pvalue, "Contrast probability")
    _close(p, expected_p, "Contrast probability", probability=True)
    if interval.shape != (1, 2) or not np.isfinite(interval).all():
        raise AnalysisInvariantError("Contrast confidence interval has an invalid shape or values")
    _close(interval[0], expected_interval, "Contrast confidence interval")
    if use_t:
        _close(_number(observed.df_denom, "Contrast degrees of freedom"), df, "Contrast degrees of freedom")
    return {"method": "linear_contrast", "estimate": estimate, "null": null,
            "covariance_type": getattr(result, "cov_type", "unspecified"),
            "standard_error": standard_error, "statistic": statistic, "p": p,
            "distribution": "t" if use_t else "normal", "df": df,
            "confidence_level": 1 - alpha, "ci_low": float(interval[0, 0]),
            "ci_high": float(interval[0, 1]), "nobs": fitted["n"],
            "sample_identity": fitted["identity"],
            "sample_identity_scope": "ordered_row_ids_only",
            "checks": ["coefficient_dimensions", "row_identity", "covariance_propagation",
                       "contrast_estimate", "statistic", "probability", "confidence_interval"]}


def residual_levene(result, labels, *, row_ids=None, expected_groups=None, center="median") -> dict:
    """Levene test of the supplied fitted model's response residuals.

    Uses ``result.resid``; never substitutes observations demeaned within groups.
    Indexed labels must exactly match retained model rows. ``expected_groups``
    declares all requested groups; empty/unexpected groups are rejected. For
    categorical labels, categories supply that declaration when omitted. Each
    group needs at least two finite observations. The caller must justify using
    this diagnostic on fitted residuals and the chosen grouping.

    Returns
    -------
    dict
        A plain dict; use ``checked['statistic']`` and ``checked['p']``, not
        attribute access. ``statistic`` and ``p`` (float) are the Levene F
        statistic and upper-tail probability; ``df_num`` and ``df_den`` (int)
        are its reference F degrees of freedom. ``group_count`` and ``nobs``
        (int) count observed groups and fitted rows. ``group_sizes`` and
        ``effective_group_sizes`` (list[int]) give original and post-trimming
        sizes in first-observed group order. ``center`` (str) records the
        centering method; ``trim_fraction_per_tail`` (float) is 0.05 for
        trimmed centering and 0.0 otherwise. ``method`` and ``source`` (str)
        identify the residual diagnostic and fitted-model response residuals.
        ``sample_identity`` and ``sample_identity_scope`` (str) describe ordered
        row IDs only, not an input-file version. ``checks`` (list[str]) names
        performed checks, not scientific validity. No field is nullable.
    """
    fitted = _model(result, row_ids)
    if center not in {"mean", "median", "trimmed"}:
        raise AnalysisInvariantError("Levene center must be mean, median or trimmed")
    groups = aligned_groups(result.resid, row_ids=fitted["ids"], labels=labels)
    if expected_groups is None:
        if isinstance(getattr(labels, "dtype", None), pd.CategoricalDtype):
            expected_groups = labels.cat.categories if isinstance(labels, pd.Series) else labels.categories
    if expected_groups is not None:
        expected = tuple(expected_groups)
        RowPartition(expected).verify(expected)
        if set(expected) != set(groups):
            raise AnalysisInvariantError("Declared Levene groups include missing or unexpected groups")
    arrays = list(groups.values())
    if len(arrays) < 2 or any(len(values) < 2 for values in arrays):
        raise AnalysisInvariantError("Levene needs at least two groups with two observations each")
    observed = stats.levene(*arrays, center=center)
    statistic = _number(observed.statistic, "Levene statistic")
    p = _number(observed.pvalue, "Levene probability")
    if statistic < 0 or not 0 <= p <= 1:
        raise AnalysisInvariantError("Levene statistic or probability is outside its valid range")
    sizes = [len(values) for values in arrays]
    # The trimmed convention changes the effective sample; reflect SciPy's
    # 5% per-tail default explicitly and check its actual F reference degrees.
    included = ([stats.trimboth(np.sort(values), 0.05) for values in arrays]
                if center == "trimmed" else arrays)
    effective = [len(values) for values in included]
    df_num, df_den = len(arrays) - 1, sum(effective) - len(arrays)
    center_function = np.median if center == "median" else np.mean
    deviations = [np.abs(values - center_function(values)) for values in included]
    means = np.array([values.mean() for values in deviations])
    grand = float(sum(values.sum() for values in deviations) / sum(effective))
    between = float(np.sum(np.array(effective) * (means - grand) ** 2))
    within = float(sum(np.sum((values - mean) ** 2) for values, mean in zip(deviations, means)))
    if within <= 0 or df_den <= 0:
        raise AnalysisInvariantError("Levene absolute deviations do not support a finite variance test")
    _close(statistic, (between / df_num) / (within / df_den), "Levene statistic")
    _close(p, float(stats.f.sf(statistic, df_num, df_den)), "Levene probability", probability=True)
    return {"method": "residual_levene", "source": "fitted_model_response_residuals",
            "center": center, "trim_fraction_per_tail": 0.05 if center == "trimmed" else 0.0,
            "statistic": statistic, "p": p, "df_num": df_num, "df_den": df_den,
            "group_count": len(arrays), "group_sizes": sizes,
            "effective_group_sizes": effective, "nobs": fitted["n"],
            "sample_identity": fitted["identity"],
            "sample_identity_scope": "ordered_row_ids_only",
            "checks": ["model_response_residuals", "exact_row_alignment", "group_coverage",
                       "finite_values", "nonempty_groups", "absolute_deviation_ANOVA", "F_probability"]}
