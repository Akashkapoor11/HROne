"""Endpoint smoke tests using a thread-safe in-memory Mongo-like test double.

These verify API logic and compare-and-set behavior without pretending to validate
real MongoDB aggregation execution, index selection, or explain plans. Run against
real MongoDB separately for those checks.
"""
from __future__ import annotations

import copy
import os
import threading
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

# Import-time configuration for a lazy, unused MongoClient in database-independent tests.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app import main

UTC = timezone.utc


def _matches(document: dict, query: dict) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(document, condition) for condition in expected):
                return False
            continue
        actual = document.get(key, _MISSING)
        if isinstance(expected, dict) and any(str(op).startswith("$") for op in expected):
            for op, value in expected.items():
                if op == "$exists":
                    if (actual is not _MISSING) != bool(value):
                        return False
                elif op == "$type":
                    if value == "date" and not isinstance(actual, datetime):
                        return False
                elif op == "$lte":
                    if actual is _MISSING or actual > value:
                        return False
                elif op == "$lt":
                    if actual is _MISSING or actual >= value:
                        return False
                elif op == "$gte":
                    if actual is _MISSING or actual < value:
                        return False
                elif op == "$gt":
                    if actual is _MISSING or actual <= value:
                        return False
                else:
                    raise AssertionError(f"Unsupported fake query operator: {op}")
        elif actual is _MISSING or actual != expected:
            return False
    return True


_MISSING = object()


class Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.documents.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
        return self

    def skip(self, number):
        self.documents = self.documents[number:]
        return self

    def limit(self, number):
        self.documents = self.documents[:number]
        return self

    def hint(self, _hint):
        return self

    def __iter__(self):
        return iter(copy.deepcopy(self.documents))


class Collection:
    def __init__(self, name):
        self.name = name
        self.documents = []
        self.lock = threading.RLock()
        self.next_id = 1

    def create_index(self, *_args, **_kwargs):
        return None

    def insert_one(self, document):
        with self.lock:
            if self.name == "employees" and any(d.get("emp_code") == document.get("emp_code") for d in self.documents):
                raise DuplicateKeyError("duplicate emp_code")
            if self.name == "attendance_logs" and any(
                d.get("emp_code") == document.get("emp_code") and d.get("date") == document.get("date")
                for d in self.documents
            ):
                raise DuplicateKeyError("duplicate attendance natural key")
            saved = copy.deepcopy(document)
            saved.setdefault("_id", self.next_id)
            self.next_id += 1
            self.documents.append(saved)
            return type("InsertResult", (), {"inserted_id": saved["_id"]})()

    def find_one(self, query, projection=None, sort=None, hint=None):
        with self.lock:
            found = [d for d in self.documents if _matches(d, query)]
            if sort:
                for key, direction in reversed(sort):
                    found.sort(key=lambda d: (d.get(key) is None, d.get(key)), reverse=direction < 0)
            if not found:
                return None
            result = copy.deepcopy(found[0])
            if projection:
                include = {key for key, value in projection.items() if value}
                if include:
                    result = {k: v for k, v in result.items() if k in include or k == "_id" and projection.get("_id")}
                else:
                    result = {k: v for k, v in result.items() if not projection.get(k, 0)}
            return result

    def find(self, query=None, projection=None):
        query = query or {}
        with self.lock:
            docs = [copy.deepcopy(d) for d in self.documents if _matches(d, query)]
        if projection:
            include = {key for key, value in projection.items() if value}
            if include:
                docs = [{k: v for k, v in d.items() if k in include or k == "_id" and projection.get("_id")} for d in docs]
            else:
                docs = [{k: v for k, v in d.items() if not projection.get(k, 0)} for d in docs]
        return Cursor(docs)

    def count_documents(self, query, hint=None):
        with self.lock:
            return sum(1 for d in self.documents if _matches(d, query))

    def find_one_and_update(self, query, update, return_document=None):
        with self.lock:
            for document in self.documents:
                if _matches(document, query):
                    before = copy.deepcopy(document)
                    for key, value in update.get("$set", {}).items():
                        document[key] = copy.deepcopy(value)
                    for key, value in update.get("$push", {}).items():
                        document.setdefault(key, []).append(copy.deepcopy(value))
                    return copy.deepcopy(document if return_document else before)
        return None


