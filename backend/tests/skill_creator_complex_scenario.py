"""Artificial multi-method study; expected values are independently hand-derived."""
import math


def comprehensive_study(data):
    import itertools
    import math
    import statistics

    def number(value, label, nullable=False):
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(label + " must be a finite non-boolean number")
        return value

    def text(value, label):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(label + " must be a nonempty string")
        return value

    def quantile(values, probability):
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    def describe(values):
        count = len(values)
        if not count:
            return {"n": 0, "sum": 0, "mean": None, "median": None, "q1": None, "q3": None,
                    "minimum": None, "maximum": None, "sample_sd": None, "standard_error": None}
        mean = statistics.mean(values)
        variance = sum((value - mean) ** 2 for value in values) / (count - 1) if count > 1 else None
        return {"n": count, "sum": sum(values), "mean": mean, "median": quantile(values, .5),
                "q1": quantile(values, .25), "q3": quantile(values, .75), "minimum": min(values),
                "maximum": max(values), "sample_sd": math.sqrt(variance) if variance is not None else None,
                "standard_error": math.sqrt(variance / count) if variance is not None else None}

    def rank(values):
        order = sorted(range(len(values)), key=lambda index: values[index])
        ranks = [0.0] * len(values)
        cursor = 0
        while cursor < len(order):
            end = cursor + 1
            while end < len(order) and values[order[end]] == values[order[cursor]]:
                end += 1
            average = (cursor + 1 + end) / 2
            for position in range(cursor, end):
                ranks[order[position]] = average
            cursor = end
        return ranks

    def pearson(x, y):
        if len(x) < 2:
            return None
        mx, my = statistics.mean(x), statistics.mean(y)
        xx = sum((value - mx) ** 2 for value in x)
        yy = sum((value - my) ** 2 for value in y)
        xy = sum((left - mx) * (right - my) for left, right in zip(x, y))
        return xy / math.sqrt(xx * yy) if xx > 0 and yy > 0 else None

    if not isinstance(data, dict):
        raise ValueError("top-level input must be an object")
    for table in ("observations", "pairs", "associations", "hypotheses", "long_rows"):
        if not isinstance(data.get(table), list) or any(not isinstance(row, dict) for row in data[table]):
            raise ValueError(table + " must be an array of objects")
    fields = data.get("fields", {})
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    roles = ("id", "group", "feature", "value", "unit", "before", "after", "x", "y", "family", "p")
    if set(fields) - set(roles):
        raise ValueError("unknown field role")
    mapping = {role: text(fields.get(role, role), role) for role in roles}
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("field mappings must be distinct")
    units = data.get("units", {"mg": 1, "ug": .001})
    if not isinstance(units, dict) or not units:
        raise ValueError("units must be a nonempty object")
    for unit, factor in units.items():
        text(unit, "unit")
        if number(factor, "unit factor") <= 0:
            raise ValueError("unit factor must be positive")
    alpha = number(data.get("alpha", .05), "alpha")
    mad_threshold = number(data.get("mad_threshold", 3.5), "mad_threshold")
    iqr_multiplier = number(data.get("iqr_multiplier", 1.5), "iqr_multiplier")
    scale = number(data.get("scale", 1), "scale")
    if not 0 < alpha < 1 or mad_threshold <= 0 or iqr_multiplier <= 0 or scale <= 0:
        raise ValueError("invalid alpha or positive threshold/scale")
    group_a = text(data.get("group_a", "A"), "group_a")
    group_b = text(data.get("group_b", "B"), "group_b")
    if group_a == group_b:
        raise ValueError("comparison groups must differ")

    def field(row, role):
        if mapping[role] not in row:
            raise ValueError("missing field role " + role)
        return row[mapping[role]]

    # Unit harmonization, robust quality summaries, missingness and zero counts.
    buckets, seen_observations = {}, set()
    for row in data["observations"]:
        identifier = text(field(row, "id"), "observation id")
        group = text(field(row, "group"), "observation group")
        feature = text(field(row, "feature"), "observation feature")
        unit = text(field(row, "unit"), "observation unit")
        if unit not in units or (identifier, feature) in seen_observations:
            raise ValueError("unknown unit or duplicate observation cell")
        seen_observations.add((identifier, feature))
        value = number(field(row, "value"), "observation value", nullable=True)
        entry = buckets.setdefault((group, feature), {"values": [], "missing": 0, "rows": 0})
        entry["rows"] += 1
        if value is None:
            entry["missing"] += 1
        else:
            entry["values"].append(number(value * units[unit], "converted observation"))
    profiles = []
    for (group, feature), entry in sorted(buckets.items()):
        values = entry["values"]
        summary = describe(values)
        median = summary["median"]
        mad = quantile([abs(value - median) for value in values], .5) if values else None
        iqr = summary["q3"] - summary["q1"] if values else None
        mad_outliers = sum(abs(.67448975 * (value - median) / mad) > mad_threshold for value in values) if mad else 0
        iqr_outliers = sum(value < summary["q1"] - iqr_multiplier * iqr or value > summary["q3"] + iqr_multiplier * iqr for value in values) if values else 0
        profiles.append({"group": group, "feature": feature, "rows": entry["rows"], "missing": entry["missing"],
                         "zeros": sum(value == 0 for value in values), **summary, "mad": mad, "iqr": iqr,
                         "mad_outliers": mad_outliers, "iqr_outliers": iqr_outliers})

    # Pair completeness, exact sign-flip test, and group-specific change summaries.
    pair_groups, pair_ids = {}, set()
    for row in data["pairs"]:
        identifier = text(field(row, "id"), "pair id")
        group = text(field(row, "group"), "pair group")
        if identifier in pair_ids:
            raise ValueError("duplicate pair id")
        pair_ids.add(identifier)
        before = number(field(row, "before"), "before", nullable=True)
        after = number(field(row, "after"), "after", nullable=True)
        entry = pair_groups.setdefault(group, {"changes": [], "excluded": 0})
        if before is None or after is None:
            entry["excluded"] += 1
        else:
            entry["changes"].append(number(after - before, "change"))
    paired = []
    for group, entry in sorted(pair_groups.items()):
        values = entry["changes"]
        if not 2 <= len(values) <= 12:
            raise ValueError("each group requires 2 through 12 complete pairs")
        summary = describe(values)
        permutations = 2 ** len(values)
        extreme = sum(abs(sum(sign * value for sign, value in zip(signs, values)) / len(values)) >= abs(summary["mean"]) - 1e-12
                      for signs in itertools.product((-1, 1), repeat=len(values)))
        paired.append({"group": group, "excluded": entry["excluded"], **summary,
                       "permutations": permutations, "p_value": extreme / permutations,
                       "direction": "after-minus-before"})

    # Independent groups: exhaustive label permutations, Welch components, and Hedges g.
    if group_a not in pair_groups or group_b not in pair_groups:
        raise ValueError("both requested comparison groups are required")
    left, right = pair_groups[group_a]["changes"], pair_groups[group_b]["changes"]
    na, nb = len(left), len(right)
    if na + nb > 12:
        raise ValueError("exact group comparison supports at most 12 complete pairs")
    combined = left + right
    mean_a, mean_b = statistics.mean(left), statistics.mean(right)
    var_a, var_b = statistics.variance(left), statistics.variance(right)
    difference = mean_b - mean_a
    permutations = 0
    extreme = 0
    for selected in itertools.combinations(range(na + nb), na):
        selected = set(selected)
        a = [value for index, value in enumerate(combined) if index in selected]
        b = [value for index, value in enumerate(combined) if index not in selected]
        statistic = statistics.mean(b) - statistics.mean(a)
        permutations += 1
        extreme += abs(statistic) >= abs(difference) - 1e-12
    standard_error = math.sqrt(var_a / na + var_b / nb)
    denominator = (var_a / na) ** 2 / (na - 1) + (var_b / nb) ** 2 / (nb - 1)
    welch_df = (var_a / na + var_b / nb) ** 2 / denominator if denominator else None
    pooled_sd = math.sqrt(((na - 1) * var_a + (nb - 1) * var_b) / (na + nb - 2))
    cohen_d = difference / pooled_sd if pooled_sd else None
    correction = 1 - 3 / (4 * (na + nb - 2) - 1)
    comparison = {"group_a": group_a, "group_b": group_b, "n_a": na, "n_b": nb,
                  "mean_a": mean_a, "mean_b": mean_b, "difference_b_minus_a": difference,
                  "permutations": permutations, "p_value": extreme / permutations,
                  "standard_error": standard_error, "welch_t": difference / standard_error if standard_error else None,
                  "welch_df": welch_df, "pooled_sd": pooled_sd, "cohen_d": cohen_d,
                  "hedges_correction": correction, "hedges_g": cohen_d * correction if cohen_d is not None else None}

    # Pairwise complete-case association, midrank ties, and OLS with intercept.
    association_ids, xs, ys = set(), [], []
    excluded = 0
    for row in data["associations"]:
        identifier = text(field(row, "id"), "association id")
        if identifier in association_ids:
            raise ValueError("duplicate association id")
        association_ids.add(identifier)
        x, y = number(field(row, "x"), "x", True), number(field(row, "y"), "y", True)
        if x is None or y is None:
            excluded += 1
        else:
            xs.append(x)
            ys.append(y)
    n = len(xs)
    if n < 2:
        raise ValueError("association requires at least two complete rows")
    mx, my = statistics.mean(xs), statistics.mean(ys)
    xx = sum((value - mx) ** 2 for value in xs)
    yy = sum((value - my) ** 2 for value in ys)
    xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = xy / xx if xx else None
    intercept = my - slope * mx if slope is not None else None
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys)) if slope is not None else None
    association = {"n": n, "excluded": excluded, "mean_x": mx, "mean_y": my,
                   "covariance": xy / (n - 1), "pearson": pearson(xs, ys), "spearman": pearson(rank(xs), rank(ys)),
                   "slope": slope, "intercept": intercept, "residual_df": n - 2,
                   "sse": sse, "r_squared": 1 - sse / yy if sse is not None and yy else None,
                   "slope_se": math.sqrt(sse / (n - 2) / xx) if sse is not None and n > 2 and xx else None}

    # Four multiple-testing corrections with explicit family membership and null handling.
    families, hypothesis_ids = {}, set()
    for row in data["hypotheses"]:
        identifier = text(field(row, "id"), "hypothesis id")
        family = text(field(row, "family"), "family")
        p_value = number(field(row, "p"), "p", True)
        if identifier in hypothesis_ids or (p_value is not None and not 0 <= p_value <= 1):
            raise ValueError("duplicate hypothesis id or invalid p value")
        hypothesis_ids.add(identifier)
        families.setdefault(family, []).append((identifier, p_value))
    adjustments = []
    for family, entries in sorted(families.items()):
        ordered = sorted(((identifier, p) for identifier, p in entries if p is not None), key=lambda item: (item[1], item[0]))
        m = len(ordered)
        harmonic = sum(1 / index for index in range(1, m + 1))
        bh, by, holm = {}, {}, {}
        last_bh, last_by = 1.0, 1.0
        for index in range(m - 1, -1, -1):
            identifier, p = ordered[index]
            last_bh = min(last_bh, p * m / (index + 1))
            last_by = min(last_by, p * m * harmonic / (index + 1))
            bh[identifier], by[identifier] = last_bh, last_by
        last_holm = 0.0
        for index, (identifier, p) in enumerate(ordered):
            last_holm = max(last_holm, min(1, p * (m - index)))
            holm[identifier] = last_holm
        for identifier, p in sorted(entries):
            adjusted = {"bh": bh.get(identifier), "by": by.get(identifier), "holm": holm.get(identifier),
                        "bonferroni": min(1, p * m) if p is not None else None}
            adjustments.append({"family": family, "id": identifier, "p": p, "family_size": m, **adjusted,
                                **{"reject_" + method: value <= alpha if value is not None else None for method, value in adjusted.items()}})

    # Long-to-wide conversion: distinguish explicit missing entries and absent cells.
    wide, features, cells = {}, set(), set()
    explicit_missing = 0
    for row in data["long_rows"]:
        identifier = text(field(row, "id"), "long row id")
        feature = text(field(row, "feature"), "long row feature")
        value = number(field(row, "value"), "long row value", True)
        if feature == "id" or (identifier, feature) in cells:
            raise ValueError("reserved feature or duplicate long cell")
        cells.add((identifier, feature))
        features.add(feature)
        wide.setdefault(identifier, {})[feature] = number(value * scale, "scaled long value") if value is not None else None
        explicit_missing += value is None
    features = sorted(features)
    wide_rows = [{"id": identifier, **{feature: values.get(feature) for feature in features}} for identifier, values in sorted(wide.items())]
    pivot = {"columns": ["id", *features], "rows": wide_rows,
             "quality": {"source_rows": len(data["long_rows"]), "output_rows": len(wide_rows),
                         "observed_missing": explicit_missing, "absent_cells": len(wide_rows) * len(features) - len(cells)}}
    quality = {"observation_rows": len(data["observations"]), "profile_count": len(profiles),
               "complete_pairs": sum(len(entry["changes"]) for entry in pair_groups.values()),
               "excluded_pairs": sum(entry["excluded"] for entry in pair_groups.values()),
               "association_complete": n, "association_excluded": excluded, "hypotheses": len(hypothesis_ids)}
    return {"profiles": profiles, "paired": paired, "comparison": comparison, "association": association,
            "adjustments": adjustments, "pivot": pivot, "quality": quality}


