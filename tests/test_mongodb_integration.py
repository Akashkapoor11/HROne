"""Opt-in integration tests for MongoDB aggregation semantics and index plans.

Set HRONE_TEST_MONGO_URI to a disposable MongoDB 6.0+ instance before running:
    HRONE_TEST_MONGO_URI='mongodb://localhost:27017' python -m unittest discover -s tests -v
The tests create and drop a uniquely named database. They never touch MONGO_DB.
"""
from __future__ import annotations

import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException
from pymongo import MongoClient

# Import-time configuration for a lazy, unused MongoClient in database-independent tests.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app import main

UTC = timezone.utc
TEST_URI = os.getenv("HRONE_TEST_MONGO_URI")


def _employee(emp_code: str, department: str, joined_on: str) -> dict[str, Any]:
    return {
        "emp_code": emp_code,
        "name": emp_code,
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }


def _log(
    emp_code: str,
    day: str,
    status: str,
    work_hours: float | None = None,
    late_minutes: int = 0,
    overtime_minutes: int = 0,
    half_day: bool = False,
    punch_in: datetime | None = None,
    punch_out: datetime | None = None,
) -> dict[str, Any]:
    return {
        "emp_code": emp_code,
        "date": day,
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
        "history": [],
    }


def _all_values_named(value: Any, key: str):
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for nested in value.values():
            yield from _all_values_named(nested, key)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_values_named(nested, key)


