"""Validated request and response types shared by the Python API and CLI."""

from datetime import date, time
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class APIModel(BaseModel):
    """Immutable public data model that rejects misspelled input fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DayInterval(APIModel):
    """A positive interval within one local calendar day, at minute precision."""

    day: date
    start: time
    end: time
    comment: str = Field(default="", max_length=4000)

    @field_validator("start", "end")
    @classmethod
    def local_time(cls, value: time) -> time:
        """Require local clock times; the configured timezone supplies the offset."""
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Time must be local, without seconds or a timezone, e.g. 09:00.")
        return value

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        """Reject zero, negative and overnight intervals."""
        if self.end <= self.start:
            raise ValueError("End must be later than start within a single day.")
        return self


class RemoteWorkCreate(DayInterval):
    """Create remote work; Bitrix robots manage the associated workday."""


class ContractRef(APIModel):
    """Reference to a contract, technical proposal or template smart-process item."""

    entity_type_id: Literal[1048, 1052, 1298]
    item_id: int = Field(gt=0)

    @property
    def crm_value(self) -> str:
        """Encode the dynamic CRM reference as used by the portal.

        Format: ``T{entity_type_id:X}_{item_id}`` where the prefix is the
        hexadecimal entity type id, e.g. ``T512_123`` for type 1298, item 123.
        """
        return f"T{self.entity_type_id:X}_{self.item_id}"


class TimeEntryCreate(APIModel):
    """Add work performed against a contract on an existing workday."""

    day: date
    contract: ContractRef
    description: str = Field(min_length=1, max_length=4000)
    hours: int = Field(ge=0, le=24, strict=True)
    minutes: Literal[0, 30] = 0

    @field_validator("description")
    @classmethod
    def nonblank_description(cls, value: str) -> str:
        """Normalize whitespace at the boundaries and reject a blank description."""
        value = value.strip()
        if not value:
            raise ValueError("Description must not be blank.")
        return value

    @model_validator(mode="after")
    def positive_duration(self) -> Self:
        """Require a positive duration not exceeding one calendar day."""
        if not 0 < self.hours * 60 + self.minutes <= 1440:
            raise ValueError("Duration must be greater than zero and must not exceed 24 hours.")
        return self


class ItemSummary(APIModel):
    """A compact portal item suitable for contract search and bot menus."""

    id: int
    title: str
    entity_type_id: int


class Workday(APIModel):
    """Workday with employee, date and optional start/end timestamps."""

    id: int
    title: str
    day: date
    employee_id: int
    start: str | None = None
    end: str | None = None


class OperationResult(APIModel):
    """Preview or apply outcome, including exact REST fields and reconciliation ID."""

    status: Literal["preview", "created", "existing", "recovered"]
    kind: Literal["time_entry", "remote_work"]
    operation_id: str
    entity_type_id: int
    item_id: int | None = None
    item_url: str | None = None
    workday_id: int | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