COMPLEX_INPUT = {
    "observations": [
        {"id": "o1", "group": "A", "feature": "signal", "value": 0, "unit": "mg"},
        {"id": "o2", "group": "A", "feature": "signal", "value": 1000, "unit": "ug"},
        {"id": "o3", "group": "A", "feature": "signal", "value": 2, "unit": "mg"},
        {"id": "o4", "group": "A", "feature": "signal", "value": 3, "unit": "mg"},
        {"id": "o5", "group": "A", "feature": "signal", "value": 100, "unit": "mg"},
        {"id": "o6", "group": "A", "feature": "signal", "value": None, "unit": "ug"}],
    "pairs": [
        {"id": "p1", "group": "A", "before": 1, "after": 2},
        {"id": "p2", "group": "A", "before": 2, "after": 5},
        {"id": "p3", "group": "B", "before": 3, "after": 5},
        {"id": "p4", "group": "B", "before": 4, "after": 8},
        {"id": "p5", "group": "A", "before": None, "after": 2}],
    "associations": [
        {"id": "r1", "x": 1, "y": 2}, {"id": "r2", "x": 2, "y": 1},
        {"id": "r3", "x": 2, "y": 3}, {"id": "r4", "x": 4, "y": 4},
        {"id": "r5", "x": None, "y": 5}],
    "hypotheses": [
        {"id": "h1", "family": "f1", "p": .01}, {"id": "h2", "family": "f1", "p": .04},
        {"id": "h3", "family": "f1", "p": .03}, {"id": "h4", "family": "f1", "p": None},
        {"id": "h5", "family": "f2", "p": 0}, {"id": "h6", "family": "f2", "p": 1}],
    "long_rows": [{"id": "b", "feature": "z", "value": None}, {"id": "a", "feature": "x", "value": 0},
                  {"id": "b", "feature": "x", "value": 3}],
    "scale": 2,
}