class MemoryDB:
    def __init__(self):
        self.employees = Collection("employees")
        self.attendance_logs = Collection("attendance_logs")

    def __getitem__(self, name):
        return getattr(self, name)


class EmployeeAndAttendanceAPISmokeTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = MemoryDB()
        self.patch_db = patch.object(main, "db", self.memory_db)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        employee = main.EmployeeIn.model_validate({
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-05",
            "shift_start": "09:30",
            "shift_end": "18:30",
        })
        main.create_employee(employee)

    def test_duplicate_employee_creation_returns_conflict(self):
        duplicate = main.EmployeeIn.model_validate({
            "emp_code": "EMP0001", "name": "Duplicate", "email": "dup@example.com",
            "department": "Engineering", "joined_on": "2026-01-05",
        })
        with self.assertRaises(HTTPException) as ctx:
            main.create_employee(duplicate)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_employee_pagination_counts_only_filtered_rows(self):
        other = main.EmployeeIn.model_validate({
            "emp_code": "EMP0002", "name": "Ravi Shah", "email": "ravi@example.com",
            "department": "Sales", "joined_on": "2026-01-05",
        })
        main.create_employee(other)
        page = main.list_employees(department="Engineering", page=1, page_size=20)
        self.assertEqual(page["total"], 1)
        self.assertEqual([item["emp_code"] for item in page["items"]], ["EMP0001"])

    def test_duplicate_punch_in_returns_conflict_and_keeps_one_record(self):
        timestamp = 1783311000000
        request = main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": timestamp})
        main.punch_in(request)
        with self.assertRaises(HTTPException) as ctx:
            main.punch_in(request)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(self.memory_db.attendance_logs.documents), 1)

    def test_attendance_listing_applies_filters_before_total_and_pagination(self):
        main.punch_in(main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783311000000, "status": "PRESENT"}))
        main.punch_in(main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783397400000, "status": "WFH"}))
        page = main.list_attendance(
            emp_code="EMP0001",
            date_from=date(2026, 7, 6),
            date_to=date(2026, 7, 7),
            status="PRESENT",
            page=1,
            page_size=1,
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["date"], "2026-07-06")
        self.assertNotIn("_id", page["items"][0])

    def test_two_simultaneous_punch_ins_have_one_winner(self):
        timestamp = 1783311000000
        request = main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": timestamp})
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def run():
            barrier.wait()
            try:
                value = main.punch_in(request)
                outcome = ("ok", value)
            except HTTPException as exc:
                outcome = ("error", exc.status_code)
            with result_lock:
                results.append(outcome)

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sorted(code for kind, code in results if kind == "error"), [409])

    def test_two_simultaneous_punch_outs_have_one_winner(self):
        punched_in = main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783311000000})
        main.punch_in(punched_in)
        punched_out = main.PunchOutIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783344000000})
        barrier = threading.Barrier(2)
        results = []
        result_lock = threading.Lock()

        def run():
            barrier.wait()
            try:
                value = main.punch_out(punched_out)
                outcome = ("ok", value)
            except HTTPException as exc:
                outcome = ("error", exc.status_code)
            with result_lock:
                results.append(outcome)

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sorted(code for kind, code in results if kind == "error"), [409])
        record = self.memory_db.attendance_logs.documents[0]
        self.assertIsNotNone(record["punch_out"])
        self.assertGreater(record["work_hours"], 0)

    def test_manual_correction_appends_audit_history_only_for_changed_fields(self):
        main.punch_in(main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783311000000}))
        response = main.regularize_attendance(
            "EMP0001",
            date(2026, 7, 6),
            main.RegularizeIn.model_validate({
                "status": "WFH",
                "reason": "Update work status",
                "regularized_by": "hr.admin",
            }),
        )
        self.assertEqual(response["status"], "WFH")
        self.assertEqual(len(response["history"]), 1)
        self.assertEqual(list(response["history"][0]["changes"].keys()), ["status"])

    def test_two_concurrent_regularizations_do_not_overwrite_audit_history(self):
        main.punch_in(main.PunchInIn.model_validate({"emp_code": "EMP0001", "punched_at": 1783311000000}))
        collection = self.memory_db.attendance_logs
        original_find_one = collection.find_one
        snapshot_barrier = threading.Barrier(2)

        def synchronized_find_one(query, projection=None, sort=None, hint=None):
            result = original_find_one(query, projection=projection, sort=sort, hint=hint)
            if query == {"emp_code": "EMP0001", "date": "2026-07-06"}:
                # Force both corrections to calculate from the same old version.
                snapshot_barrier.wait(timeout=2)
            return result

        with patch.object(collection, "find_one", side_effect=synchronized_find_one):
            barrier = threading.Barrier(2)
            results = []
            result_lock = threading.Lock()

            def run(new_status):
                barrier.wait()
                try:
                    response = main.regularize_attendance(
                        "EMP0001",
                        date(2026, 7, 6),
                        main.RegularizeIn.model_validate({
                            "status": new_status,
                            "reason": "Correct attendance status",
                            "regularized_by": "hr.admin",
                        }),
                    )
                    outcome = ("ok", response)
                except HTTPException as exc:
                    outcome = ("error", exc.status_code)
                with result_lock:
                    results.append(outcome)

            workers = [
                threading.Thread(target=run, args=("WFH",)),
                threading.Thread(target=run, args=("ON_DUTY",)),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(sum(1 for kind, _ in results if kind == "ok"), 1)
        self.assertEqual(sorted(code for kind, code in results if kind == "error"), [409])
        stored = collection.documents[0]
        self.assertEqual(len(stored["history"]), 1)
        self.assertIn(stored["status"], ("WFH", "ON_DUTY"))

    def test_unknown_employee_is_not_found_on_punch_in(self):
        request = main.PunchInIn.model_validate({"emp_code": "EMP9999", "punched_at": 1783311000000})
        with self.assertRaises(HTTPException) as ctx:
            main.punch_in(request)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_http_status_codes_validation_and_punch_lifecycle(self):
        with TestClient(main.app) as api:
            invalid_timestamp = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783311000})
            self.assertEqual(invalid_timestamp.status_code, 422)

            missing_employee = api.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": 1783311000000})
            self.assertEqual(missing_employee.status_code, 404)

            body = {"emp_code": "EMP0001", "punched_at": 1783311000000, "status": "PRESENT"}
            created = api.post("/attendance/punch-in", json=body)
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["date"], "2026-07-06")
            self.assertEqual(api.post("/attendance/punch-in", json=body).status_code, 409)

            punch_out_body = {"emp_code": "EMP0001", "punched_at": 1783339800000}
            closed = api.post("/attendance/punch-out", json=punch_out_body)
            self.assertEqual(closed.status_code, 200)
            self.assertEqual(closed.json()["work_hours"], 8.0)
            self.assertEqual(api.post("/attendance/punch-out", json=punch_out_body).status_code, 409)

    def test_http_regularization_ignores_client_supplied_derived_fields(self):
        with TestClient(main.app) as api:
            created = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783311000000})
            self.assertEqual(created.status_code, 201)
            updated = api.patch(
                "/attendance/EMP0001/2026-07-06",
                json={
                    "status": "WFH",
                    "reason": "Correct status",
                    "regularized_by": "hr.admin",
                    "late_minutes": 999,
                    "work_hours": 99,
                    "half_day": True,
                },
            )
            self.assertEqual(updated.status_code, 200)
            body = updated.json()
            self.assertEqual(body["status"], "WFH")
            self.assertEqual(body["late_minutes"], 0)
            self.assertIsNone(body["work_hours"])
            self.assertFalse(body["half_day"])
            self.assertEqual(set(body["history"][-1]["changes"]), {"status"})
            self.assertEqual(
                body["history"][-1]["changes"]["status"],
                {"from": "PRESENT", "to": "WFH"},
            )


if __name__ == "__main__":
    unittest.main()
