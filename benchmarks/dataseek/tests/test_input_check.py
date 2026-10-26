import unittest
from unittest.mock import patch

from benchmarks.dataseek.input_check import verify_service_inputs, verify_api_origin
from benchmarks.dataseek.environment import EnvironmentInspectionError


class InputCheckTests(unittest.TestCase):
    def test_remote_service_cannot_borrow_local_fingerprints(self):
        with self.assertRaises(EnvironmentInspectionError):
            verify_api_origin("https://example.org")

    def test_local_service_must_match_inspected_port(self):
        ports = '{"80/tcp":[{"HostIp":"127.0.0.1","HostPort":"7001"}]}'
        with patch("benchmarks.dataseek.input_check._run", return_value=ports):
            self.assertTrue(verify_api_origin("http://127.0.0.1:7001")["local_frontend_verified"])
            with self.assertRaises(EnvironmentInspectionError):
                verify_api_origin("http://127.0.0.1:7000")

    def task(self):
        return {"id": "t", "dataset_id": "d", "domain": "general", "source_family_id": "d",
                "prompt": "Read table.csv", "expected": {"answer": {"count": 123456}},
                "required_artifacts": [{"name": "answer.json", "kind": "json"}],
                "input_files": ["table.csv"],
                "metadata": {"input_identities": [{"name": "table.csv", "size": 3, "sha256": "a" * 64}]}}

    def client(self, raw=b"abc", **overrides):
        from types import SimpleNamespace
        import hashlib
        info = {"file_id": "owned", "filename": "table.csv", "size": 3, **overrides}
        calls = []
        def api(method, path):
            calls.append(path)
            return {"id": "user-1"} if path.endswith("/auth/me") else info
        return SimpleNamespace(token="test", api=api, download=lambda *a, **kw: raw, calls=calls)

    def check(self, task=None, client=None):
        import hashlib
        task = task or self.task()
        if "input_identities" in task.get("metadata", {}):
            task["metadata"]["input_identities"][0]["sha256"] = hashlib.sha256(b"abc").hexdigest()
        return verify_service_inputs([task], client or self.client(), {"d": {"table.csv": "owned"}})

    def test_private_checker_uses_only_owned_file_routes(self):
        client = self.client()
        with patch("benchmarks.dataseek.input_check.verify_user_controls"):
            result = self.check(client=client)
        self.assertTrue(result["all_inputs_match"])
        self.assertEqual(client.calls, ["/api/v1/auth/me", "/api/v1/files/owned/info"])
        self.assertNotIn("123456", str(result))
        self.assertNotIn("expected", str(result))

    def test_changed_bytes_stop_before_analysis(self):
        with patch("benchmarks.dataseek.input_check.verify_user_controls"):
            with self.assertRaises(EnvironmentInspectionError): self.check(client=self.client(raw=b"xyz"))
            with self.assertRaises(EnvironmentInspectionError): self.check(client=self.client(filename="other.csv"))

    def test_automatic_skills_are_not_silently_ignored(self):
        with patch("benchmarks.dataseek.input_check._docker_python", return_value={"found": True, "auto_enabled_skills": ["unexpected"]}):
            with self.assertRaises(EnvironmentInspectionError): self.check()

    def test_missing_identities_fail_closed(self):
        task = self.task()
        del task["metadata"]["input_identities"]
        with patch("benchmarks.dataseek.input_check.verify_user_controls"), self.assertRaises(ValueError):
            self.check(task=task)

    def test_anonymous_missing_auth_and_path_bindings_fail_closed(self):
        from benchmarks.dataseek.input_check import current_user, validate_bindings
        client = self.client()
        client.token = ""
        with self.assertRaises(EnvironmentInspectionError): current_user(client)
        client.token = "test"
        client.api = lambda *a: {"id": "anonymous"}
        with self.assertRaises(EnvironmentInspectionError): current_user(client)
        for binding in ({"d": {"../input.csv": "owned"}}, {"d": {"input.csv": "/private/input"}}):
            with self.assertRaises(ValueError): validate_bindings(binding)


if __name__ == "__main__":
    unittest.main()