@unittest.skipUnless(TEST_URI, "Set HRONE_TEST_MONGO_URI to run real MongoDB integration tests")
class MongoDBIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MongoClient(TEST_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
        cls.client.admin.command("ping")
        cls.db_name = f"hrone_assignment_test_{uuid.uuid4().hex[:12]}"
        cls.database = cls.client[cls.db_name]
        cls._original_db = main.db
        cls._original_client = main.client
        cls._original_indexes_ready = main._indexes_ready
        main.db = cls.database
        main.client = cls.client
        main._indexes_ready = False
        main.ensure_indexes()

        cls.database.employees.insert_many([
            _employee("EMP0001", "Engineering", "2026-07-01"),
            _employee("EMP0002", "Engineering", "2026-07-15"),  # no attendance logs
            _employee("EMP0003", "Sales", "2026-07-01"),
            _employee("EMP0004", "Engineering", "2026-07-01"),
        ])
        cls.database.attendance_logs.insert_many([
            _log("EMP0001", "2026-07-06", "PRESENT", 9.08, 35, 40, False,
                 datetime(2026, 7, 6, 4, 35, tzinfo=UTC), datetime(2026, 7, 6, 13, 40, tzinfo=UTC)),
            _log("EMP0001", "2026-07-07", "WFH", 4.49, 0, 0, True,
                 datetime(2026, 7, 7, 4, 0, tzinfo=UTC), datetime(2026, 7, 7, 8, 29, 24, tzinfo=UTC)),
            _log("EMP0001", "2026-07-08", "LEAVE"),
            _log("EMP0001", "2026-07-09", "ON_DUTY", 8.0, 0, 0, False,
                 datetime(2026, 7, 9, 4, 0, tzinfo=UTC), datetime(2026, 7, 9, 12, 0, tzinfo=UTC)),
            # A weekend log still contributes to late/overtime, but not monthly present_days.
            _log("EMP0001", "2026-07-11", "PRESENT", 10.0, 20, 80, False,
                 datetime(2026, 7, 11, 4, 20, tzinfo=UTC), datetime(2026, 7, 11, 14, 20, tzinfo=UTC)),
            _log("EMP0003", "2026-07-06", "PRESENT", 8.5, 55, 0, False,
                 datetime(2026, 7, 6, 4, 55, tzinfo=UTC), datetime(2026, 7, 6, 13, 25, tzinfo=UTC)),
            _log("EMP0004", "2026-07-06", "PRESENT", 8.5, 55, 0, False,
                 datetime(2026, 7, 6, 4, 55, tzinfo=UTC), datetime(2026, 7, 6, 13, 25, tzinfo=UTC)),
            _log("EMP0004", "2026-07-16", "PRESENT", 7.5, 0, 0, False,
                 datetime(2026, 7, 16, 4, 0, tzinfo=UTC), datetime(2026, 7, 16, 11, 30, tzinfo=UTC)),
            # Trend counts presence on weekends too, even though its rate is null.
            _log("EMP0003", "2026-07-18", "WFH", 4.5, 0, 0, True,
                 datetime(2026, 7, 18, 4, 0, tzinfo=UTC), datetime(2026, 7, 18, 8, 30, tzinfo=UTC)),
        ])

    @classmethod
    def tearDownClass(cls):
        cls.client.drop_database(cls.db_name)
        main.db = cls._original_db
        main.client = cls._original_client
        main._indexes_ready = cls._original_indexes_ready
        cls.client.close()

    def test_employee_monthly_handles_half_days_weekends_leaves_and_join_dates(self):
        result = main.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(result["working_days"], 23)
        self.assertEqual(result["present_days"], 2.5)
        self.assertEqual(result["leave_days"], 1)
        self.assertEqual(result["late_count"], 2)
        self.assertEqual(result["total_late_minutes"], 55)
        self.assertEqual(result["total_overtime_minutes"], 120)
        self.assertEqual(result["attendance_pct"], 10.8696)

    def test_department_summary_includes_employees_without_logs_and_averages_records(self):
        result = main.department_summary("2026-07", "Engineering")
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["headcount"], 3)  # includes EMP0002 with no logs
        self.assertEqual(item["present_days"], 4.5)  # weekend record excluded
        self.assertEqual(item["avg_work_hours"], 7.93)  # record average, not employee-average
        self.assertEqual(item["late_count"], 3)
        self.assertEqual(item["total_late_minutes"], 110)
        self.assertEqual(item["leave_count"], 1)
        self.assertEqual(item["on_duty_count"], 1)

    def test_leaderboard_returns_all_tied_rows_at_cutoff_and_filters_department_before_rank(self):
        overall = main.late_leaderboard("2026-07", limit=1)
        self.assertEqual([(row["emp_code"], row["rank"]) for row in overall["items"]], [
            ("EMP0001", 1), ("EMP0003", 1), ("EMP0004", 1),
        ])
        engineering = main.late_leaderboard("2026-07", limit=1, department="Engineering")
        self.assertEqual([(row["emp_code"], row["rank"]) for row in engineering["items"]], [
            ("EMP0001", 1), ("EMP0004", 1),
        ])

    def test_department_trend_fills_dates_counts_daily_joiners_and_windows_non_null_rates(self):
        result = main.department_trend("Engineering", date(2026, 7, 14), date(2026, 7, 18))
        items = result["items"]
        self.assertEqual(len(items), 5)
        self.assertEqual([item["date"] for item in items], [
            "2026-07-14", "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18",
        ])
        self.assertEqual([item["headcount"] for item in items], [2, 3, 3, 3, 3])
        self.assertEqual(items[2]["present_count"], 1)
        self.assertEqual(items[2]["attendance_rate"], 0.3333)
        self.assertIsNone(items[4]["attendance_rate"], "weekend rates are null")
        self.assertEqual(items[2]["moving_avg_7d"], 0.1111)
        self.assertEqual(items[4]["moving_avg_7d"], 0.0833)

    def test_trend_counts_weekend_presence_but_keeps_weekend_rate_null(self):
        result = main.department_trend("Sales", date(2026, 7, 18), date(2026, 7, 18))
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertFalse(item["is_working_day"])
        self.assertEqual(item["headcount"], 1)
        self.assertEqual(item["present_count"], 0.5)
        self.assertEqual(item["late_count"], 0)
        self.assertIsNone(item["attendance_rate"])
        self.assertIsNone(item["moving_avg_7d"])

    def test_explain_endpoints_use_indexes_without_collection_scans(self):
        calls = [
            main.explain_endpoint("attendance_list", emp_code="EMP0001", date_from=date(2026, 7, 1), date_to=date(2026, 7, 31), status="PRESENT", page=1, page_size=20),
            main.explain_endpoint("employee_monthly", emp_code="EMP0001", month="2026-07"),
            main.explain_endpoint("department_summary", month="2026-07", department="Engineering"),
            main.explain_endpoint("late_leaderboard", month="2026-07", limit=1, department="Engineering"),
            main.explain_endpoint("department_trend", department="Engineering", from_date=date(2026, 7, 14), to=date(2026, 7, 18)),
        ]
        for response in calls:
            with self.subTest(endpoint=response["endpoint"]):
                explain = response["explain"]
                stages = list(_all_values_named(explain, "stage"))
                self.assertIn("IXSCAN", stages)
                self.assertNotIn("COLLSCAN", stages)

    def test_live_mongodb_concurrent_punch_in_and_punch_out_guards(self):
        # Separate employees keep this test isolated from the analytics fixtures.
        codes = ("EMP9997", "EMP9998")
        for code in codes:
            main.create_employee(main.EmployeeIn.model_validate({
                "emp_code": code,
                "name": code,
                "email": f"{code.lower()}@example.com",
                "department": "Concurrency-Test",
                "joined_on": "2026-07-01",
            }))

        timestamp_in = 1783311000000
        timestamp_out = 1783339800000
        try:
            def invoke_punch_in():
                try:
                    return ("ok", main.punch_in(main.PunchInIn.model_validate({
                        "emp_code": "EMP9997", "punched_at": timestamp_in,
                    })))
                except HTTPException as exc:
                    return ("http", exc.status_code)

            with ThreadPoolExecutor(max_workers=2) as pool:
                punch_in_results = list(pool.map(lambda _n: invoke_punch_in(), range(2)))
            self.assertEqual(sum(kind == "ok" for kind, _ in punch_in_results), 1)
            self.assertEqual(sorted(value for kind, value in punch_in_results if kind == "http"), [409])

            main.punch_in(main.PunchInIn.model_validate({
                "emp_code": "EMP9998", "punched_at": timestamp_in,
            }))

            def invoke_punch_out():
                try:
                    return ("ok", main.punch_out(main.PunchOutIn.model_validate({
                        "emp_code": "EMP9998", "punched_at": timestamp_out,
                    })))
                except HTTPException as exc:
                    return ("http", exc.status_code)

            with ThreadPoolExecutor(max_workers=2) as pool:
                punch_out_results = list(pool.map(lambda _n: invoke_punch_out(), range(2)))
            self.assertEqual(sum(kind == "ok" for kind, _ in punch_out_results), 1)
            self.assertEqual(sorted(value for kind, value in punch_out_results if kind == "http"), [409])
        finally:
            self.database.attendance_logs.delete_many({"emp_code": {"$in": list(codes)}})
            self.database.employees.delete_many({"emp_code": {"$in": list(codes)}})


if __name__ == "__main__":
    unittest.main()
