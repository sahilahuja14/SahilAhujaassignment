"""
Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""
import json
import os
import re
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
EPOCH_MIN_MS = 100000000000
EPOCH_MAX_MS = 4102444800000
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
IDX_EMP_CODE = "uniq_emp_code"
IDX_EMP_DEPT_JOINED = "department_joined_emp"
IDX_EMP_JOINED_DEPT = "joined_department_emp"
IDX_ATT_EMP_DATE = "uniq_attendance_emp_date"
IDX_ATT_LIST_SORT = "attendance_list_sort"
IDX_ATT_EMP_DATE_SORT = "attendance_emp_date_sort"
IDX_ATT_STATUS_DATE_EMP = "attendance_status_date_emp"
IDX_ATT_EMP_STATUS_DATE = "attendance_emp_status_date"
IDX_ATT_RECENT_PUNCH = "attendance_recent_punch"
IDX_ATT_ANALYTICS_DATE = "attendance_analytics_date_emp_late"

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_date(cls, value: str) -> str:
        validate_date_string(value)
        return value

    @model_validator(mode="after")
    def shift_bounds_must_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return data

    @field_validator("punched_at", mode="before")
    @classmethod
    def punched_at_must_be_epoch_ms(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return data

    @field_validator("punched_at", mode="before")
    @classmethod
    def punched_at_must_be_epoch_ms(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in ("punch_in", "punch_out"):
                if field in data and data[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return data

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def instants_must_be_epoch_ms(cls, value: Optional[int]) -> Optional[int]:
        return validate_epoch_ms(value)


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name=IDX_EMP_CODE)
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name=IDX_EMP_DEPT_JOINED,
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name=IDX_EMP_JOINED_DEPT,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name=IDX_ATT_EMP_DATE,
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)],
        name=IDX_ATT_LIST_SORT,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)],
        name=IDX_ATT_EMP_DATE_SORT,
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name=IDX_ATT_STATUS_DATE_EMP,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("status", ASCENDING), ("date", DESCENDING)],
        name=IDX_ATT_EMP_STATUS_DATE,
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name=IDX_ATT_RECENT_PUNCH,
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", DESCENDING)],
        name=IDX_ATT_ANALYTICS_DATE,
    )


def validate_epoch_ms(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timestamp must be an integer epoch milliseconds value")
    if value < EPOCH_MIN_MS or value > EPOCH_MAX_MS:
        raise ValueError("timestamp is outside the supported epoch millisecond range")
    return value


def validate_date_string(value: str) -> Date:
    if not DATE_PATTERN.match(value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid calendar date") from exc


def parse_date(value: str) -> Date:
    try:
        return validate_date_string(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def parse_month(value: str) -> tuple[str, str]:
    if not MONTH_PATTERN.match(value):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    start = Date(year, month, 1)
    next_month = Date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    end = next_month - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def require_date_range(start: Optional[str], end: Optional[str]) -> None:
    if start:
        parse_date(start)
    if end:
        parse_date(end)
    if start and end and start > end:
        raise HTTPException(422, "date_from must be on or before date_to")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_to_second(value: datetime) -> datetime:
    return to_utc(value).replace(microsecond=0)


def epoch_ms_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_to_second(value).timestamp() * 1000)


def parse_shift(value: str) -> tuple[int, int]:
    hour, minute = map(int, value.split(":"))
    return hour, minute


def shift_minutes(value: str) -> int:
    hour, minute = parse_shift(value)
    return hour * 60 + minute


def is_overnight_shift(employee: dict[str, Any]) -> bool:
    return shift_minutes(employee["shift_end"]) <= shift_minutes(employee["shift_start"])


def attendance_date_for(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = truncate_to_second(punch_in).astimezone(IST)
    attendance_day = local.date()
    if is_overnight_shift(employee):
        local_minutes = local.hour * 60 + local.minute
        if local_minutes < shift_minutes(employee["shift_end"]):
            attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()


def shift_instant(attendance_date: str, shift_value: str, next_day: bool = False) -> datetime:
    day = parse_date(attendance_date)
    if next_day:
        day += timedelta(days=1)
    hour, minute = parse_shift(shift_value)
    local = datetime.combine(day, time(hour, minute), tzinfo=IST)
    return local.astimezone(UTC)


def shift_start_instant(attendance_date: str, employee: dict[str, Any]) -> datetime:
    return shift_instant(attendance_date, employee["shift_start"])


def shift_end_instant(attendance_date: str, employee: dict[str, Any]) -> datetime:
    return shift_instant(attendance_date, employee["shift_end"], is_overnight_shift(employee))


def round_half_up(value: Decimal | int | float | str, places: int = 2) -> float:
    quant = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, attendance_date: str, employee: dict[str, Any]) -> int:
    seconds = (truncate_to_second(punch_in) - shift_start_instant(attendance_date, employee)).total_seconds()
    return int(seconds // 60) if seconds > 10 * 60 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(int((truncate_to_second(punch_out) - truncate_to_second(punch_in)).total_seconds()))
    return round_half_up(seconds / Decimal(3600), 2)


def compute_overtime_minutes(punch_out: datetime, attendance_date: str, employee: dict[str, Any]) -> int:
    seconds = (truncate_to_second(punch_out) - shift_end_instant(attendance_date, employee)).total_seconds()
    return int(seconds // 60) if seconds >= 30 * 60 else 0


def validate_punch_out_window(punch_in: datetime, punch_out: datetime) -> None:
    seconds = (truncate_to_second(punch_out) - truncate_to_second(punch_in)).total_seconds()
    if seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if seconds > 24 * 60 * 60:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


def derived_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    attendance_date: str,
    employee: dict[str, Any],
) -> dict[str, Any]:
    if status in ("ABSENT", "LEAVE"):
        return {
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    if punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    punch_in = truncate_to_second(punch_in)
    late_minutes = compute_late_minutes(punch_in, attendance_date, employee)
    if punch_out is None:
        return {
            "punch_in": punch_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": late_minutes,
            "overtime_minutes": 0,
            "half_day": False,
        }
    punch_out = truncate_to_second(punch_out)
    validate_punch_out_window(punch_in, punch_out)
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": compute_overtime_minutes(punch_out, attendance_date, employee),
        "half_day": work_hours < 4.5,
    }


def working_days_in_month(employee: dict[str, Any], month_start: str, month_end: str) -> int:
    start = max(parse_date(employee["joined_on"]), parse_date(month_start))
    end = parse_date(month_end)
    if start > end:
        return 0
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def serialize_history_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return datetime_to_epoch_ms(value)
    return value


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": serialize_history_value(change.get("from")),
                "to": serialize_history_value(change.get("to")),
            }
        history.append(
            {
                "at": datetime_to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def attendance_filter(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    require_date_range(date_from, date_to)
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def attendance_list_hint(emp_code: Optional[str] = None, status: Optional[str] = None) -> str:
    if emp_code and status:
        return IDX_ATT_EMP_STATUS_DATE
    if emp_code:
        return IDX_ATT_EMP_DATE_SORT
    if status:
        return IDX_ATT_STATUS_DATE_EMP
    return IDX_ATT_LIST_SORT


def department_employee_hint(department: Optional[str] = None) -> str:
    return IDX_EMP_DEPT_JOINED if department else IDX_EMP_JOINED_DEPT


def date_from_string_expr(value: str = "$date") -> dict[str, Any]:
    fallback = datetime(1970, 1, 3, tzinfo=UTC)
    return {
        "$dateFromString": {
            "dateString": value,
            "format": "%Y-%m-%d",
            "timezone": "UTC",
            "onError": fallback,
            "onNull": fallback,
        }
    }


def weekday_expr(date_expr: dict[str, Any]) -> dict[str, Any]:
    return {"$in": [{"$dayOfWeek": date_expr}, [2, 3, 4, 5, 6]]}


def present_day_expr(log_prefix: str = "") -> dict[str, Any]:
    status = f"${log_prefix}status"
    half_day = {"$ifNull": [f"${log_prefix}half_day", False]}
    return {
        "$cond": [
            {"$in": [status, list(PRESENCE_STATUSES)]},
            {"$cond": [half_day, 0.5, 1]},
            0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list[dict[str, Any]]:
    date_value = date_from_string_expr()
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    weekday_expr(date_value),
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    month_start, month_end = parse_month(month)
    match: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match["department"] = department
    date_value = date_from_string_expr("$logs.date")
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    weekday_expr(date_value),
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
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


def late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list[dict[str, Any]]:
    month_start, month_end = parse_month(month)
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(department: str, start: str, end: str) -> list[dict[str, Any]]:
    start_day = datetime.combine(parse_date(start), time.min, tzinfo=UTC)
    end_day = datetime.combine(parse_date(end), time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "department": {"$literal": department}, "day": {"$literal": start_day}}},
        {
            "$densify": {
                "field": "day",
                "partitionByFields": ["department"],
                "range": {"step": 1, "unit": "day", "bounds": [start_day, end_day]},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day", "timezone": "UTC"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"dept": "$department", "day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_doc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept": "$department", "day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"$expr": {"$eq": ["$employee.department", "$$dept"]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": present_day_expr()},
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "log_doc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$headcount_doc.count"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$log_doc.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$log_doc.late_count"}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate_raw": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": "$attendance_rate_raw",
                "moving_avg_7d": "$moving_avg_7d_raw",
            }
        },
    ]


def jsonable_mongo(value: Any) -> Any:
    return json.loads(json_util.dumps(value))


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is not ready") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .hint(IDX_EMP_DEPT_JOINED if department else IDX_EMP_CODE)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    assert punched_at is not None
    attendance_date = attendance_date_for(punched_at, employee)
    punch_in_time = truncate_to_second(punched_at)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_time, attendance_date, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    assert punched_at is not None
    punched_at = truncate_to_second(punched_at)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
        hint=IDX_ATT_RECENT_PUNCH,
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_time = truncate_to_second(record["punch_in"])
    validate_punch_out_window(punch_in_time, punched_at)
    updates = derived_fields(record["status"], punch_in_time, punched_at, record["date"], employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .hint(attendance_list_hint(emp_code, status))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeRequest,
    emp_code: str = Path(...),
    date: str = Path(...),
) -> dict[str, Any]:
    parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "attendance record not found")

    final_status = body.status or record["status"]
    requested_punch_in = "punch_in" in body.model_fields_set
    requested_punch_out = "punch_out" in body.model_fields_set
    final_punch_in = epoch_ms_to_datetime(body.punch_in) if requested_punch_in else record.get("punch_in")
    final_punch_out = epoch_ms_to_datetime(body.punch_out) if requested_punch_out else record.get("punch_out")

    if final_status in ("ABSENT", "LEAVE"):
        if requested_punch_in or requested_punch_out:
            raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        if final_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(final_punch_in, employee) != date:
            raise HTTPException(422, "punch_in does not belong to the record attendance date")

    computed = derived_fields(final_status, final_punch_in, final_punch_out, date, employee)
    new_values = {"status": final_status, **computed}
    compare_fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes: dict[str, dict[str, Any]] = {}
    for field in compare_fields:
        old_value = record.get(field)
        if field in ("punch_in", "punch_out") and old_value is not None:
            old_value = truncate_to_second(old_value)
        if old_value != new_values[field]:
            changes[field] = {"from": old_value, "to": new_values[field]}
    if not changes:
        raise HTTPException(422, "regularization changes nothing")

    history = record.get("history") or []
    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_match: dict[str, Any]
    if "history" in record:
        history_match = {"history": history}
    else:
        history_match = {"history": {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], **history_match},
        {"$set": new_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "attendance record was modified; retry regularization")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    month_start, month_end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    working_days = working_days_in_month(employee, month_start, month_end)
    rows = list(
        db.attendance_logs.aggregate(
            employee_monthly_pipeline(emp_code, month_start, month_end),
            hint=IDX_ATT_EMP_DATE,
        )
    )
    metrics = rows[0] if rows else {}
    present_days = round_half_up(metrics.get("present_days", 0), 2)
    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * Decimal(100), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(metrics.get("leave_days", 0)),
        "late_count": int(metrics.get("late_count", 0)),
        "total_late_minutes": int(metrics.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    items = []
    for row in db.employees.aggregate(
        department_summary_pipeline(month, department),
        hint=department_employee_hint(department),
    ):
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(row.get("present_days", 0), 2),
                "avg_work_hours": None
                if row.get("avg_work_hours") is None
                else round_half_up(row["avg_work_hours"], 2),
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    items = list(
        db.attendance_logs.aggregate(
            late_leaderboard_pipeline(month, limit, department),
            hint=IDX_ATT_ANALYTICS_DATE,
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    start = parse_date(from_date)
    end = parse_date(to_date)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days > 91:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    items = []
    for row in db.employees.aggregate(
        department_trend_pipeline(department, from_date, to_date),
        hint=IDX_EMP_DEPT_JOINED,
    ):
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": round_half_up(row.get("present_count", 0), 2),
                "late_count": int(row.get("late_count", 0)),
                "attendance_rate": None
                if row.get("attendance_rate") is None
                else round_half_up(row["attendance_rate"], 4),
                "moving_avg_7d": None
                if row.get("moving_avg_7d") is None
                else round_half_up(row["moving_avg_7d"], 4),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "hint": attendance_list_hint(emp_code, status),
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_mongo(explain)}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        month_start, month_end = parse_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month_start, month_end)
        collection = "attendance_logs"
        hint = IDX_ATT_EMP_DATE
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = department_summary_pipeline(month, department)
        collection = "employees"
        hint = department_employee_hint(department)
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        pipeline = late_leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
        hint = IDX_ATT_ANALYTICS_DATE
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date)
        end = parse_date(to_date)
        if end < start:
            raise HTTPException(422, "to must be on or after from")
        if (end - start).days > 91:
            raise HTTPException(422, "date range cannot exceed 92 days")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        collection = "employees"
        hint = IDX_EMP_DEPT_JOINED

    explain = db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint},
        verbosity="executionStats",
    )
    return {"endpoint": endpoint, "collection": collection, "explain": jsonable_mongo(explain)}
