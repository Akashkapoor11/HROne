"""Database-independent tests for HROne's attendance rules and pipeline builders.

Run with: python -m unittest discover -s tests -v
A real MongoDB 6.0+ instance is still required for endpoint integration and explain plans.
"""
import os
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from pydantic import ValidationError

# Import-time configuration for a lazy, unused MongoClient in database-independent tests.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app.main import (
    EmployeeIn,
    PunchInIn,
    RegularizeIn,
    attendance_date_for,
    attendance_list_filter,
    attendance_list_hint,
    calculate_derived_fields,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    department_summary_pipeline,
    department_trend_pipeline,
    employee_monthly_pipeline,
    epoch_ms_to_datetime,
    late_leaderboard_pipeline,
    truncate_seconds,
    IDX_ATT_DATE_EMP,
    IDX_ATT_EMP_DATE,
    IDX_ATT_STATUS_DATE_EMP,
)

UTC = timezone.utc


class AttendanceTimeRuleTests(unittest.TestCase):
    def test_late_grace_period_is_strictly_greater_than_ten_minutes(self):
        # 09:40:00 IST is exactly ten minutes after the 09:30 shift start.
        self.assertEqual(
            compute_late_minutes(datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC), "09:30", "2026-07-06"),
            0,
        )
        # Once over the threshold, count whole minutes from the shift start.
        self.assertEqual(
            compute_late_minutes(datetime(2026, 7, 6, 4, 10, 1, tzinfo=UTC), "09:30", "2026-07-06"),
            10,
        )
        self.assertEqual(
            compute_late_minutes(datetime(2026, 7, 6, 4, 45, 59, tzinfo=UTC), "09:30", "2026-07-06"),
            45,
        )

    def test_early_arrival_is_not_late(self):
        self.assertEqual(
            compute_late_minutes(datetime(2026, 7, 6, 3, 58, 0, tzinfo=UTC), "09:30", "2026-07-06"),
            0,
        )

    def test_instants_are_truncated_to_whole_seconds(self):
        value = datetime(2026, 7, 6, 4, 10, 0, 987654, tzinfo=UTC)
        self.assertEqual(truncate_seconds(value), datetime(2026, 7, 6, 4, 10, 0, tzinfo=UTC))

    def test_naive_bson_datetime_is_treated_as_utc(self):
        value = datetime(2026, 7, 6, 4, 10, 0)
        self.assertEqual(truncate_seconds(value), value.replace(tzinfo=UTC))

    def test_epoch_milliseconds_convert_in_utc_and_drop_fractional_seconds(self):
        # 2026-07-06T04:10:00.999Z; milliseconds are truncated to whole seconds.
        value = epoch_ms_to_datetime(1783311000999)
        self.assertEqual(value.microsecond, 0)
        self.assertEqual(value.tzinfo, UTC)

    def test_overtime_requires_thirty_whole_minutes(self):
        # 18:59:59 IST is only 29m59s past an 18:30 shift end.
        self.assertEqual(
            compute_overtime_minutes(datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC), "09:30", "18:30", "2026-07-06"),
            0,
        )
        self.assertEqual(
            compute_overtime_minutes(datetime(2026, 7, 6, 13, 30, 0, tzinfo=UTC), "09:30", "18:30", "2026-07-06"),
            30,
        )
        self.assertEqual(
            compute_overtime_minutes(datetime(2026, 7, 6, 13, 29, 59, tzinfo=UTC), "09:30", "18:30", "2026-07-06"),
            0,
        )

    def test_overnight_overtime_uses_next_day_shift_end(self):
        # 06:30 IST on July 7 is 30 minutes after the July 7 06:00 shift end.
        self.assertEqual(
            compute_overtime_minutes(datetime(2026, 7, 7, 1, 0, 0, tzinfo=UTC), "22:00", "06:00", "2026-07-06"),
            30,
        )

    def test_overnight_punch_in_before_shift_end_belongs_to_previous_day(self):
        employee = {"shift_start": "22:00", "shift_end": "06:00"}
        # 05:59 IST on July 7 is 23:59 UTC on July 6.
        self.assertEqual(
            attendance_date_for(datetime(2026, 7, 6, 23, 59, 0, tzinfo=UTC), employee),
            "2026-07-06",
        )
        # At 06:00 IST, the attendance day is the current date.
        self.assertEqual(
            attendance_date_for(datetime(2026, 7, 7, 0, 30, 0, tzinfo=UTC), employee),
            "2026-07-07",
        )

    def test_normal_shift_date_uses_ist_not_utc_date(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        # 18:45 UTC is already the next IST date.
        self.assertEqual(
            attendance_date_for(datetime(2026, 7, 6, 18, 45, 0, tzinfo=UTC), employee),
            "2026-07-07",
        )

    def test_work_hours_uses_half_up_rounding(self):
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        self.assertEqual(compute_work_hours(start, datetime(2026, 1, 1, 4, 30, 18, tzinfo=UTC)), 4.51)
        self.assertEqual(compute_work_hours(start, datetime(2026, 1, 1, 4, 30, 9, tzinfo=UTC)), 4.50)

    def test_half_day_uses_rounded_work_hours_threshold(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        start = datetime(2026, 7, 6, 3, 30, 0, tzinfo=UTC)
        below = calculate_derived_fields(
            employee, "2026-07-06", "PRESENT", start,
            start + timedelta(hours=4, minutes=29, seconds=42),
        )
        at_threshold = calculate_derived_fields(
            employee, "2026-07-06", "PRESENT", start,
            start + timedelta(hours=4, minutes=30),
        )
        self.assertEqual(below["work_hours"], 4.50)
        self.assertFalse(below["half_day"], "half_day is based on the rounded duration")
        self.assertEqual(at_threshold["work_hours"], 4.50)
        self.assertFalse(at_threshold["half_day"])

    def test_short_shift_is_half_day_after_rounding(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        start = datetime(2026, 7, 6, 3, 30, 0, tzinfo=UTC)
        derived = calculate_derived_fields(
            employee, "2026-07-06", "WFH", start,
            start + timedelta(hours=4, minutes=29, seconds=30),
        )
        self.assertEqual(derived["work_hours"], 4.49)
        self.assertTrue(derived["half_day"])

    def test_non_positive_work_duration_is_rejected(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        instant = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        with self.assertRaises(HTTPException) as ctx:
            calculate_derived_fields(employee, "2026-07-06", "PRESENT", instant, instant)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_work_hours_over_24_hours_is_rejected(self):
        employee = {"shift_start": "09:30", "shift_end": "18:30"}
        instant = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)
        with self.assertRaises(HTTPException) as ctx:
            calculate_derived_fields(employee, "2026-07-06", "PRESENT", instant, instant + timedelta(days=1, seconds=1))
        self.assertEqual(ctx.exception.status_code, 422)


class RequestValidationTests(unittest.TestCase):
    def test_employee_shift_defaults_follow_contract(self):
        employee = EmployeeIn.model_validate({
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
        })
        self.assertEqual(employee.shift_start, "09:30")
        self.assertEqual(employee.shift_end, "18:30")

    def test_employee_code_pattern_is_enforced(self):
        with self.assertRaises(ValidationError):
            EmployeeIn.model_validate({
                "emp_code": "employee-1", "name": "Asha", "email": "a@example.com",
                "department": "Engineering", "joined_on": "2026-01-05",
            })

    def test_regularization_ignores_client_supplied_derived_fields(self):
        # The contract explicitly says clients cannot control derived fields;
        # if they send them, the API ignores them instead of trusting or storing them.
        body = RegularizeIn.model_validate({
            "reason": "Corrected by manager",
            "regularized_by": "manager-1",
            "status": "PRESENT",
            "punch_in": 1783311000000,
            "work_hours": 999,
            "late_minutes": 999,
            "overtime_minutes": 999,
            "half_day": True,
        })
        self.assertNotIn("work_hours", body.model_fields_set)
        self.assertNotIn("late_minutes", body.model_fields_set)
        self.assertNotIn("overtime_minutes", body.model_fields_set)
        self.assertNotIn("half_day", body.model_fields_set)

    def test_employee_joined_on_must_be_a_real_calendar_date(self):
        with self.assertRaises(ValidationError):
            EmployeeIn.model_validate({
                "emp_code": "EMP0001", "name": "Asha", "email": "a@example.com",
                "department": "Engineering", "joined_on": "2026-02-30",
            })

    def test_equal_shift_times_are_rejected_for_new_employee(self):
        with self.assertRaises(ValidationError):
            EmployeeIn.model_validate({
                "emp_code": "EMP0001", "name": "Asha", "email": "a@example.com",
                "department": "Engineering", "joined_on": "2026-01-05",
                "shift_start": "09:30", "shift_end": "09:30",
            })

    def test_punch_in_allows_omitted_timestamp_but_rejects_null(self):
        self.assertIsNone(PunchInIn.model_validate({"emp_code": "EMP0001"}).punched_at)
        with self.assertRaises(ValidationError):
            PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": None})

    def test_punch_in_rejects_seconds_floats_and_strings_as_timestamps(self):
        for value in (1783311000, 1783311000000.0, "1783311000000"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": value})

    def test_regularization_ignores_derived_fields(self):
        body = RegularizeIn.model_validate({
            "reason": "Correct attendance", "regularized_by": "hr.admin", "work_hours": 9.0,
        })
        self.assertNotIn("work_hours", body.model_fields_set)

    def test_regularization_rejects_explicit_null_punch_times(self):
        with self.assertRaises(ValidationError):
            RegularizeIn.model_validate({
                "reason": "Correct attendance", "regularized_by": "hr.admin", "punch_in": None,
            })

    def test_regularization_rejects_blank_reason(self):
        with self.assertRaises(ValidationError):
            RegularizeIn.model_validate({
                "reason": "     ", "regularized_by": "hr.admin",
            })


class AttendanceFilterTests(unittest.TestCase):
    def test_attendance_filter_keeps_an_explicit_empty_employee_code(self):
        self.assertEqual(attendance_list_filter("", None, None, None), {"emp_code": ""})

    def test_attendance_filter_uses_inclusive_iso_date_bounds(self):
        actual = attendance_list_filter(
            None, date(2026, 7, 1), date(2026, 7, 31), "PRESENT",
        )
        self.assertEqual(actual, {
            "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"},
            "status": "PRESENT",
        })

    def test_attendance_list_chooses_indexes_for_filter_shape(self):
        self.assertEqual(attendance_list_hint("EMP0001", "PRESENT"), IDX_ATT_EMP_DATE)
        self.assertEqual(attendance_list_hint(None, "PRESENT"), IDX_ATT_STATUS_DATE_EMP)
        self.assertEqual(attendance_list_hint(None, None), IDX_ATT_DATE_EMP)


class PipelineCorrectnessShapeTests(unittest.TestCase):
    def test_monthly_pipeline_uses_employee_join_and_calendar_workdays(self):
        pipeline = employee_monthly_pipeline("EMP0001", "2026-07-01", "2026-08-01")
        self.assertEqual(pipeline[0], {"$match": {"emp_code": "EMP0001"}})
        self.assertTrue(any("$lookup" in stage and stage["$lookup"].get("from") == "attendance_logs" for stage in pipeline))
        rendered = repr(pipeline)
        self.assertIn("$dayOfWeek", rendered)
        self.assertIn("working_days", rendered)
        self.assertIn("total_overtime_minutes", rendered)
        self.assertIn("attendance_pct", rendered)

    def test_department_summary_starts_from_employees_to_keep_zero_log_headcount(self):
        pipeline = department_summary_pipeline("2026-07-01", "2026-08-01")
        self.assertTrue(any("$lookup" in stage and stage["$lookup"].get("from") == "attendance_logs" for stage in pipeline))
        self.assertEqual(pipeline[0]["$match"], {"joined_on": {"$lte": "2026-07-31"}})
        self.assertTrue(any("headcount" in stage.get("$group", {}) for stage in pipeline))
        rendered = repr(pipeline)
        self.assertIn("work_hours_count", rendered)
        self.assertIn("PRESENT", rendered)
        self.assertIn("ON_DUTY", rendered)

    def test_leaderboard_ranks_before_cutoff_filter_and_has_tie_sort(self):
        pipeline = late_leaderboard_pipeline("2026-07-01", "2026-08-01", 10)
        set_window_index = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
        rank_cutoff_index = next(i for i, stage in enumerate(pipeline) if "$match" in stage and "rank" in stage["$match"])
        final_sort_index = next(i for i, stage in enumerate(pipeline) if "$sort" in stage and "emp_code" in stage["$sort"])
        self.assertLess(set_window_index, rank_cutoff_index)
        self.assertLess(rank_cutoff_index, final_sort_index)
        self.assertEqual(pipeline[set_window_index]["$setWindowFields"]["sortBy"], {"total_late_minutes": -1})
        self.assertEqual(pipeline[final_sort_index]["$sort"], {"total_late_minutes": -1, "emp_code": 1})

    def test_department_filter_is_applied_before_leaderboard_ranking(self):
        pipeline = late_leaderboard_pipeline("2026-07-01", "2026-08-01", 5, "Engineering")
        lookup_index = next(i for i, stage in enumerate(pipeline) if "$lookup" in stage)
        department_match_index = next(i for i, stage in enumerate(pipeline) if stage.get("$match") == {"employee.department": "Engineering"})
        ranking_index = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
        self.assertLess(lookup_index, department_match_index)
        self.assertLess(department_match_index, ranking_index)

    def test_department_trend_is_gap_filled_and_windowed_in_mongodb(self):
        pipeline = department_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
        rendered = repr(pipeline)
        self.assertIn("$range", rendered)
        self.assertTrue(any("$lookup" in stage for stage in pipeline))
        self.assertTrue(any("$setWindowFields" in stage for stage in pipeline))
        window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
        self.assertEqual(window["output"]["_moving_avg_raw"]["window"], {"documents": [-6, 0]})
        self.assertEqual(pipeline[-1], {"$sort": {"date": 1}})

    def test_trend_pipeline_contains_per_day_headcount_and_null_rate_conditions(self):
        pipeline = department_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
        rendered = repr(pipeline)
        self.assertIn("joined_on", rendered)
        self.assertIn("is_working_day", rendered)
        self.assertIn("attendance_rate", rendered)
        self.assertIn("present_count", rendered)
        self.assertIn("late_count", rendered)

    def test_trend_record_counts_are_not_filtered_by_join_date(self):
        pipeline = department_trend_pipeline("Engineering", date(2026, 7, 1), date(2026, 7, 7))
        rendered = repr(pipeline)
        # `joined_on` is used for headcount, while the specified numerator and
        # late count are based on stored records for that day.
        self.assertIn("present_count", rendered)
        self.assertIn("late_count", rendered)
        self.assertNotIn("employee_is_active", rendered)


if __name__ == "__main__":
    unittest.main()
