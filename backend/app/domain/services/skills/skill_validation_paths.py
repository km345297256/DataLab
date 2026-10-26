"""Shared public filename grammar for declarations and isolated test bindings."""
import re


VALIDATION_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


def validation_relative_path(value: str) -> str:
    if not value or any(not VALIDATION_FILENAME.fullmatch(part) or part in {".", ".."} for part in value.split("/")):
        raise ValueError("Validation paths must be safe relative filenames")
    return value
