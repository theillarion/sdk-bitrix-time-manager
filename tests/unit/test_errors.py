"""Domain errors redact credentials and preserve actionable error metadata."""

import pytest

from bitrix_time_manager.errors import BitrixAPIError, UncertainWriteError, redact


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("plain text", "plain text"),
        ("https://host/rest/500/secret/profile.json", "https://host/rest/500/***/profile.json"),
        ('URL "/rest/500/secret/"', 'URL "/rest/500/***/"'),
        ("/rest/1/one/ and /rest/2/two/", "/rest/1/***/ and /rest/2/***/"),
    ],
)
def test_redaction(source: str, expected: str) -> None:
    assert redact(source) == expected


def test_api_error_redacts_code_and_description() -> None:
    error = BitrixAPIError("/rest/500/secret/", "failed /rest/500/secret/", outcome_unknown=True)
    assert "secret" not in str(error) and "secret" not in error.api_code
    assert error.outcome_unknown and error.code == "bitrix_api_error"


def test_unknown_write_preserves_operation_id() -> None:
    error = UncertainWriteError("operation-123")
    assert error.operation_id == "operation-123"
    assert "operation-123" in str(error) and error.code == "uncertain_write"
