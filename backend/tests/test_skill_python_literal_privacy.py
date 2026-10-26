"""Python source escaping is not a filesystem path; decoded values still are."""
import ast
from unittest.mock import patch

import pytest

from app.domain.services.skills.skill_creation import privacy
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue


PORTABLE_HELP = "public command " + chr(92) + "\n"


def document(code, shape="scripts"):
    return {"content": code} if shape == "asset" else {"scripts": [{"content": code}]}


@pytest.mark.parametrize("shape", ["asset", "scripts"])
@pytest.mark.parametrize("prefix", ["", "u"])
def test_valid_ordinary_string_source_escaping_is_not_a_unc_path(shape, prefix):
    code = "help_text = " + prefix + repr(PORTABLE_HELP) + "\n"
    assert ast.literal_eval(ast.parse(code).body[0].value) == PORTABLE_HELP
    original = document(code, shape)
    assert PrivacyContext().scan_document(original) == []
    assert PrivacyContext().redact_document(original) == original


@pytest.mark.parametrize("text", [
    "/Users/private/input.csv", "/private/tmp/input.csv", "/data/research/input.csv",
    r"C:\Users\private\input.csv", r"\\server\private\input.csv",
])
@pytest.mark.parametrize("shape", ["asset", "scripts"])
def test_real_paths_in_decoded_ordinary_strings_still_fail(text, shape):
    code = "location = " + repr(text)
    assert any(issue.category == "host_path" for issue in PrivacyContext().scan_document(document(code, shape)))


@pytest.mark.parametrize("code", [
    r"location = '\x2fUsers\x2fprivate\x2finput.csv'",
    r"location = '\u002fUsers/private/input.csv'",
    r"location = '\x5c\x5cserver\x5cprivate\x5cinput.csv'",
    "location = '/Us' 'ers/private/input.csv'",
    "location = ('/Us'\n # ordinary comment\n 'ers/private/input.csv')",
])
def test_escaped_and_implicitly_concatenated_paths_still_fail(code):
    context = PrivacyContext()
    value = document(code)
    assert any(issue.category == "host_path" for issue in context.scan_document(value))
    assert not context.scan_document(context.redact_document(value))


@pytest.mark.parametrize("code", [
    "help_text = f" + repr(PORTABLE_HELP),
    "help_text = b" + repr(PORTABLE_HELP),
    "help_text = " + repr(PORTABLE_HELP) + "\nif broken syntax",
    "# " + repr(PORTABLE_HELP) + "\nhelp_text = 'safe'",
    "help_text = ('first'\n # " + repr(PORTABLE_HELP) + "\n 'second')",
    "help_text = ('first'\n # /Users/private/input.csv\n 'second')",
])
def test_comments_fstrings_bytes_and_invalid_python_get_no_raw_path_exemption(code):
    assert any(issue.category == "host_path" for issue in PrivacyContext().scan_document(document(code)))


def test_plain_str_inside_interpolated_expression_is_not_exempt():
    # Python versions differ in how their tokenizer exposes nested strings;
    # every string within the JoinedStr source range stays outside the rule.
    code = 'value = f"{\'plain\'}"\n'
    parsed = privacy._python_code(code)
    assert parsed is not None
    assert privacy._ordinary_string_spans(code, parsed) == []


def test_prose_fields_do_not_receive_python_token_exemption():
    assert PrivacyContext().scan_document({"description": "help_text = " + repr(PORTABLE_HELP)})


@pytest.mark.parametrize("category", ["identity", "host_path", "source_filename", "credential", "url"])
def test_exact_raw_context_values_are_never_exempt(category):
    raw = repr(PORTABLE_HELP)[1:-1]
    context = PrivacyContext((PrivateValue(raw, category),))
    issues = context.scan_document(document("help_text = " + repr(PORTABLE_HELP)))
    assert any(issue.category == category for issue in issues)


@pytest.mark.parametrize("text,category", [
    ("https://example.test/private.csv", "url"),
    ("sk-testcredential12345", "credential"),
    ("12345678-1234-1234-1234-123456789012", "identity"),
])
def test_generic_secret_url_and_identity_patterns_remain_scanned(text, category):
    for code in ("value = " + repr(text), "value = '" + "".join(f"\\x{ord(ch):02x}" for ch in text) + "'"):
        assert any(issue.category == category for issue in PrivacyContext().scan_document(document(code)))


def test_implicit_literal_concatenation_cannot_hide_known_private_filename():
    context = PrivacyContext((PrivateValue("private-data.csv", "source_filename"),))
    assert context.scan_document(document("value = 'private-' 'data.csv'"))[0].category == "source_filename"


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_unicode_text_separators_are_not_python_source_rows(separator, newline):
    code = ("说明 = '科学" + separator + "文本'; token = args.token" + newline
            + "help_text = " + repr(PORTABLE_HELP) + newline
            + r"路径 = '\x2fUsers/private/input.csv'" + newline)
    context = PrivacyContext()
    value = document(code)
    issues = context.scan_document(value)
    assert issues and all(issue.category == "host_path" for issue in issues)
    if newline != "\r":
        assert all(issue.line == 3 for issue in issues)
    safe = context.redact_document(value)["scripts"][0]["content"]
    assert "科学" + separator + "文本" in safe
    assert "token = args.token" in safe
    assert "help_text = " + repr(PORTABLE_HELP) in safe
    assert not context.scan_document(document(safe))


def test_field_source_is_parsed_once_for_path_assignment_and_decoded_checks():
    original_parse = ast.parse
    calls = []
    def count(source, *args, **kwargs):
        calls.append(source)
        return original_parse(source, *args, **kwargs)
    code = "token = args.token\nhelp_text = " + repr(PORTABLE_HELP)
    with patch.object(privacy.ast, "parse", side_effect=count):
        assert PrivacyContext().scan_document(document(code)) == []
    # literal_eval parses individual tokens independently, not the full module.
    assert calls.count(code) == 1
