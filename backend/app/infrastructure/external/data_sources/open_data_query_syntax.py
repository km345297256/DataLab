"""Consistent plain-topic queries for providers accepting Lucene syntax."""
import re


def all_terms_query(query: str) -> str:
    """Require each plain term while preserving explicit search expressions.

    CKAN, Zenodo and several repository APIs otherwise make topic words
    optional, so a generic word can return records missing the core subject.
    Keep quoted phrases together and leave fielded/Boolean expert queries
    unchanged instead of guessing the user's intended grouping.
    """
    query = query.strip()
    token_pattern = r'"(?:\\.|[^"\\])*"|[^\s"]+'
    if not re.fullmatch(r'\s*(?:' + token_pattern + r')(?:\s+(?:' + token_pattern + r'))*\s*', query):
        return query
    terms = re.findall(token_pattern, query)
    if any(term in {"AND", "OR", "NOT", "&&", "||"} for term in terms):
        return query
    # Fielded, grouped, range, escaped and boosted expressions are explicit.
    if any(not term.startswith('"') and re.search(r'[:()\[\]{}\\^~!]|^[+-]', term) for term in terms):
        return query
    return " AND ".join(terms)
