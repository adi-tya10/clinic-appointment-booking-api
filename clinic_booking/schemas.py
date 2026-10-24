from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from clinic_booking.models import BookingStatus, SlotStatus

MAX_SLOT_DURATION = timedelta(hours=24)
# Integer primary keys are 32-bit in PostgreSQL; larger ids could never exist and
# would otherwise surface as a database overflow error.
MAX_ID = 2_147_483_647

ResourceId = Annotated[int, Field(ge=1, le=MAX_ID)]
ClinicId = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"),
    Field(
        description="Clinic identifier: 1-64 letters, digits, '-' or '_'.", examples=["clinic-42"]
    ),
]
PatientName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotCreate(RequestModel):
    clinic_id: ClinicId
    start_at: AwareDatetime = Field(examples=["2026-11-02T09:00:00+01:00"])
    end_at: AwareDatetime = Field(examples=["2026-11-02T09:30:00+01:00"])

    @field_validator("start_at", "end_at")
    @classmethod
    def to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def check_time_range(self) -> "SlotCreate":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        if self.end_at - self.start_at > MAX_SLOT_DURATION:
            raise ValueError("a slot may not be longer than 24 hours")
        if self.start_at <= datetime.now(UTC):
            raise ValueError("start_at must be in the future")
        return self


class SlotFilters(BaseModel):
    clinic_id: ClinicId | None = None
    status: SlotStatus | None = None
    starts_from: AwareDatetime | None = Field(
        None, description="Only slots starting at or after this time."
    )
    starts_before: AwareDatetime | None = Field(
        None, description="Only slots starting before this time."
    )
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=MAX_ID)

    @model_validator(mode="after")
    def check_range(self) -> "SlotFilters":
        if self.starts_from and self.starts_before and self.starts_before <= self.starts_from:
            raise ValueError("starts_before must be after starts_from")
        return self


class BookingCreate(RequestModel):
    patient_name: PatientName = Field(examples=["Asha Rao"])
    patient_email: EmailStr = Field(max_length=254, examples=["asha.rao@example.com"])


class BookingFilters(BaseModel):
    slot_id: ResourceId | None = None
    status: BookingStatus | None = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=MAX_ID)


class SlotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    clinic_id: str
    start_at: datetime
    end_at: datetime
    status: SlotStatus
    created_at: datetime
    updated_at: datetime


class BookingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slot_id: int
    patient_name: str
    patient_email: str
    status: BookingStatus
    created_at: datetime
    cancelled_at: datetime | None


class SlotPage(BaseModel):
    items: list[SlotRead]
    limit: int
    offset: int


class BookingPage(BaseModel):
    items: list[BookingRead]
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    location: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: {"model": ErrorResponse} for code in status_codes}
