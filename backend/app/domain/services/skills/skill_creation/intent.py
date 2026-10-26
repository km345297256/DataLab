"""Recognize explicit skill authoring without consuming ordinary analysis turns."""
import re

def _creation_intent_excluded(text: str) -> bool:
    return bool(re.search(r"(?:不要|别|无需|不必|不想|不允许|do not|don't|never)\s*(?:使用|调用|执行|创建|保存|固化|/skill|create|save|run)", text, re.IGNORECASE)
                or re.search(r"^(?:请|帮我)?\s*(?:解释|介绍|说明|什么是|怎么使用|如何使用|what is|explain|how (?:do|to))", text, re.IGNORECASE)
                or re.search(r"/skill-(?:creator|create)\s*(?:是什么|怎么用|如何使用|的(?:用途|含义|作用)|means?\b)", text, re.IGNORECASE))

def is_explicit_skill_create_command(text: str) -> bool:
    value = (text or "").strip()
    return not _creation_intent_excluded(value) and bool(re.match(
        r"^(?:(?:请|帮我)\s*)?(?:(?:使用|调用|执行|运行)\s*)?/skill-(?:creator|create)(?![a-z0-9_-])", value, re.IGNORECASE))

def is_skill_create_request(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered or _creation_intent_excluded(lowered):
        return False
    if is_explicit_skill_create_command(lowered):
        return True
    # Reusing an existing skill or describing its earlier creation is not a new
    # creation request, even if the sentence contains the words "saved skill".
    if re.match(r"^(?:使用|用|调用|运行|复用|use\b|apply\b|run\b|reuse\b)", lowered):
        return False
    return bool(re.search(r"(?:保存|固化|转成|转换成|创建)(?:为|成)?(?:一个|新的)?(?:可复用的?)?(?:技能|skill)", lowered)
                or re.search(r"^(?:请|帮我|请帮我)?(?:保存|固化)(?:本次|当前|刚才|这个)分析流程", lowered)
                or re.search(r"^(?:(?:please|can you|help me)\s+)?(?:create|save|turn|convert|make)\b.*\bskill\b", lowered)
                or re.match(r"^save this workflow(?:[.!?]|$)", lowered))

_is_skill_create_request = is_skill_create_request
