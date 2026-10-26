"""Library choice remains open throughout authoring, execution and review."""
from pathlib import Path

import pytest

from app.domain.services.safety.reviewer import SAFETY_REVIEW_SYSTEM_PROMPT
from app.domain.services.skills.skill_creation.prompts import IMPLEMENTATION_INSTRUCTION, SYSTEM_PROMPT
from app.domain.services.skills.skill_reference_verification import REFERENCE_IMPLEMENTATION_PROMPT


@pytest.mark.parametrize("prompt", [SYSTEM_PROMPT, IMPLEMENTATION_INSTRUCTION, REFERENCE_IMPLEMENTATION_PROMPT],
                         ids=["skill-authoring", "script-implementation", "reference-implementation"])
def test_skill_authoring_preserves_open_library_choice_and_honest_environment_evidence(prompt):
    assert "所有库和 API 均可使用" in prompt
    assert "隔离环境" in prompt
    assert "无网络" in prompt
    assert "基础库" in prompt
    for restriction in ("blocked_operations", "静态检查不支持 rename/remove", "不得安装依赖",
                        "禁止联网、安装", "使用允许的标准/科学库"):
        assert restriction not in prompt


def test_safety_review_does_not_treat_unknown_packages_as_malicious():
    assert "unknown software" not in SAFETY_REVIEW_SYSTEM_PROMPT
    assert "not by library names or unfamiliar APIs" in SAFETY_REVIEW_SYSTEM_PROMPT
    assert "installation request alone is not grounds" in SAFETY_REVIEW_SYSTEM_PROMPT
    assert "malware" in SAFETY_REVIEW_SYSTEM_PROMPT
    assert "unauthorized access" in SAFETY_REVIEW_SYSTEM_PROMPT


@pytest.mark.parametrize("name", ["SKILL.md", "references/design-quality.md", "references/portable-scripts.md"])
def test_bundled_skill_creator_has_no_dependency_or_api_restriction(name):
    text = (Path(__file__).parents[1] / "skills" / "skill-creator" / name).read_text()
    assert "所有库和 API 均可使用" in text
    assert "隔离环境" in text
    for restriction in ("不自动扩大权限或安装软件", "或联网安装依赖", "动态源码执行、联网请求、依赖安装"):
        assert restriction not in text
