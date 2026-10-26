"""A partitioned oracle must name its observable without guessing array indices."""
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import _review_parts, review_targets
from test_skill_validation_plan import plan_payload


def test_json_targets_bind_flat_nested_and_neighboring_statistics_explicitly():
    payload = plan_payload()
    case = payload["scripts"][0]["cases"][0]
    pointers = ["/association/covariance", "/association/slope", "/association/intercept",
                "/paired/0/n", "/paired/0/describe/n"]
    case["assertions"] = [{"type": "json_value", "path": "report.json", "pointer": pointer,
        "expected": {"value": 2.5, "atol": 1e-12}} for pointer in pointers]
    case["assertions"].append({"type": "json_array_length", "path": "report.json",
                               "pointer": "/pivot/columns", "length": 3})
    targets = review_targets(SyntheticValidationPlan.model_validate(payload))[0]["cases"][0]["targets"]
    for index, pointer in enumerate(pointers):
        assert targets[index] == {"target_id": f"T{index + 1:03d}", "kind": "json_value",
            "assertion_index": index, "assertion_type": "json_value", "path": "report.json", "pointer": pointer,
            "expected": {"value": 2.5, "atol": 1e-12, "rtol": 0.0}}
    assert targets[-1] == {"target_id": "T006", "kind": "json_array_length", "assertion_index": 5,
        "assertion_type": "json_array_length", "path": "report.json", "pointer": "/pivot/columns", "expected": 3}


def test_csv_targets_bind_matching_row_count_field_and_scalar_type():
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["assertions"] = [{"type": "csv_rows", "path": "summary.csv",
        "match": {"group": "B", "flag": False}, "count": 2,
        "fields": {"count": {"value": 0}, "score": {"value": None}}}]
    targets = review_targets(SyntheticValidationPlan.model_validate(payload))[0]["cases"][0]["targets"]
    assert len(targets) == 3
    for target in targets:
        assert target["path"] == "summary.csv" and target["assertion_type"] == "csv_rows"
        assert target["match"] == {"group": "B", "flag": False} and target["row_count"] == 2
        assert "pointer" not in target
    assert targets[0]["kind"] == "csv_row_count" and targets[0]["expected"] == 2
    assert targets[1]["field"] == "count" and type(targets[1]["expected"]["value"]) is int
    assert targets[2]["field"] == "score" and targets[2]["expected"]["value"] is None


def test_sorted_partitions_keep_original_T_to_observable_binding_without_implementation():
    payload = plan_payload()
    case = payload["scripts"][0]["cases"][0]
    case["assertions"] = [{"type": "json_value", "path": "report.json", "pointer": f"/values/{index}",
        "expected": {"value": index}} for index in reversed(range(70))]
    plan = SyntheticValidationPlan.model_validate(payload)
    full = review_targets(plan)[0]["cases"][0]
    expected = {target["target_id"]: target for target in full["targets"]}
    seen = []
    for targets, scope in _review_parts(plan):
        for part_case in targets[0]["cases"]:
            if part_case["case_id"] != case["id"]:
                continue
            for target in part_case["targets"]:
                assert target == expected[target["target_id"]]
                assert target["pointer"] == "/values/" + str(target["expected"]["value"])
                assert set(target) == {"target_id", "kind", "assertion_index", "assertion_type", "path", "pointer", "expected"}
                seen.append(target["target_id"])
    assert len(seen) == len(set(seen)) == 70 and set(seen) == set(expected)
