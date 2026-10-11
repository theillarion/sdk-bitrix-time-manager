from dataclasses import replace

import pytest
from pydantic import SecretStr, ValidationError

from bitrix_time_manager import ContractRef, OperationResult, RemoteWorkCreate, Settings, TimeEntryCreate
from bitrix_time_manager.errors import ConfigurationError


def test_dynamic_crm_reference_uses_hexadecimal_type() -> None:
    assert ContractRef(entity_type_id=1298, item_id=123).crm_value == "T512_123"
    assert ContractRef(entity_type_id=1048, item_id=17).crm_value == "T418_17"


@pytest.mark.parametrize(
    ("start", "end"), [("18:00", "09:00"), ("09:00", "09:00"), ("09:00:01", "18:00"), ("09:00+03:00", "18:00")]
)
def test_invalid_interval(start: str, end: str) -> None:
    with pytest.raises(ValidationError):
        RemoteWorkCreate.model_validate({"day": "2026-10-09", "start": start, "end": end})


@pytest.mark.parametrize(
    ("hours", "minutes", "description"),
    [(0, 0, "Task"), (24, 30, "Task"), (1, 15, "Task"), (1.5, 0, "Task"), (8, 0, "   ")],
)
def test_invalid_entry(hours: float, minutes: int, description: str) -> None:
    with pytest.raises(ValidationError):
        TimeEntryCreate.model_validate(
            {
                "day": "2026-10-05",
                "contract": {"entity_type_id": 1298, "item_id": 123},
                "description": description,
                "hours": hours,
                "minutes": minutes,
            }
        )


def test_settings_mask_token_and_reject_insecure_url(settings: Settings) -> None:
    assert "test-secret" not in repr(settings)
    with pytest.raises(ConfigurationError):
        replace(settings, webhook_url=SecretStr("http://example.test/rest/500/test-secret/"))
    with pytest.raises(ConfigurationError):
        replace(settings, timezone="not/a/timezone")


@pytest.mark.parametrize(("hours", "minutes"), [(0, 30), (1, 0), (23, 30), (24, 0)])
def test_valid_duration_and_normalized_description(hours: int, minutes: int) -> None:
    spec = TimeEntryCreate.model_validate(
        {
            "day": "2026-10-08",
            "contract": {"entity_type_id": 1048, "item_id": 17},
            "description": "  Task  ",
            "hours": hours,
            "minutes": minutes,
        }
    )
    assert spec.description == "Task" and spec.hours == hours and spec.minutes == minutes
    assert spec.model_dump(mode="json")["day"] == "2026-10-08"


@pytest.mark.parametrize(
    "updates",
    [{"unknown": "value"}, {"comment": "x" * 4001}, {"day": "invalid"}, {"start": "09:00:00.000001"}],
)
def test_interval_rejects_bad_input(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RemoteWorkCreate.model_validate({"day": "2026-10-08", "start": "09:00", "end": "18:00", **updates})


@pytest.mark.parametrize("contract", [{"entity_type_id": 1208, "item_id": 1}, {"entity_type_id": 1298, "item_id": 0}])
def test_invalid_contract(contract: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ContractRef.model_validate(contract)


def test_models_are_frozen() -> None:
    spec = RemoteWorkCreate.model_validate({"day": "2026-10-08", "start": "09:00", "end": "18:00"})
    with pytest.raises(ValidationError, match="frozen"):
        spec.comment = "changed"


def test_result_defaults_are_independent() -> None:
    first = OperationResult(status="preview", kind="remote_work", operation_id="one", entity_type_id=1250)
    second = OperationResult(status="preview", kind="remote_work", operation_id="two", entity_type_id=1250)
    first.warnings.append("warning")
    first.fields["id"] = 1
    assert second.warnings == [] and second.fields == {}
