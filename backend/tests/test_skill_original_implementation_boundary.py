"""Opt-in private original reply: code punctuation is not a rendered link."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.domain.services.skills.method_contract import MethodContract, validate_coverage
from app.domain.services.skills.skill_creation.models import HelperScript, SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_implementation, validate_portable_document, validate_recipe
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.source_response import source_response_adapter


def test_original_complete_script_admits_dependencies_and_normal_apis():
    location = os.environ.get("DATALAB_PRIVATE_WORKFLOW_REPLAY")
    if not location:
        pytest.skip("Opt-in owned private reply; no provider, raw data or program execution.")
    root = Path(location)
    snapshot = json.loads((root / "source-snapshot.json").read_text())["snapshot"]
    run = root / "run-0vn_hgaw"
    request = json.loads((run / "013-implementation-request.json").read_text())["content"]
    answer = json.loads((run / "015-implementation-response.json").read_text())["content"]
    assert hashlib.sha256(answer.encode()).hexdigest() == "d45473c2d2f7e944f6f84a7840f0bff9bfea4e6083d80d8441041c37cb0e3fb2"
    assert len(answer) == 99606
    script = HelperScript.model_validate(source_response_adapter(HelperScript, "implementation", request).document(answer))
    original_source = script.content
    recipe = SkillRecipe.model_validate({**request["workflow"], "scripts": [script.model_dump()]})
    method = MethodContract.model_validate(request["method_contract"])
    privacy = PrivacyContext(tuple(PrivateValue(**entry) for entry in snapshot["privacy_entries"]))
    validate_portable_document(recipe, privacy)
    validate_implementation(recipe, method, request["implementation_sources"])
    validate_coverage(method, recipe.model_dump())
    # Preserve the original source. API names, module exports and introspection
    # do not force a rewrite. Static admission does not prove runtime input/output
    # safety; that requires a separate real isolated execution receipt.
    validate_recipe(recipe, privacy)
    assert script.content == original_source
