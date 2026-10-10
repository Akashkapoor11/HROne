"""Employee Attendance & Analytics API.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Configure MONGO_URI with either a standard mongodb:// URI (for local MongoDB,
including a MongoDB Docker container) or mongodb+srv:// (for MongoDB Atlas).
MONGO_DB selects the database in either deployment. A local, untracked .env is
loaded for convenience without overriding environment variables.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

logger = logging.getLogger(__name__)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def resolve_mongo_uri(value: Optional[str]) -> str:
    """Validate a local Docker or Atlas URI supplied through configuration.

    PyMongo supports both ``mongodb://`` (local/Docker or standard deployments)
    and ``mongodb+srv://`` (MongoDB Atlas SRV connection strings). The same app
    and database code path is intentionally used for either deployment. No URI is
    embedded in the application; environment or local ``.env`` configuration is required.
    """
    uri = (value or "").strip()
    if not uri:
        raise ValueError("MONGO_URI must be set in the environment or local .env file")
    if not (uri.startswith("mongodb://") or uri.startswith("mongodb+srv://")):
        raise ValueError(
            "MONGO_URI must start with mongodb:// (local/Docker) or "
            "mongodb+srv:// (MongoDB Atlas)"
        )
    return uri


MONGO_URI = resolve_mongo_uri(os.getenv("MONGO_URI"))
MONGO_DB_NAME = (os.getenv("MONGO_DB") or "").strip()
if not MONGO_DB_NAME:
    raise RuntimeError("MONGO_DB must be set in the environment or local .env file")

# PyMongo accepts both URI schemes. pymongo[srv] installs the DNS dependency
# needed for Atlas mongodb+srv:// URIs. Only the selected URI is used per run;
# switching between Docker and Atlas requires configuration, not code changes.
client = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
)
db = client[MONGO_DB_NAME]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

ERROR_RESPONSE_DOC = {
    "description": "Error",
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["detail"],
                "properties": {"detail": {"type": "string"}},
            }
        }
    },
}

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# Stable names are also used for explain calls. Hints keep plans predictable
# on the larger evaluation dataset and make the intended indexes explicit.
IDX_EMP_CODE = "ux_employees_emp_code"
IDX_EMP_DEPT_CODE = "ix_employees_department_emp_code"
IDX_EMP_DEPT_JOINED = "ix_employees_department_joined"
IDX_EMP_JOINED = "ix_employees_joined"
IDX_ATT_EMP_DATE = "ux_attendance_emp_date"
IDX_ATT_DATE_EMP = "ix_attendance_date_emp"
IDX_ATT_STATUS_DATE_EMP = "ix_attendance_status_date_emp"
IDX_ATT_EMP_PUNCH = "ix_attendance_emp_punch_in"
_indexes_ready = False


def ensure_indexes() -> None:
    """Create the required indexes idempotently."""
    global _indexes_ready
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name=IDX_EMP_CODE
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name=IDX_EMP_DEPT_CODE,
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name=IDX_EMP_DEPT_JOINED,
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)], name=IDX_EMP_JOINED
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name=IDX_ATT_EMP_DATE,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name=IDX_ATT_DATE_EMP,
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name=IDX_ATT_STATUS_DATE_EMP,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name=IDX_ATT_EMP_PUNCH,
    )
    _indexes_ready = True


@app.on_event("startup")
def _startup_indexes() -> None:
    # Keep the process available for readiness polling if MongoDB is briefly
    # unavailable. /health retries index creation before reporting ready.
    try:
        ensure_indexes()
    except PyMongoError:
        logger.exception("Could not create MongoDB indexes during startup")


# --------------------------------------------------------------------------- #
# Request models and input validation
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS, json_schema_extra={"format": "int64"}, description="Epoch milliseconds since 1970-01-01T00:00:00Z.")]
ShiftTime = Annotated[StrictStr, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
MonthString = Annotated[StrictStr, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(RequestModel):
    emp_code: Annotated[StrictStr, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    email: Annotated[StrictStr, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[StrictStr, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: Annotated[StrictStr, Field(json_schema_extra={"format": "date"})]

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid calendar date") from exc
        return value

    @field_validator("name", "department", "email")
    @classmethod
    def reject_blank_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(RequestModel):
    emp_code: StrictStr
    punched_at: EpochMillis = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def null_timestamp_is_not_omission(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return value


class PunchOutIn(RequestModel):
    emp_code: StrictStr
    punched_at: EpochMillis = None

    @model_validator(mode="before")
    @classmethod
    def null_timestamp_is_not_omission(cls, value):
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return value


class RegularizeIn(RequestModel):
    # Match the API contract: unknown input fields, including read-only derived
    # fields, are ignored and are never persisted or trusted.
    model_config = ConfigDict(extra="ignore")

    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: Annotated[StrictStr, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[StrictStr, Field(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_fields(cls, value):
        if isinstance(value, dict):
            for key in ("punch_in", "punch_out", "status"):
                if key in value and value[key] is None:
                    raise ValueError(f"{key} cannot be null when supplied")
        return value

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @field_validator("regularized_by")
    @classmethod
    def by_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("regularized_by must not be blank")
        return value


# Explicit response models keep the generated /openapi.json aligned with the
# supplied contract and validate the public response shape at the API boundary.
class HealthOut(BaseModel):
    status: Literal["ok"]


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: Annotated[str, Field(json_schema_extra={"format": "date"})]
    created_at: Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS, json_schema_extra={"format": "int64", "readOnly": True})]


class EmployeePageOut(BaseModel):
    items: list[EmployeeOut]
    total: int
    page: int
    page_size: int


class ChangeValueOut(BaseModel):
    # The changed field names vary by correction; every change contains a before
    # and after value, which may legitimately be null (for example, a new or
    # cleared punch time).
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    from_: Any = Field(default=None, alias="from", json_schema_extra={"nullable": True})
    to_: Any = Field(default=None, alias="to", json_schema_extra={"nullable": True})


class HistoryEntryOut(BaseModel):
    at: EpochMillis
    by: str
    reason: str
    changes: dict[str, ChangeValueOut]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: Annotated[str, Field(json_schema_extra={"format": "date"})]
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    punch_in: Optional[int] = Field(json_schema_extra={"format": "int64", "minimum": MIN_EPOCH_MS, "maximum": MAX_EPOCH_MS})
    punch_out: Optional[int] = Field(json_schema_extra={"format": "int64", "minimum": MIN_EPOCH_MS, "maximum": MAX_EPOCH_MS})
    work_hours: Optional[float] = Field(json_schema_extra={"readOnly": True})
    late_minutes: int = Field(json_schema_extra={"readOnly": True})
    overtime_minutes: int = Field(json_schema_extra={"readOnly": True})
    half_day: bool = Field(json_schema_extra={"readOnly": True})
    history: list[HistoryEntryOut]


class AttendancePageOut(BaseModel):
    items: list[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItemOut(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: list[DepartmentSummaryItemOut]


class LateLeaderboardItemOut(BaseModel):
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LateLeaderboardOut(BaseModel):
    month: str
    items: list[LateLeaderboardItemOut]


class DepartmentTrendItemOut(BaseModel):
    date: Annotated[str, Field(json_schema_extra={"format": "date"})]
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class DepartmentTrendOut(BaseModel):
    department: str
    items: list[DepartmentTrendItemOut]


class ExplainOut(BaseModel):
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    collection: str
    explain: dict[str, Any]


# --------------------------------------------------------------------------- #
# Time, rounding, and serialization helpers
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    """Treat naive BSON datetimes as UTC; return an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Validate separately, then drop milliseconds before any calculation."""
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = truncate_seconds(value)
    return int(value.timestamp()) * 1000


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def shift_minutes(shift_time: str) -> int:
    hour, minute = (int(part) for part in shift_time.split(":"))
    return hour * 60 + minute


def attendance_date_for(punch_in: datetime, employee: dict) -> str:
    """Return the IST attendance day, including the overnight early-punch rule."""
    local = truncate_seconds(punch_in).astimezone(IST)
    local_day = local.date()
    start_minute = shift_minutes(employee["shift_start"])
    end_minute = shift_minutes(employee["shift_end"])
    local_clock_minute = local.hour * 60 + local.minute
    if end_minute <= start_minute and local_clock_minute < end_minute:
        local_day -= timedelta(days=1)
    return local_day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    """R2: only >10 minutes is late; report whole minutes since shift start."""
    punch_in = truncate_seconds(punch_in)
    day = date.fromisoformat(attendance_day)
    hour, minute = (int(part) for part in shift_start.split(":"))
    start = datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)
    elapsed_seconds = int((punch_in - start).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    """R3: whole overtime minutes, but only after at least 30 minutes."""
    punch_out = truncate_seconds(punch_out)
    day = date.fromisoformat(attendance_day)
    start_minute = shift_minutes(shift_start)
    end_minute = shift_minutes(shift_end)
    hour, minute = (int(part) for part in shift_end.split(":"))
    end_day = day + timedelta(days=1) if end_minute <= start_minute else day
    shift_end_at = datetime.combine(end_day, time(hour, minute), tzinfo=IST).astimezone(UTC)
    elapsed_seconds = int((punch_out - shift_end_at).total_seconds())
    if elapsed_seconds < 1800:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def calculate_derived_fields(
    employee: dict,
    attendance_day: str,
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if punch_in is None:
        raise HTTPException(422, "a presence status requires punch_in")
    punch_in = truncate_seconds(punch_in)
    if punch_out is None:
        return {
            "work_hours": None,
            "late_minutes": compute_late_minutes(punch_in, employee["shift_start"], attendance_day),
            "overtime_minutes": 0,
            "half_day": False,
        }
    punch_out = truncate_seconds(punch_out)
    elapsed = (punch_out - punch_in).total_seconds()
    if elapsed <= 0:
        raise HTTPException(422, "punch_out must be after punch_in")
    if elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, employee["shift_start"], attendance_day),
        "overtime_minutes": compute_overtime_minutes(
            punch_out, employee["shift_start"], employee["shift_end"], attendance_day
        ),
        "half_day": work_hours < 4.50,
    }


def employee_to_api(document: dict) -> dict:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch_ms(document["created_at"]),
    }


def attendance_to_api(document: dict) -> dict:
    history = []
    for entry in document.get("history") or []:
        changes_out = {}
        for field_name, change in (entry.get("changes") or {}).items():
            converted = {}
            for side in ("from", "to"):
                value = change.get(side)
                if field_name in ("punch_in", "punch_out"):
                    value = datetime_to_epoch_ms(value) if value is not None else None
                converted[side] = value
            changes_out[field_name] = converted
        history.append(
            {
                "at": datetime_to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes_out,
            }
        )
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch_ms(document.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


def parse_month(month: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        start_date = date.fromisoformat(f"{month}-01")
        if start_date.month == 12:
            next_date = date(start_date.year + 1, 1, 1)
        else:
            next_date = date(start_date.year, start_date.month + 1, 1)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "month must be a valid calendar month") from exc
    return start_date.isoformat(), next_date.isoformat(), start_date, next_date


def validate_date_range(from_date: date, to_date: date, maximum_days: Optional[int] = None) -> None:
    if to_date < from_date:
        raise HTTPException(422, "to/date_to must be on or after from/date_from")
    if maximum_days is not None and (to_date - from_date).days + 1 > maximum_days:
        raise HTTPException(422, f"date range must not exceed {maximum_days} days")


def mongo_round_half_up(value_expression, places: int):
    """Round a non-negative aggregation value half-up with Decimal128 math.

    MongoDB's $round uses ties-to-even. Decimal128 keeps exact decimal arithmetic
    for the scale/add/floor sequence, especially for values at a half-cent edge.
    Convert response fields back to doubles only in the final projection.
    """
    factor = Decimal128(str(10 ** places))
    half = Decimal128("0.5")
    decimal_value = {"$convert": {"input": value_expression, "to": "decimal"}}
    scaled = {"$multiply": [decimal_value, factor]}
    return {"$divide": [{"$floor": {"$add": [scaled, half]}}, factor]}


def mongo_is_weekday(date_expression):
    return {"$not": [{"$in": [{"$dayOfWeek": date_expression}, [1, 7]]}]}


def attendance_list_filter(
    emp_code: Optional[str], date_from: Optional[date], date_to: Optional[date], status: Optional[str]
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    date_query = {}
    if date_from is not None:
        date_query["$gte"] = date_from.isoformat()
    if date_to is not None:
        date_query["$lte"] = date_to.isoformat()
    if date_query:
        query["date"] = date_query
    if status is not None:
        query["status"] = status
    return query


def attendance_list_hint(emp_code: Optional[str], status: Optional[str] = None) -> str:
    # Employee/date is the most selective path when an employee is supplied.
    # Otherwise, a status-first compound index preserves the required ordering
    # while avoiding a full index walk for status-only filters.
    if emp_code is not None:
        return IDX_ATT_EMP_DATE
    if status is not None:
        return IDX_ATT_STATUS_DATE_EMP
    return IDX_ATT_DATE_EMP


# --------------------------------------------------------------------------- #
# MongoDB aggregation builders. The endpoints and explain route use the same
# builders, filters, order and index hints.
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month_start: str, next_month: str) -> list[dict]:
    start_datetime = datetime.combine(date.fromisoformat(month_start), time.min, tzinfo=UTC)
    next_datetime = datetime.combine(date.fromisoformat(next_month), time.min, tzinfo=UTC)
    work_start = {
        "$max": [
            {"$dateFromString": {"dateString": "$joined_on", "timezone": "UTC"}},
            start_datetime,
        ]
    }
    day_offsets = {
        "$cond": [
            {"$lt": ["$_work_start", next_datetime]},
            {"$range": [0, {"$dateDiff": {"startDate": "$_work_start", "endDate": next_datetime, "unit": "day"}}]},
            [],
        ]
    }
    calendar_days = {
        "$map": {
            "input": day_offsets,
            "as": "offset",
            "in": {"$dateAdd": {"startDate": "$_work_start", "unit": "day", "amount": "$$offset"}},
        }
    }
    weekday_days = {
        "$filter": {
            "input": calendar_days,
            "as": "calendar_day",
            "cond": mongo_is_weekday("$$calendar_day"),
        }
    }
    present_logs = {
        "$filter": {
            "input": "$logs",
            "as": "rec",
            "cond": {
                "$and": [
                    {"$in": ["$$rec.status", list(PRESENCE_STATUSES)]},
                    mongo_is_weekday({"$dateFromString": {"dateString": "$$rec.date", "timezone": "UTC"}}),
                ]
            },
        }
    }
    late_logs = {"$filter": {"input": "$logs", "as": "rec", "cond": {"$gt": [{"$ifNull": ["$$rec.late_minutes", 0]}, 0]}}}
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1, "overtime_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$set": {"_work_start": work_start}},
        {"$set": {"working_days": {"$size": weekday_days}}},
        {
            "$set": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": present_logs,
                            "as": "rec",
                            "in": {"$cond": [{"$eq": [{"$ifNull": ["$$rec.half_day", False]}, True]}, 0.5, 1.0]},
                        }
                    }
                },
                "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "rec", "cond": {"$eq": ["$$rec.status", "LEAVE"]}}}},
                "late_count": {"$size": late_logs},
                "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "rec", "in": {"$ifNull": ["$$rec.late_minutes", 0]}}}},
                "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "rec", "in": {"$ifNull": ["$$rec.overtime_minutes", 0]}}}},
            }
        },
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        mongo_round_half_up(
                            {"$multiply": [
                                {"$divide": [{"$toDecimal": "$present_days"}, {"$toDecimal": "$working_days"}]},
                                Decimal128("100"),
                            ]},
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month_start[:7]},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {"$convert": {"input": "$attendance_pct", "to": "double", "onNull": None, "onError": None}},
            }
        },
    ]


def department_summary_pipeline(month_start: str, next_month: str, department: Optional[str] = None) -> list[dict]:
    month_end = (date.fromisoformat(next_month) - timedelta(days=1)).isoformat()
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department
    attendance_present = {"$in": ["$attendance.status", list(PRESENCE_STATUSES)]}
    attendance_date_exists = {"$ifNull": ["$attendance.date", False]}
    weekday_for_record = {
        "$cond": [
            attendance_date_exists,
            mongo_is_weekday({"$dateFromString": {"dateString": "$attendance.date", "timezone": "UTC"}}),
            False,
        ]
    }
    has_work_hours = {
        "$and": [
            attendance_present,
            {"$ne": [{"$ifNull": ["$attendance.work_hours", None]}, None]},
        ]
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1}},
                ],
                "as": "attendance",
            }
        },
        {"$unwind": {"path": "$attendance", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "_is_weekday": weekday_for_record,
                "_is_present": attendance_present,
                "_is_late": {"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]},
                "_has_work_hours": has_work_hours,
            }
        },
        {
            "$set": {
                "_present_day_value": {
                    "$cond": [
                        {"$and": ["$_is_present", "$_is_weekday"]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$attendance.half_day", False]}, True]}, 0.5, 1.0]},
                        0,
                    ]
                },
                "_work_hours_value": {
                    "$cond": ["$_has_work_hours", {"$convert": {"input": "$attendance.work_hours", "to": "decimal"}}, Decimal128("0")]
                },
                "_work_hours_count": {"$cond": ["$_has_work_hours", 1, 0]},
            }
        },
        {
            "$group": {
                "_id": {"department": "$department", "emp_code": "$emp_code"},
                "department": {"$first": "$department"},
                "present_days": {"$sum": "$_present_day_value"},
                "late_count": {"$sum": {"$cond": ["$_is_late", 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$attendance.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$attendance.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$attendance.status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {"$sum": "$_work_hours_value"},
                "work_hours_count": {"$sum": "$_work_hours_count"},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                # R8: present_days is a sum of 0.5/1.0 increments; round to 2 dp half-up.
                "present_days": {
                    "$convert": {
                        "input": mongo_round_half_up({"$toDecimal": "$present_days"}, 2),
                        "to": "double", "onNull": None, "onError": None,
                    }
                },
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$convert": {
                            "input": mongo_round_half_up({"$divide": ["$work_hours_sum", {"$toDecimal": "$work_hours_count"}]}, 2),
                            "to": "double", "onNull": None, "onError": None,
                        }},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month_start: str, next_month: str, limit: int, department: Optional[str] = None) -> list[dict]:
    employee_match = {}
    if department is not None:
        employee_match["employee.department"] = department
    stages: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lt": next_month}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if employee_match:
        stages.append({"$match": employee_match})
    stages.extend(
        [
            {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
        ]
    )
    return stages


def department_trend_pipeline(department: str, from_date: date, to_date: date) -> list[dict]:
    day_count = (to_date - from_date).days + 1
    start_datetime = datetime.combine(from_date, time.min, tzinfo=UTC)
    day_series = {
        "$map": {
            "input": {"$range": [0, day_count]},
            "as": "offset",
            "in": {
                "$dateToString": {
                    "date": {"$dateAdd": {"startDate": start_datetime, "unit": "day", "amount": "$$offset"}},
                    "format": "%Y-%m-%d",
                    "timezone": "UTC",
                }
            },
        }
    }
    # The trend contract defines this numerator from actual presence-status
    # records for the day (weekends included); employment eligibility affects
    # headcount and the rate denominator, not whether a stored record is counted.
    present_count_value = {
        "$cond": [
            {"$in": ["$attendance.status", list(PRESENCE_STATUSES)]},
            {"$cond": [{"$eq": [{"$ifNull": ["$attendance.half_day", False]}, True]}, 0.5, 1.0]},
            0,
        ]
    }
    return [
        {"$match": {"department": department}},
        {"$set": {"_days": day_series}},
        {"$unwind": "$_days"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code", "attendance_day": "$_days"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$eq": ["$date", "$$attendance_day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "attendance",
            }
        },
        {"$unwind": {"path": "$attendance", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_days",
                "headcount": {"$sum": {"$cond": [{"$lte": ["$joined_on", "$_days"]}, 1, 0]}},
                "present_count": {"$sum": present_count_value},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$attendance.late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {
            "$set": {
                "is_working_day": mongo_is_weekday({"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}}),
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        mongo_round_half_up(
                            {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]},
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {"_moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": [{"$ifNull": ["$_moving_avg_raw", None]}, None]},
                        mongo_round_half_up("$_moving_avg_raw", 4),
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$convert": {"input": "$attendance_rate", "to": "double", "onNull": None, "onError": None}},
                "moving_avg_7d": {"$convert": {"input": "$moving_avg_7d", "to": "double", "onNull": None, "onError": None}},
            }
        },
        {"$sort": {"date": 1}},
    ]


def aggregate_with_hint(collection_name: str, pipeline: list[dict], hint: str):
    return list(db[collection_name].aggregate(pipeline, hint=hint, allowDiskUse=True))


def explain_aggregate(collection_name: str, pipeline: list[dict], hint: str) -> dict:
    command = {
        "aggregate": collection_name,
        "pipeline": pipeline,
        "cursor": {},
        "hint": hint,
        "allowDiskUse": True,
    }
    result = db.command("explain", command, verbosity="executionStats")
    # Return JSON-safe raw explain data (timestamps / BSON Timestamp values use
    # Extended JSON representations rather than leaking Python BSON objects).
    return json.loads(json_util.dumps(result))


def explain_find(collection_name: str, query: dict, sort: dict, skip: int, limit: int, hint: str) -> dict:
    command = {
        "find": collection_name,
        "filter": query,
        "sort": sort,
        "skip": skip,
        "limit": limit,
        "hint": hint,
    }
    result = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(result))


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthOut, responses={503: ERROR_RESPONSE_DOC}, operation_id="health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable or indexes are not ready") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201, response_model=EmployeeOut, responses={409: ERROR_RESPONSE_DOC}, operation_id="createEmployee")
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = now_utc_seconds()
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_to_api(document)


@app.get("/employees", response_model=EmployeePageOut, operation_id="listEmployees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    hint = IDX_EMP_DEPT_CODE if department is not None else IDX_EMP_CODE
    total = db.employees.count_documents(query, hint=hint)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {"items": [employee_to_api(item) for item in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut, responses={404: ERROR_RESPONSE_DOC, 409: ERROR_RESPONSE_DOC}, operation_id="punchIn")
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, hint=IDX_EMP_CODE)
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = now_utc_seconds() if body.punched_at is None else epoch_ms_to_datetime(body.punched_at)
    punched_at = truncate_seconds(punched_at)
    attendance_day = attendance_date_for(punched_at, employee)
    derived = calculate_derived_fields(employee, attendance_day, body.status, punched_at, None)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this attendance date") from exc
    return attendance_to_api(document)


@app.get("/attendance", response_model=AttendancePageOut, operation_id="listAttendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from is not None and date_to is not None:
        validate_date_range(date_from, date_to)
    query = attendance_list_filter(emp_code, date_from, date_to, status)
    hint = attendance_list_hint(emp_code, status)
    total = db.attendance_logs.count_documents(query, hint=hint)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .hint(hint)
    )
    return {"items": [attendance_to_api(item) for item in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", response_model=AttendanceRecordOut, responses={404: ERROR_RESPONSE_DOC, 409: ERROR_RESPONSE_DOC}, operation_id="punchOut")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code}, hint=IDX_EMP_CODE)
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = now_utc_seconds() if body.punched_at is None else epoch_ms_to_datetime(body.punched_at)
    punched_at = truncate_seconds(punched_at)

    query = {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": punched_at}}
    record = db.attendance_logs.find_one(query, sort=[("punch_in", DESCENDING)], hint=IDX_ATT_EMP_PUNCH)
    if record is None:
        future_record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$gt": punched_at}},
            sort=[("punch_in", ASCENDING)],
            hint=IDX_ATT_EMP_PUNCH,
        )
        if future_record is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found before punched_at")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")

    punch_in_value = truncate_seconds(record["punch_in"])
    elapsed = (punched_at - punch_in_value).total_seconds()
    if elapsed <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    derived = calculate_derived_fields(employee, record["date"], record["status"], punch_in_value, punched_at)
    update_filter = {
        "_id": record["_id"],
        "punch_in": record["punch_in"],
        "status": record["status"],
        "date": record["date"],
    }
    if "punch_out" in record:
        update_filter["punch_out"] = record["punch_out"]
    else:
        update_filter["punch_out"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": {"punch_in": punch_in_value, "punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # The compare-and-set filter ensures simultaneous requests cannot both
        # report success for the same open attendance record.
        raise HTTPException(409, "attendance record was updated by another request")
    return attendance_to_api(updated)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut, responses={404: ERROR_RESPONSE_DOC, 409: ERROR_RESPONSE_DOC}, operation_id="regularizeAttendance")
def regularize_attendance(
    emp_code: str,
    attendance_date: Annotated[date, Path(alias="date", description="IST calendar date YYYY-MM-DD")],
    body: RegularizeIn,
):
    employee = db.employees.find_one({"emp_code": emp_code}, hint=IDX_EMP_CODE)
    if employee is None:
        raise HTTPException(404, "employee not found")
    record_day = attendance_date.isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_day}, hint=IDX_ATT_EMP_DATE)
    if record is None:
        raise HTTPException(404, "attendance record not found for that date")

    supplied = body.model_fields_set
    old_status = record.get("status")
    final_status = body.status if "status" in supplied and body.status is not None else old_status
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied else old_punch_in
        final_punch_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied else old_punch_out
        if final_punch_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        final_punch_in = truncate_seconds(final_punch_in)
        if attendance_date_for(final_punch_in, employee) != record_day:
            raise HTTPException(422, "punch_in must belong to the record's attendance date")
        if final_punch_out is not None:
            final_punch_out = truncate_seconds(final_punch_out)
            elapsed = (final_punch_out - final_punch_in).total_seconds()
            if elapsed <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = calculate_derived_fields(employee, record_day, final_status, final_punch_in, final_punch_out)
    final_values = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived,
    }
    compare_fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for field_name in compare_fields:
        before = record.get(field_name, defaults.get(field_name))
        after = final_values.get(field_name, defaults.get(field_name))
        if field_name in ("punch_in", "punch_out"):
            before_norm = truncate_seconds(before) if before is not None else None
            after_norm = truncate_seconds(after) if after is not None else None
            is_changed = before_norm != after_norm
            change_before = before_norm
            change_after = after_norm
        else:
            is_changed = before != after
            change_before = before
            change_after = after
        if is_changed:
            changes[field_name] = {"from": change_before, "to": change_after}
    if not changes:
        raise HTTPException(422, "regularization must change at least one field")

    history_entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Compare-and-set all fields used for the calculation plus the whole prior
    # history. One concurrent editor wins; the other receives 409, so neither
    # values nor history entries can silently overwrite one another.
    conditions = [{"_id": record["_id"]}]
    for field_name in compare_fields:
        if field_name in record:
            conditions.append({field_name: record[field_name]})
        else:
            conditions.append({field_name: {"$exists": False}})
    if "history" in record:
        conditions.append({"history": record["history"]})
    else:
        conditions.append({"history": {"$exists": False}})
    updated = db.attendance_logs.find_one_and_update(
        {"$and": conditions},
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently; retry the correction")
    return attendance_to_api(updated)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut, responses={404: ERROR_RESPONSE_DOC}, operation_id="employeeMonthly")
def employee_monthly(emp_code: str, month: MonthString):
    month_start, next_month, _, _ = parse_month(month)
    results = aggregate_with_hint(
        "employees", employee_monthly_pipeline(emp_code, month_start, next_month), IDX_EMP_CODE
    )
    if not results:
        raise HTTPException(404, "employee not found")
    return results[0]


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut, operation_id="departmentSummary")
def department_summary(month: MonthString, department: Optional[str] = None):
    month_start, next_month, _, _ = parse_month(month)
    pipeline = department_summary_pipeline(month_start, next_month, department)
    hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED
    rows = aggregate_with_hint("employees", pipeline, hint)
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late", response_model=LateLeaderboardOut, operation_id="lateLeaderboard")
def late_leaderboard(
    month: MonthString,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    month_start, next_month, _, _ = parse_month(month)
    rows = aggregate_with_hint(
        "attendance_logs",
        late_leaderboard_pipeline(month_start, next_month, limit, department),
        IDX_ATT_DATE_EMP,
    )
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend", response_model=DepartmentTrendOut, responses={404: ERROR_RESPONSE_DOC}, operation_id="departmentTrend")
def department_trend(
    department: str,
    from_date: Annotated[date, Query(alias="from")],
    to: date,
):
    validate_date_range(from_date, to, maximum_days=92)
    if db.employees.find_one({"department": department}, {"_id": 1}, hint=IDX_EMP_DEPT_JOINED) is None:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_date, to)
    rows = aggregate_with_hint("employees", pipeline, IDX_EMP_DEPT_JOINED)
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}", response_model=ExplainOut, operation_id="explainEndpoint")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[MonthString] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to: Optional[date] = None,
):
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None:
            validate_date_range(date_from, date_to)
        query = attendance_list_filter(emp_code, date_from, date_to, status)
        hint = attendance_list_hint(emp_code, status)
        explain = explain_find(
            "attendance_logs", query, {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size, hint
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required to explain employee_monthly")
        month_start, next_month, _, _ = parse_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start, next_month)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline, IDX_EMP_CODE)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required to explain department_summary")
        month_start, next_month, _, _ = parse_month(month)
        pipeline = department_summary_pipeline(month_start, next_month, department)
        hint = IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline, hint)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required to explain late_leaderboard")
        month_start, next_month, _, _ = parse_month(month)
        pipeline = late_leaderboard_pipeline(month_start, next_month, limit, department)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline, IDX_ATT_DATE_EMP)}

    # department_trend
    if not department or from_date is None or to is None:
        raise HTTPException(422, "department, from and to are required to explain department_trend")
    validate_date_range(from_date, to, maximum_days=92)
    pipeline = department_trend_pipeline(department, from_date, to)
    return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline, IDX_EMP_DEPT_JOINED)}
