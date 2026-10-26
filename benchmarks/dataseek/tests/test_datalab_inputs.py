"""Offline regressions for authenticated file inputs and isolated staging cleanup."""
import asyncio
import hashlib
import io
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from benchmarks.dataseek import baseline_runner as b
from benchmarks.dataseek.http_client import DataLabClient


INPUT = {"file_id": "owned-id", "filename": "table.csv", "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}


class FileInputTests(unittest.TestCase):
    def test_worker_contract_rejects_anonymous_paths_duplicate_and_oversized_inputs(self):
        for user in (None, "", "anonymous"):
            with self.assertRaises(ValueError): b.verified_file_inputs(user, [INPUT])
        for files in ([{**INPUT, "filename": "../table.csv"}], [{**INPUT, "host_path": "/private/data"}],
                      [INPUT, INPUT], [{**INPUT, "size": 257 * 1024**2}], [{**INPUT, "sha256": "x" * 64}]):
            with self.assertRaises(ValueError): b.verified_file_inputs("user", files)
        self.assertEqual(b.verified_file_inputs("user", [INPUT]), [INPUT])

    def test_download_reauthorizes_owner_rechecks_bytes_and_closes_stream(self):
        module = ModuleType("app.domain.utils.file_stream")
        async def rewind(source): return source
        module.rewindable_file, module.close_file = rewind, lambda source: source.close()
        for owner, raw, expected_error in (("user", b"abc", None), ("other", b"abc", "input_owner_or_identity_mismatch"),
                                            ("user", b"xyz", "input_bytes_changed")):
            with self.subTest(owner=owner, raw=raw):
                stream, calls = io.BytesIO(raw), []
                async def download(file_id, user_id):
                    calls.append((file_id, user_id))
                    return stream, SimpleNamespace(**{key: INPUT[key] for key in ("file_id", "filename", "size")}, user_id=owner)
                with patch.dict(sys.modules, {"app.domain.utils.file_stream": module}):
                    if expected_error:
                        with self.assertRaises(b.BaselineStopped) as error:
                            asyncio.run(b._download_owned_inputs(SimpleNamespace(download_file=download), "user", [INPUT]))
                        self.assertEqual(error.exception.code, expected_error)
                    else:
                        self.assertEqual(asyncio.run(b._download_owned_inputs(SimpleNamespace(download_file=download), "user", [INPUT])), [("table.csv", b"abc")])
                self.assertEqual(calls, [("owned-id", "user")])
                self.assertTrue(stream.closed)

    def test_volume_cleanup_refuses_foreign_replacement_and_confirms_owned_absence(self):
        class NotFound(Exception): pass
        for changed in (False, True):
            state = {"present": True, "removed": False, "closed": False}
            identity = {"name": "datalab-benchmark-inputs-" + "a" * 32, "labels": {"owner": "ours"}, "created_at": "today"}
            def remove(force=False):
                self.assertFalse(force)
                state.update(present=False, removed=True)
            volume = SimpleNamespace(name=identity["name"], attrs={"Labels": {"owner": "other"} if changed else identity["labels"], "CreatedAt": "today"}, remove=remove)
            def get(name):
                self.assertEqual(name, identity["name"])
                if not state["present"]: raise NotFound()
                return volume
            client = SimpleNamespace(volumes=SimpleNamespace(get=get), close=lambda: state.update(closed=True))
            docker = SimpleNamespace(from_env=lambda **kwargs: client, errors=SimpleNamespace(NotFound=NotFound))
            if changed:
                with self.assertRaises(b.BaselineStopped): b._remove_owned_input_volume(identity, docker_module=docker)
                self.assertFalse(state["removed"])
            else:
                b._remove_owned_input_volume(identity, docker_module=docker)
                self.assertTrue(state["removed"])
            self.assertTrue(state["closed"])

    def test_access_token_header_is_memory_only_and_redirects_are_disabled(self):
        captured = []
        client = DataLabClient("http://127.0.0.1:10030", token="test-bearer")
        self.assertTrue(any(type(handler).__name__ == "NoRedirect" for handler in client.opener.handlers))
        self.assertTrue(all(not getattr(handler, "proxies", {}) for handler in client.opener.handlers))
        client.opener = SimpleNamespace(open=lambda request, **kw: captured.append(request))
        client._open("GET", "/api/v1/auth/me")
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer test-bearer")
        self.assertNotIn("test-bearer", captured[0].full_url)
        with self.assertRaises(ValueError): DataLabClient("http://127.0.0.1:10030", token="bad\r\nHeader: injected")


if __name__ == "__main__": unittest.main()