# Each value below comes from hand arithmetic, not comprehensive_study's output.
COMPLEX_EXPECTED = {
    "profiles": [{"group": "A", "feature": "signal", "rows": 6, "missing": 1, "zeros": 1,
                  "n": 5, "sum": 106, "mean": 21.2, "median": 2, "q1": 1, "q3": 3,
                  "minimum": 0, "maximum": 100, "sample_sd": math.sqrt(1941.7),
                  "standard_error": math.sqrt(1941.7 / 5), "mad": 1, "iqr": 2, "mad_outliers": 1, "iqr_outliers": 1}],
    "paired": [
        {"group": "A", "excluded": 1, "n": 2, "sum": 4, "mean": 2, "median": 2, "q1": 1.5, "q3": 2.5,
         "minimum": 1, "maximum": 3, "sample_sd": math.sqrt(2), "standard_error": 1, "permutations": 4,
         "p_value": .5, "direction": "after-minus-before"},
        {"group": "B", "excluded": 0, "n": 2, "sum": 6, "mean": 3, "median": 3, "q1": 2.5, "q3": 3.5,
         "minimum": 2, "maximum": 4, "sample_sd": math.sqrt(2), "standard_error": 1, "permutations": 4,
         "p_value": .5, "direction": "after-minus-before"}],
    "comparison": {"group_a": "A", "group_b": "B", "n_a": 2, "n_b": 2, "mean_a": 2, "mean_b": 3,
                   "difference_b_minus_a": 1, "permutations": 6, "p_value": 2 / 3, "standard_error": math.sqrt(2),
                   "welch_t": 1 / math.sqrt(2), "welch_df": 2, "pooled_sd": math.sqrt(2),
                   "cohen_d": 1 / math.sqrt(2), "hedges_correction": 4 / 7, "hedges_g": 4 / 7 / math.sqrt(2)},
    "association": {"n": 4, "excluded": 1, "mean_x": 2.25, "mean_y": 2.5, "covariance": 7 / 6,
                    "pearson": 7 / math.sqrt(95), "spearman": math.sqrt(.4), "slope": 14 / 19,
                    "intercept": 16 / 19, "residual_df": 2, "sse": 46 / 19, "r_squared": 49 / 95,
                    "slope_se": 2 * math.sqrt(23) / 19},
    "adjustments": [],
    "pivot": {"columns": ["id", "x", "z"], "rows": [{"id": "a", "x": 0, "z": None}, {"id": "b", "x": 6, "z": None}],
              "quality": {"source_rows": 3, "output_rows": 2, "observed_missing": 1, "absent_cells": 1}},
    "quality": {"observation_rows": 6, "profile_count": 1, "complete_pairs": 4, "excluded_pairs": 1,
                "association_complete": 4, "association_excluded": 1, "hypotheses": 6},
}
for identifier, family, p, size, bh, by, holm, bonferroni in (
    ("h1", "f1", .01, 3, .03, .055, .03, .03),
    ("h2", "f1", .04, 3, .04, 11 / 150, .06, .12),
    ("h3", "f1", .03, 3, .04, 11 / 150, .06, .09),
    ("h4", "f1", None, 3, None, None, None, None),
    ("h5", "f2", 0, 2, 0, 0, 0, 0),
    ("h6", "f2", 1, 2, 1, 1, 1, 1),
):
    adjusted = {"bh": bh, "by": by, "holm": holm, "bonferroni": bonferroni}
    COMPLEX_EXPECTED["adjustments"].append({"family": family, "id": identifier, "p": p, "family_size": size, **adjusted,
        **{"reject_" + method: value <= .05 if value is not None else None for method, value in adjusted.items()}})

