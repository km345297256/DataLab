"""Explicit test-only projection of a fixture review onto a host request.

Keep each original assessment and target ID. This is used by fake generators
that intentionally answer a valid request, never by malformed-review fixtures.
"""
import copy


def review_for_payload(review, payload):
    document = review.model_dump(mode="json") if hasattr(review, "model_dump") else copy.deepcopy(review)
    scripts = {item["script_path"]: item for item in document["scripts"]}
    requested = []
    for scope in payload["review_targets"]:
        script = copy.deepcopy(scripts[scope["script_path"]])
        cases = {item["case_id"]: item for item in script["cases"]}
        script["cases"] = []
        for case_scope in scope["cases"]:
            case = cases[case_scope["case_id"]]
            targets = {item["target_id"]: item for item in case["assertions"]}
            case["assertions"] = [targets[item["target_id"]] for item in case_scope["targets"]]
            script["cases"].append(case)
        requested.append(script)
    return {**document, "scripts": requested}
