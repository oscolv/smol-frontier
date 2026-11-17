"""Exceptions shared by the publish modules (and the exit codes they map to)."""


class PublishRefused(Exception):
    """Precondition refusal (exit 2): the run cannot back a contract."""


class ContractInvalid(Exception):
    """smolf's own output failed its schema (exit 1): a bug, never published."""


def _require_dict(value: object, what: str) -> dict:
    """``value`` if it is a dict; else ContractInvalid (a malformed run input)."""
    if not isinstance(value, dict):
        msg = f"{what} is not a JSON object (got {type(value).__name__})"
        raise ContractInvalid(msg)
    return value
