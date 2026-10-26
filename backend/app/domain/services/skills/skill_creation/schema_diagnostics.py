"""Value-free schema diagnostics safe to return to an authoring model."""
from pydantic import ValidationError
from app.domain.services.skills.skill_validation_arguments import ARGUMENT_ERROR_HINTS

def _schema_diagnostics(error: ValidationError, schema) -> list[dict]:
    """Keep declared locations/types/constraints, never values or extra keys."""
    allowed = set()
    def collect(value):
        if isinstance(value, dict):
            allowed.update(value.get("properties", {}))
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
    collect(schema.model_json_schema())
    issues = []
    for item in error.errors(include_input=False, include_url=False):
        location = [part if isinstance(part, int) or part in allowed else "<extra_field>"
                    for part in item.get("loc", ())]
        issue = {"location": location, "type": item["type"]}
        if item["type"] in ARGUMENT_ERROR_HINTS:
            issue["hint"] = ARGUMENT_ERROR_HINTS[item["type"]]
        constraints = {key: value for key, value in item.get("ctx", {}).items()
                       if key in {"min_length", "max_length", "gt", "ge", "lt", "le"}
                       and isinstance(value, (int, float))}
        if constraints:
            issue["constraints"] = constraints
        issues.append(issue)
    return issues[:16]


def _schema_error_fields(error: ValidationError, schema) -> list[str]:
    return sorted({part for issue in _schema_diagnostics(error, schema)
                   for part in issue["location"] if isinstance(part, str) and part != "<extra_field>"})[:8]
