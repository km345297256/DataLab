"""Interfaces authored before code, shared by candidate and blind verification.

Declarations contain public data structure and missing-value semantics, never
source, examples of computed answers, or observed results. Syntax inspection
may reject a proven contradiction; it cannot define or silently change these
contracts. Unknown implementation structure remains an execution obligation.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation_paths import validation_relative_path


Flag = Annotated[str, Field(pattern=r"^--[a-z][a-z0-9-]*$")]
Scalar = StrictStr | StrictInt | StrictFloat | StrictBool | None


class InterfaceModel(BaseModel):
    # Defaults, column names and JSON keys may contain significant whitespace.
    # Freeze their bytes rather than normalizing scientific input semantics.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class InterfaceShape(InterfaceModel):
    """Names and containers only. Scalar values/formulas have no schema slot."""
    type: Literal["object", "array", "string", "integer", "number", "boolean", "null", "unknown"]
    properties: dict[str, "InterfaceShape"] = Field(default_factory=dict,
        description="Required named properties. Dynamic or optional keys are described through additional_keys and structure.")
    items: "InterfaceShape | None" = None
    additional_keys: bool = False
    column_name_prefix: list[str] = Field(default_factory=list,
        description="Fixed leading column-name strings in an array, never scientific values or expected calculation results.")

    @model_validator(mode="after")
    def consistent_container(self):
        if self.type != "object" and (self.properties or self.additional_keys):
            raise ValueError("Only object shapes declare properties or additional keys")
        if (self.type == "array") != (self.items is not None):
            raise ValueError("Array shapes require items; other shapes cannot declare items")
        if self.column_name_prefix and (self.type != "array" or any(not value for value in self.column_name_prefix)):
            raise ValueError("Only arrays may declare nonempty leading column names")
        if any(not key or any(ord(char) < 32 for char in key) for key in self.properties):
            raise ValueError("Public field names must be nonempty and contain no control characters")
        return self

    def public_shape(self):
        # Existing structural assertions deliberately erase scalar values and
        # types. Missing/null behavior is frozen separately in missing_semantics;
        # these shapes describe containers, not numerical validation verdicts.
        if self.type == "object":
            return {"type": "object", "properties": {key: value.public_shape() for key, value in self.properties.items()},
                    "additional_keys": self.additional_keys}
        if self.type == "array":
            return {"type": "array", "items": self.items.public_shape(),
                    **({"column_name_prefix": list(self.column_name_prefix)} if self.column_name_prefix else {})}
        return {"type": "unknown"}


class DeclaredCLIOption(InterfaceModel):
    flags: list[Flag] = Field(min_length=1)
    required: bool
    value_type: Literal["str", "int", "float", "bool"]
    action: str = Field(default="store", min_length=1,
                        description="Declared CLI behavior, for example store, append or store_true; no parsing library is prescribed.")
    nargs: Annotated[StrictInt, Field(ge=0)] | Literal["?", "*", "+"] | None = None
    default: JsonValue = None
    choices: list[Scalar] = Field(default_factory=list)
    meaning: str = Field(min_length=8)

    @model_validator(mode="after")
    def consistent_option(self):
        if len(self.flags) != len(set(self.flags)):
            raise ValueError("Option aliases must be unique")
        if self.action in {"store_true", "store_false"} and self.value_type != "bool":
            raise ValueError("Boolean switches must declare boolean type")
        # Container defaults (append, nargs, JSON-config/custom parsers) are
        # serialized literally. CLI choices constrain supplied scalar tokens;
        # they do not justify rewriting a container default or its semantics.
        return self


class DeclaredData(InterfaceModel):
    format: str = Field(min_length=2, max_length=60)
    structure: str = Field(min_length=8)
    missing_semantics: list[str] = Field(min_length=1)
    shape: InterfaceShape | None = None
    columns: list[str] = Field(default_factory=list)
    dynamic_columns: bool = False

    @model_validator(mode="after")
    def concrete_data_structure(self):
        if any(len(item.strip()) < 8 for item in self.missing_semantics):
            raise ValueError("Missing, null, empty and zero behavior must be explicit")
        if len(self.columns) != len(set(self.columns)) or any(not column for column in self.columns):
            raise ValueError("Declared columns must be nonempty and unique")
        if self.format.lower() == "json" and (self.shape is None or self.shape.type == "unknown"):
            raise ValueError("JSON data needs an explicit root shape")
        if self.format.lower() == "csv" and not self.columns and not self.dynamic_columns:
            raise ValueError("CSV data needs named columns or an explicit dynamic-column contract")
        return self


class DeclaredInput(DeclaredData):
    argument: Flag


class DeclaredOutput(DeclaredData):
    path: str = Field(description="@output for a single output file; @output/name.ext for a directory output.")
    filename_argument: Flag | None = None

    @model_validator(mode="after")
    def relative_output(self):
        if self.path != "@output":
            if not self.path.startswith("@output/"):
                raise ValueError("Output paths must be relative to the declared output binding")
            validation_relative_path(self.path[len("@output/"):])
        if "\\" in self.path or any(ord(char) < 32 for char in self.path):
            raise ValueError("Output paths contain invalid characters")
        return self


class ScriptInterfaceContract(InterfaceModel):
    version: Literal[1] = 1
    input_filename_role: Literal["binding_only", "analysis_value", "unknown"] = Field(
        default="unknown", exclude_if=lambda value: value == "unknown",
        description=("Source-grounded role of every input filename, including files bound by an input manifest. "
                     "binding_only means all names only locate authorized inputs and carry no analysis meaning; "
                     "analysis_value means any basename is used for labels, dates, grouping, filtering or other analysis; "
                     "unknown means the sources do not establish the role. Independently review this declaration. "
                     "Only an accepted binding_only interface may rename initial synthetic input bindings."))
    cli_options: list[DeclaredCLIOption] = Field(min_length=2)
    inputs: list[DeclaredInput] = Field(min_length=1)
    output_mode: Literal["file", "directory"]
    outputs: list[DeclaredOutput] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_bindings(self):
        flags = [flag for option in self.cli_options for flag in option.flags]
        if len(flags) != len(set(flags)) or not {"--input", "--output"} <= set(flags):
            raise ValueError("Declare unique options including --input and --output")
        paths = [output.path for output in self.outputs]
        if len(paths) != len(set(paths)):
            raise ValueError("Each output binding must be unique")
        if self.output_mode == "file" and paths != ["@output"]:
            raise ValueError("File output declares exactly one @output file")
        if self.output_mode == "directory" and "@output" in paths:
            raise ValueError("Directory outputs must name their files under @output/")
        if "--input" not in {item.argument for item in self.inputs}:
            raise ValueError("The selected input requires a concrete --input declaration")
        for value in [item.argument for item in self.inputs] + [item.filename_argument for item in self.outputs if item.filename_argument]:
            if value not in flags:
                raise ValueError("Data bindings must refer to declared CLI options")
        for option in self.cli_options:
            if set(option.flags) & {"--input", "--output"} and (option.action != "store" or option.value_type != "str"):
                raise ValueError("Input and output bindings must accept path strings")
        return self


def _contract(value):
    return value if isinstance(value, ScriptInterfaceContract) else ScriptInterfaceContract.model_validate(value)


def validate_declared_invocation(script, declaration):
    """Reject impossible declaration/command pairs while workflow is editable."""
    if declaration is None:
        return
    from .script_policy import invocation_arguments
    contract = _contract(declaration)
    flags = set(invocation_arguments(script))
    allowed = {flag for option in contract.cli_options for flag in option.flags}
    if not flags <= allowed or any(option.required and not flags.intersection(option.flags) for option in contract.cli_options):
        raise SkillPackageValidationError("invalid_declared_script_interface", details={
            "interface_issues": [{"location": ["invocation"], "reason": "declared_cli_binding_conflict"}]})


def validate_declared_script_interface(script, declaration):
    """Check proven structural conflicts only, without interpreting any code."""
    if declaration is None:
        return
    from app.domain.services.skills.script_public_interface import public_script_interface
    contract = _contract(declaration)
    validate_declared_invocation(script, contract)
    observed = public_script_interface(script.content)
    issues = []
    declared_flags = {flag: option for option in contract.cli_options for flag in option.flags}
    for index, option in enumerate(observed["cli_options"]):
        for flag in option["flags"]:
            expected = declared_flags.get(flag)
            if expected is None:
                issues.append({"location": ["cli_options"], "reason": "undeclared_cli_option"})
                continue
            for key in ("required", "value_type", "action", "nargs", "choices"):
                if key in option and option[key] != getattr(expected, key):
                    issues.append({"location": ["cli_options", contract.cli_options.index(expected), key],
                                   "reason": "cli_option_conflict"})
    if observed["output_mode"] not in {"unknown", contract.output_mode}:
        issues.append({"location": ["output_mode"], "reason": "file_directory_conflict"})

    def compare(actual, expected, location):
        if actual.get("type", "unknown") == "unknown" or expected.type == "unknown":
            return
        if actual["type"] == "tuple" and expected.type == "array":
            # JSON encoders serialize tuples as arrays; tuple syntax is not an
            # interface contradiction, and scalar elements remain unknown.
            for item in actual.get("items", []):
                compare(item, expected.items, [*location, "items"])
            return
        if actual["type"] != expected.type:
            issues.append({"location": location, "reason": "output_container_conflict"})
        elif expected.type == "object":
            properties = actual.get("properties", {})
            if not actual.get("additional_keys", False) and not expected.properties.keys() <= properties.keys():
                issues.append({"location": location, "reason": "required_output_field_missing"})
            if not expected.additional_keys and not properties.keys() <= expected.properties.keys():
                issues.append({"location": location, "reason": "undeclared_output_field"})
            for key in expected.properties.keys() & properties.keys():
                compare(properties[key], expected.properties[key], [*location, "properties", key])
        elif expected.type == "array":
            known_prefix = actual.get("column_name_prefix", [])
            if any(left != right for left, right in zip(known_prefix, expected.column_name_prefix)):
                issues.append({"location": location, "reason": "output_column_prefix_conflict"})
            compare(actual.get("items", {"type": "unknown"}), expected.items, [*location, "items"])

    for output in observed["json_outputs"]:
        matches = [(index, item) for index, item in enumerate(contract.outputs)
                   if item.path == output["path"] and item.filename_argument == output.get("filename_argument")]
        if not matches:
            issues.append({"location": ["outputs"], "reason": "output_binding_conflict"})
        else:
            index, expected = matches[0]
            if expected.shape is not None:
                compare(output["shape"], expected.shape, ["outputs", index, "shape"])
    if issues:
        raise SkillPackageValidationError("declared_script_interface_mismatch", details={"interface_issues": issues[:20]})


def declared_validation_interface(request, *, validation_cases=()):
    """Compile the pre-code declaration; candidate source is not an input."""
    from app.domain.services.skills.skill_validation import ScriptValidationInterface
    contract = _contract(request.interface_contract)
    return ScriptValidationInterface(
        script_path=request.path, invocation=request.invocation,
        arguments=sorted(flag for option in contract.cli_options for flag in option.flags),
        cli_options=[option.model_dump(mode="json") for option in contract.cli_options],
        output_mode=contract.output_mode,
        json_outputs=[{"path": item.path, "shape": item.shape.public_shape(),
                       **({"filename_argument": item.filename_argument} if item.filename_argument else {})}
                      for item in contract.outputs if item.format.lower() == "json" and item.shape is not None],
        input_contract=[json.dumps(item.model_dump(mode="json"), ensure_ascii=False) for item in contract.inputs],
        output_contract=[json.dumps(item.model_dump(mode="json"), ensure_ascii=False) for item in contract.outputs],
        validation_cases=list(validation_cases), interface_contract=contract,
    )


def validate_declared_validation_case(interface, case):
    """Catch cheap binding/name errors without changing an independent oracle.

    Invalid inputs deliberately need not satisfy normal CLI/data constraints.
    Computed values, tolerances and missingness remain independent scientific
    review and real-execution obligations, never inferred from the candidate.
    """
    if interface.interface_contract is None or case.expected_exit_code != 0:
        return
    contract = interface.interface_contract
    issues = []
    for index, option in enumerate(contract.cli_options):
        if option.required and not any(flag in case.argv for flag in option.flags):
            issues.append({"location": ["cli_options", index], "reason": "required_cli_option_missing"})
    output = case.argv[case.argv.index("--output") + 1]
    bindings = {}
    for item in contract.outputs:
        declared = item.path
        if item.filename_argument is not None and item.filename_argument in case.argv:
            position = case.argv.index(item.filename_argument)
            filename = case.argv[position + 1] if position + 1 < len(case.argv) else ""
            try:
                validation_relative_path(filename)
                valid_filename = "/" not in filename
            except ValueError:
                valid_filename = False
            if not valid_filename:
                issues.append({"location": ["outputs"], "reason": "filename_argument_invalid"})
                continue
            declared = declared.rsplit("/", 1)[0] + "/" + filename
        bindings[(output + declared[len("@output"):]).removeprefix("{output}/")] = item
    for index, assertion in enumerate(case.assertions):
        path = getattr(assertion, "path", None)
        if path is None:
            continue
        declared = bindings.get(path)
        if declared is None:
            issues.append({"location": ["assertions", index, "path"], "reason": "undeclared_output_binding"})
        elif assertion.type == "csv_header" and not declared.dynamic_columns and assertion.columns != declared.columns:
            issues.append({"location": ["assertions", index, "columns"], "reason": "csv_header_conflict"})
        elif assertion.type == "csv_rows" and not declared.dynamic_columns:
            if not (set(assertion.match) | set(assertion.fields)) <= set(declared.columns):
                issues.append({"location": ["assertions", index], "reason": "undeclared_csv_field"})
    if issues:
        raise SkillPackageValidationError("declared_validation_interface_mismatch", details={"interface_issues": issues[:20]})