COMPLEX_REQUEST = """将已完成的多方法纵向研究分析固化为一个可独立运行的完整Python脚本，不能以多个脚本汇总或删减方法替代。
仅用标准库，--input为只读JSON文件，--output为独立目录；目录内写report.json和wide.csv，两个文件名可参数化。所有输入验证及计算成功后才写出，任一无效表拒绝整个任务。保留六个可独立理解和测试的计算函数及统一CLI，不用代码注释或无用分支填充长度。完整实现所有统计、质量、排序、错误分支和文件写出，不能用TODO、占位或描述代替。
输入对象有observations、pairs、associations、hypotheses、long_rows五个对象数组，所有表都必需。fields可重命名id/group/feature/value/unit/before/after/x/y/family/p这11个字段角色；只接受这些角色、映射为互不相同非空字符串，字段缺失报告角色。各表标识非空；所有非null数值须有限且非布尔，拒绝字符串数字、NaN和Infinity。units默认为mg:1、ug:0.001，可替换为正有限因子映射。alpha默认0.05且0<alpha<1，mad_threshold默认3.5、iqr_multiplier默认1.5、scale默认1，后三者必须正有限非布尔。group_a=A、group_b=B可参数化且不同。
一、质量画像：observations含id/group/feature/value/unit，(id,feature)唯一，未知单位即使值为null也拒绝。非null值乘单位因子，转换溢出拒绝。按group、feature分组排序；保留全缺失组，报告rows/missing/zeros/n/sum/mean/median/q1/q3/minimum/maximum/sample_sd/standard_error/mad/iqr/mad_outliers/iqr_outliers。分位数线性插值位置(n-1)p；样本方差分母n-1，n<2时sd和SE为null，n=0时sum=0其它位置统计为null。MAD为绝对偏差中位数，修改Z=0.67448975*(x-median)/MAD，绝对值严格大于阈值才标记；MAD=0不标记。IQR栅栏Q1-kIQR、Q3+kIQR，严格超出才标记。异常值只标记不删除、不影响任何均值。
二、配对：pairs含id/group/before/after，id在表内唯一；任一时点null剔除整对，excluded为整数。所有非null原值均验证，不能因另一侧null跳过坏值。差值after-before。每个出现的组有2至12个完整配对，否则明确拒绝。按组输出group/excluded及describe的n/sum/mean/median/q1/q3/minimum/maximum/sample_sd/standard_error，再输出permutations/p_value/direction。枚举全部2^n符号组合，两侧p为abs翻转均值>=abs原均值-1e-12的个数除2^n，不加1；全零差值p=1，direction=after-minus-before。保留符号交换性与实验单位独立假设，不能替换成独立样本检验。
三、组间：在上述完整配对变化中比较group_b减group_a，两组必需且总n<=12。枚举全部C(na+nb,na)标签组合，不随机抽样；绝对均值差>=原差绝对值-1e-12计极端，p=极端数/组合数，不加1。输出group_a/group_b/n_a/n_b/mean_a/mean_b/difference_b_minus_a/permutations/p_value/standard_error/welch_t/welch_df/pooled_sd/cohen_d/hedges_correction/hedges_g。SE=sqrt(va/na+vb/nb)，Welch df=(va/na+vb/nb)^2/((va/na)^2/(na-1)+(vb/nb)^2/(nb-1))；Welch t=差/SE，仅描述统计，不伪造t分布p。pooled_sd=sqrt(((na-1)va+(nb-1)vb)/(na+nb-2))，d=差/pooled_sd，近似Hedges修正J=1-3/(4*(na+nb-2)-1)，g=J*d。零分母对应统计量null，不删除两组。
四、相关与回归：associations含唯一id/x/y，完整案例删除缺失，至少2个完整行；保留n/excluded/mean_x/mean_y/covariance/pearson/spearman/slope/intercept/residual_df/sse/r_squared/slope_se。协方差分母n-1；Pearson基于中心平方和，任一零方差则null。Spearman先给并列值平均秩再Pearson，不用无并列的简化公式。OLS含截距，slope=Sxy/Sxx、intercept=my-slope*mx、SSE=sum残差平方、df=n-2、R2=1-SSE/Syy、slope_SE=sqrt(SSE/(n-2)/Sxx)。x常量时回归各项null，y常量时R2=null，n=2时slope_SE=null，不返回NaN。
五、多重检验：hypotheses含全表唯一id/family/p，p允许null或[0,1]，每family独立校正。按(p,id)稳定排序，null不计m。BH按尾部递减最小值min(1,m*p_i/i)，BY再乘H_m；Holm按前缀递增最大值min(1,(m-i+1)*p_i)，Bonferroni=min(1,m*p)。输出按(family,id)排序的family/id/p/family_size/bh/by/holm/bonferroni/reject_bh/reject_by/reject_holm/reject_bonferroni；reject采用校正值<=alpha。null原p的校正值与reject均为null，但family_size仍为同家族有效m；全null家族m=0。
六、长宽转换：long_rows含id/feature/value，重复(id,feature)或feature为固定输出主键id拒绝；数值乘scale后有限且保留0，null/不存在单元格写空CSV，但分别计数。行id与测量列名各自升序，首列固定id，使用csv标准库处理逗号引号和UTF-8。空长表仅id表头。JSON pivot包含columns、rows、quality；quality含source_rows/output_rows/observed_missing/absent_cells，absent=行数*列数-已有组合。
最终report.json顶层仅含profiles、paired、comparison、association、adjustments、pivot、quality，完整保留上面各字段；quality汇总observation_rows/profile_count/complete_pairs/excluded_pairs/association_complete/association_excluded/hypotheses。不复制原具体研究结论。参数可从JSON指定并用明确CLI覆盖；字段映射、量纲转换、阈值和方法说明必须与实际计算一致。验收包含正常、边界、无效输入，验证所有六种方法及零值/缺失/并列秩/校正家族/置换分母/异常值不删除/输入只读。"""
