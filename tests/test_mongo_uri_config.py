"""Configuration checks for both supported MongoDB deployment options."""
import os
import unittest

# Import-time configuration for a lazy, unused MongoClient in database-independent tests.
os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGO_DB", "attendance_test")

from app.main import resolve_mongo_uri


class MongoURIConfigurationTests(unittest.TestCase):
    def test_missing_or_blank_uri_is_rejected_instead_of_using_a_hard_coded_connection(self):
        with self.assertRaisesRegex(ValueError, "MONGO_URI must be set"):
            resolve_mongo_uri(None)
        with self.assertRaisesRegex(ValueError, "MONGO_URI must be set"):
            resolve_mongo_uri("   ")

    def test_standard_mongodb_uri_supports_local_docker_or_standard_servers(self):
        uri = "mongodb://127.0.0.1:27017"
        self.assertEqual(resolve_mongo_uri(uri), uri)

    def test_srv_uri_supports_mongodb_atlas(self):
        uri = "mongodb+srv://test_user:encoded_password@cluster.example.mongodb.net/?retryWrites=true&w=majority"
        self.assertEqual(resolve_mongo_uri(uri), uri)

    def test_whitespace_around_uri_is_removed(self):
        uri = " mongodb://127.0.0.1:27017 "
        self.assertEqual(resolve_mongo_uri(uri), "mongodb://127.0.0.1:27017")

    def test_unsupported_uri_scheme_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "MONGO_URI must start with"):
            resolve_mongo_uri("https://example.com/database")


if __name__ == "__main__":
    unittest.main()
