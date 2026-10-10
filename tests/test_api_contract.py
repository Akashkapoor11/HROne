"""Static checks that the required HROne endpoints are registered."""
import os
import unittest

# Import-time configuration for a lazy, unused MongoClient in database-independent tests.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app.main import app


class APIRouteContractTests(unittest.TestCase):
    def test_all_required_routes_are_registered_with_the_expected_methods(self):
        routes = {
            (method, route.path)
            for route in app.routes
            for method in (getattr(route, "methods", None) or set())
        }
        expected = {
            ("GET", "/health"),
            ("POST", "/employees"),
            ("GET", "/employees"),
            ("POST", "/attendance/punch-in"),
            ("GET", "/attendance"),
            ("POST", "/attendance/punch-out"),
            ("PATCH", "/attendance/{emp_code}/{date}"),
            ("GET", "/analytics/employees/{emp_code}/monthly"),
            ("GET", "/analytics/departments/summary"),
            ("GET", "/analytics/leaderboard/late"),
            ("GET", "/analytics/departments/{department}/trend"),
            ("GET", "/admin/explain/{endpoint}"),
        }
        self.assertTrue(expected.issubset(routes), f"Missing routes: {expected - routes}")

    def test_app_has_expected_title_and_version(self):
        self.assertEqual(app.title, "Employee Attendance & Analytics API")
        self.assertEqual(app.version, "2.0.0")

    def test_operation_ids_and_parameter_names_match_the_supplied_contract(self):
        expected = {
            ("GET", "/health"): ("health", set()),
            ("POST", "/employees"): ("createEmployee", set()),
            ("GET", "/employees"): ("listEmployees", {"department", "page", "page_size"}),
            ("POST", "/attendance/punch-in"): ("punchIn", set()),
            ("POST", "/attendance/punch-out"): ("punchOut", set()),
            ("GET", "/attendance"): ("listAttendance", {"emp_code", "date_from", "date_to", "status", "page", "page_size"}),
            ("PATCH", "/attendance/{emp_code}/{date}"): ("regularizeAttendance", {"emp_code", "date"}),
            ("GET", "/analytics/employees/{emp_code}/monthly"): ("employeeMonthly", {"emp_code", "month"}),
            ("GET", "/analytics/departments/summary"): ("departmentSummary", {"month", "department"}),
            ("GET", "/analytics/leaderboard/late"): ("lateLeaderboard", {"month", "limit", "department"}),
            ("GET", "/analytics/departments/{department}/trend"): ("departmentTrend", {"department", "from", "to"}),
            ("GET", "/admin/explain/{endpoint}"): ("explainEndpoint", {"endpoint", "emp_code", "month", "department", "limit", "date_from", "date_to", "status", "page", "page_size", "from", "to"}),
        }
        schema = app.openapi()
        for (method, path), (operation_id, expected_params) in expected.items():
            with self.subTest(method=method, path=path):
                operation = schema["paths"][path][method.lower()]
                self.assertEqual(operation["operationId"], operation_id)
                actual_params = {item["name"] for item in operation.get("parameters", [])}
                self.assertEqual(actual_params, expected_params)

    def test_openapi_documents_contract_error_responses_and_calendar_format(self):
        schema = app.openapi()
        self.assertIn("503", schema["paths"]["/health"]["get"]["responses"])
        self.assertIn("409", schema["paths"]["/employees"]["post"]["responses"])
        self.assertIn("404", schema["paths"]["/attendance/punch-in"]["post"]["responses"])
        self.assertEqual(schema["components"]["schemas"]["EmployeeIn"]["properties"]["joined_on"]["format"], "date")
        self.assertEqual(schema["components"]["schemas"]["PunchInIn"]["properties"]["punched_at"]["format"], "int64")
        self.assertEqual(schema["components"]["schemas"]["EmployeeOut"]["properties"]["created_at"]["format"], "int64")
        self.assertEqual(schema["components"]["schemas"]["HistoryEntryOut"]["properties"]["at"]["format"], "int64")
        changes = schema["components"]["schemas"]["HistoryEntryOut"]["properties"]["changes"]
        value_schema = changes["additionalProperties"]
        if "$ref" in value_schema:
            value_schema = schema["components"]["schemas"][value_schema["$ref"].split("/")[-1]]
        self.assertEqual(set(value_schema["properties"]), {"from", "to"})

    def test_response_status_codes_match_the_assignment_contract(self):
        schema = app.openapi()
        expected = {
            ("/health", "get"): {"200", "503"},
            ("/employees", "post"): {"201", "409", "422"},
            ("/employees", "get"): {"200", "422"},
            ("/attendance/punch-in", "post"): {"201", "404", "409", "422"},
            ("/attendance/punch-out", "post"): {"200", "404", "409", "422"},
            ("/attendance", "get"): {"200", "422"},
            ("/attendance/{emp_code}/{date}", "patch"): {"200", "404", "409", "422"},
            ("/analytics/employees/{emp_code}/monthly", "get"): {"200", "404", "422"},
            ("/analytics/departments/summary", "get"): {"200", "422"},
            ("/analytics/leaderboard/late", "get"): {"200", "422"},
            ("/analytics/departments/{department}/trend", "get"): {"200", "404", "422"},
            ("/admin/explain/{endpoint}", "get"): {"200", "422"},
        }
        for (path, method), required_statuses in expected.items():
            with self.subTest(path=path, method=method):
                actual = set(schema["paths"][path][method]["responses"])
                self.assertTrue(required_statuses.issubset(actual), f"Missing responses for {method.upper()} {path}: {required_statuses - actual}")

    def test_pagination_and_leaderboard_bounds_match_contract(self):
        schema = app.openapi()
        employee_params = {p["name"]: p for p in schema["paths"]["/employees"]["get"]["parameters"]}
        self.assertEqual(employee_params["page"]["schema"]["minimum"], 1)
        self.assertEqual(employee_params["page"]["schema"]["default"], 1)
        self.assertEqual(employee_params["page_size"]["schema"]["minimum"], 1)
        self.assertEqual(employee_params["page_size"]["schema"]["maximum"], 100)
        self.assertEqual(employee_params["page_size"]["schema"]["default"], 20)
        leaderboard_params = {p["name"]: p for p in schema["paths"]["/analytics/leaderboard/late"]["get"]["parameters"]}
        self.assertEqual(leaderboard_params["limit"]["schema"]["minimum"], 1)
        self.assertEqual(leaderboard_params["limit"]["schema"]["maximum"], 50)
        self.assertEqual(leaderboard_params["limit"]["schema"]["default"], 10)

    def test_success_responses_publish_explicit_contract_schemas(self):
        schema = app.openapi()
        checks = [
            ("/health", "get", "HealthOut"),
            ("/employees", "post", "EmployeeOut"),
            ("/employees", "get", "EmployeePageOut"),
            ("/attendance/punch-in", "post", "AttendanceRecordOut"),
            ("/attendance/punch-out", "post", "AttendanceRecordOut"),
            ("/attendance", "get", "AttendancePageOut"),
            ("/attendance/{emp_code}/{date}", "patch", "AttendanceRecordOut"),
            ("/analytics/employees/{emp_code}/monthly", "get", "EmployeeMonthlyOut"),
            ("/analytics/departments/summary", "get", "DepartmentSummaryOut"),
            ("/analytics/leaderboard/late", "get", "LateLeaderboardOut"),
            ("/analytics/departments/{department}/trend", "get", "DepartmentTrendOut"),
            ("/admin/explain/{endpoint}", "get", "ExplainOut"),
        ]
        for path, method, model_name in checks:
            with self.subTest(path=path, method=method):
                response = schema["paths"][path][method]["responses"]["200" if method == "get" or path in {"/attendance/punch-out", "/attendance/{emp_code}/{date}"} else "201"]
                actual = response["content"]["application/json"]["schema"]
                self.assertEqual(actual["$ref"].split("/")[-1], model_name)


if __name__ == "__main__":
    unittest.main()
