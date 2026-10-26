"""Release preparation preserves storage and never executes dotenv contents."""
import copy
from contextlib import contextmanager, redirect_stdout
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('prepare_release', Path(__file__).with_name('prepare.py'))
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
guard = prepare.guard


def storage_containers():
    return [dict(Config={'Labels': {'com.docker.compose.project': 'datalab', 'com.docker.compose.service': service}},
                 Mounts=[{'Destination': dest, 'Type': 'volume', 'Name': ('f' * 64 if key == 'redis-data' else 'datalab-' + key)}
                         for dest, key in mounts.items() if key != 'jupyter-workspaces'])
            for service, mounts in guard.PERSISTENT_MOUNTS.items()]


class PreparationTests(unittest.TestCase):
    def test_unchanged_runtime_references_survive_frontend_only_release(self):
        for kind in ("sandbox", "jupyter"):
            previous = {kind: {"reference": f"datalab-{kind}:release-one", "id": "sha256:same"}}
            self.assertEqual(prepare.image_reference(kind, "sha256:same", "release-two", previous),
                             f"datalab-{kind}:release-one")

    def test_changed_runtime_gets_a_new_pinned_reference(self):
        previous = {"sandbox": {"reference": "datalab-sandbox:release-one", "id": "sha256:old"}}
        self.assertEqual(prepare.image_reference("sandbox", "sha256:new", "release-two", previous),
                         "datalab-sandbox:release-two")

    def test_application_references_remain_release_specific(self):
        previous = {"frontend": {"reference": "datalab-frontend:release-one", "id": "sha256:same"}}
        self.assertEqual(prepare.image_reference("frontend", "sha256:same", "release-two", previous),
                         "datalab-frontend:release-two")

    def test_existing_anonymous_storage_is_pinned_without_migration(self):
        names = prepare.inspect_existing_storage(storage_containers(), {})
        self.assertEqual(names['redis-data'], 'f' * 64)
        self.assertEqual(names['jupyter-workspaces'], 'datalab-jupyter-workspaces')
        self.assertEqual(len(names), len(guard.STATE_VOLUMES))

    def test_foreign_project_bind_and_missing_data_are_refused(self):
        for change in (
            lambda items: items[0]['Config']['Labels'].update({'com.docker.compose.project': 'ai-dataseek'}),
            lambda items: items[0]['Mounts'][0].update(Type='bind'),
            lambda items: items[0]['Mounts'][0].update(Name='ai-dataseek-dataset-data'),
            lambda items: items[0]['Mounts'].pop(0),
            lambda items: items.pop(),
        ):
            items = storage_containers()
            change(items)
            with self.assertRaises(guard.DeploymentError):
                prepare.inspect_existing_storage(items, {})

    def test_dotenv_text_is_preserved_without_shell_evaluation(self):
        source = 'API_KEY="$(touch should-never-exist)"\nFRONTEND_IMAGE=old\nexport FRONTEND_IMAGE=duplicate\n# private notes\n'
        result = prepare.rewrite_environment(source, {'FRONTEND_IMAGE': 'datalab-frontend:release-1', 'REDIS_DATA_EXTERNAL': 'true'})
        self.assertIn('API_KEY="$(touch should-never-exist)"', result)
        self.assertEqual(result.count('FRONTEND_IMAGE='), 1)
        self.assertIn('# private notes', result)
        self.assertIn('REDIS_DATA_EXTERNAL=true', result)

    def test_release_environment_excludes_application_and_compose_overrides(self):
        original = {'PATH': '/safe/bin', 'HOME': '/home/operator', 'DOCKER_CONTEXT': 'desktop-linux',
                    'MINIO_SECRET_KEY': 'untrusted-shell-value', 'FRONTEND_IMAGE': 'foreign:latest',
                    'JUPYTER_WORKSPACES_HOST_ROOT': '/other', 'COMPOSE_ENV_FILES': '/foreign/.env',
                    'DATALAB_DEPLOY_ROOT': '/other-release'}
        self.assertEqual(guard.release_environment(original),
                         {'PATH': '/safe/bin', 'HOME': '/home/operator', 'DOCKER_CONTEXT': 'desktop-linux'})

    def test_private_metadata_cannot_overwrite_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            prepare.write_private(path, 'TOKEN=private\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                prepare.write_private(path, 'replacement')
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                prepare.write_private(link, 'replacement')
            self.assertEqual(path.read_text(), 'TOKEN=private\n')

    def test_release_policy_rejects_shared_volumes_and_credential_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'
            policy = {'project': 'datalab', 'port': 10030, 'origin': 'http://localhost:10030', 'direct': 'http://127.0.0.1:10030',
                      'volumes': {key: {'name': 'datalab-' + key, 'driver': 'local', 'options': {}} for key in guard.STATE_VOLUMES}}
            for change in (lambda p: p.update(origin='https://user:secret@example.org'),
                           lambda p: p['volumes']['dataset-data'].update(name='ai-dataseek-dataset-data'),
                           lambda p: p['volumes']['dataset-data'].update(name='datalab-redis-data')):
                bad = copy.deepcopy(policy)
                change(bad)
                path.write_text(json.dumps(bad))
                with self.assertRaises(guard.DeploymentError):
                    guard.load_policy(path)


class CurrentBackendPreparationTests(unittest.TestCase):
    """Use real private files and lock, but never Docker or a real environment."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.releases = self.root / "releases"
        self.base = self.releases / "current-one"
        self.base.mkdir(parents=True)
        self.current = self.root / "current"
        self.current.symlink_to(self.base)
        self.target = self.releases / "candidate-two"
        self.candidate = "datalab-backend:verified-two"
        self.identity = "sha256:" + "a" * 64
        self.frontend_candidate = "datalab-frontend:verified-two"
        self.frontend_identity = "sha256:" + "b" * 64
        self.images = {kind: {"reference": "datalab-" + kind + ":current-one", "id": "sha256:" + str(index) * 64}
                       for index, kind in enumerate(prepare.ENV_IMAGES, 1)}
        self.policy = {"project": "datalab", "port": 10030, "origin": "http://localhost:10030",
                       "direct": "http://127.0.0.1:10030", "volumes": {
                           key: {"name": "datalab-" + key, "driver": "local", "options": {}}
                           for key in guard.STATE_VOLUMES}}
        self.policy["volumes"]["redis-data"]["name"] = "f" * 64
        self.volumes = [{"Name": value["name"], "Driver": "local", "Options": {}}
                        for value in self.policy["volumes"].values()]
        self.containers = storage_containers()
        for item in self.containers:
            service = item["Config"]["Labels"]["com.docker.compose.service"]
            item.update(Image=self.images[service]["id"], State={"Running": True})
            if service == "backend":
                item["Mounts"].append({"Destination": "/data/jupyter-workspaces", "Type": "volume",
                                       "Name": "datalab-jupyter-workspaces"})
        self.config = self.configuration(self.images)
        for relative in prepare.SNAPSHOT_FILES:
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private release source: " + relative + "\n")
        self.environment = ("# owned private configuration\nAUTH_PROVIDER=password\n"
                            "API_KEY=synthetic-secret-not-for-diagnostics\n"
                            "SERVER_HOST=http://localhost:10030\n"
                            "EXTRA_LITERAL='$(no-shell-evaluation)'\n" +
                            "".join(prepare.ENV_IMAGES[kind] + "=" + value["reference"] + "\n"
                                    for kind, value in self.images.items()))
        prepare.write_private(self.base / ".env", self.environment)
        (self.base / ".local").mkdir()
        prepare.write_private(self.base / ".local/office-viewer.env", "ONLYOFFICE_JWT_SECRET=synthetic-private-value\n")
        prepare.write_private(self.base / ".datalab-release", "existing release\n")
        prepare.write_private(self.base / "release-images.json", json.dumps(self.images))
        prepare.write_private(self.base / "release-policy.json", json.dumps(self.policy))
        self.calls = []
        self.candidate_inspections = 0
        self.command_hook = None
        self.image_override = {}
        self.output = io.StringIO()
        globals_patch = patch.multiple(guard, DEPLOY_ROOT=self.root, RELEASES=self.releases,
                                      CURRENT=self.current, LOCK=self.root / "deploy.lock",
                                      ORIGIN="http://localhost:10030", DIRECT="http://127.0.0.1:10030",
                                      PORT="10030", VOLUME_IDENTITIES={})
        globals_patch.start()
        self.addCleanup(globals_patch.stop)
        command_patch = patch.object(guard, "command", side_effect=self.command)
        self.mock_command = command_patch.start()
        self.addCleanup(command_patch.stop)

    def configuration(self, images):
        services = {kind if kind in {"frontend", "backend", *guard.INFRA_KINDS} else kind + "-image":
                    {"image": value["reference"]} for kind, value in images.items()}
        services["frontend"]["ports"] = [{"published": "10030", "target": 80}]
        services["backend"]["environment"] = {
            "AUTH_PROVIDER": "password", "SERVER_HOST": "http://localhost:10030",
            "SANDBOX_IMAGE": images["sandbox"]["reference"], "JUPYTER_IMAGE": images["jupyter"]["reference"],
            "DATASET_MANAGED_VOLUME": self.policy["volumes"]["dataset-data"]["name"],
            "JUPYTER_WORKSPACES_STORAGE_ROOT": "/data/jupyter-workspaces"}
        for service, mounts in guard.PERSISTENT_MOUNTS.items():
            services[service]["volumes"] = [{"target": target, "type": "volume", "source": key}
                                           for target, key in mounts.items()]
        return {"name": "datalab", "services": services, "volumes": {
            key: {"name": value["name"], "external": True} for key, value in self.policy["volumes"].items()}}

    def command(self, args, *, cwd, env):
        self.calls.append((args, cwd, dict(env)))
        # Commands must observe the existing real deployment lock and must not
        # receive private dotenv content as an environment override.
        with open(guard.LOCK) as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.assertNotIn("API_KEY", env)
        if self.command_hook:
            self.command_hook(args, cwd)
        if args[:4] == ["./run.sh", "--env-file", ".env", "config"]:
            if cwd == self.base:
                return json.dumps(self.config)
            images = json.loads((cwd / "release-images.json").read_text())
            return json.dumps(self.configuration(images))
        if args[:4] == ["./run.sh", "--env-file", ".env", "ps"]:
            return "\n".join(str(index) * 64 for index in range(1, 5))
        if args[:3] == ["docker", "image", "inspect"]:
            if args[3:] == [self.candidate]:
                self.candidate_inspections += 1
            known = {value["reference"]: value["id"] for value in self.images.values()}
            known[self.candidate] = self.identity
            known[self.frontend_candidate] = self.frontend_identity
            known.update(self.image_override)
            return json.dumps([{"Id": known[ref]} for ref in args[3:]])
        if args[:3] == ["docker", "container", "inspect"]:
            return json.dumps(self.containers)
        if args[:3] == ["docker", "volume", "inspect"]:
            return json.dumps(self.volumes)
        self.fail("Unexpected or mutating command: " + repr(args))

    def invoke(self, **kwargs):
        options = dict(from_current="current-one", backend_image=self.candidate, backend_image_id=self.identity)
        options.update(kwargs)
        with redirect_stdout(self.output):
            prepare.prepare("candidate-two", **options)

    def test_backend_only_inherits_private_release_and_preserves_every_other_identity(self):
        before = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        with patch.dict(os.environ, {"BACKEND_IMAGE": "foreign:latest", "API_KEY": "untrusted-shell-secret"}):
            self.invoke()
        manifest = json.loads((self.target / "release-images.json").read_text())
        self.assertEqual(manifest.pop("backend"), {"reference": self.candidate, "id": self.identity})
        self.assertEqual(manifest, {kind: value for kind, value in self.images.items() if kind != "backend"})
        self.assertEqual(json.loads((self.target / "release-policy.json").read_text()), self.policy)
        self.assertEqual((self.target / ".env").read_text(),
                         self.environment.replace(self.images["backend"]["reference"], self.candidate))
        for relative in (*prepare.SNAPSHOT_FILES, ".local/office-viewer.env"):
            self.assertEqual((self.target / relative).read_bytes(), (self.base / relative).read_bytes())
        self.assertEqual({str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}, before)
        for relative in (".env", ".local/office-viewer.env", "release-images.json", "release-policy.json", ".datalab-release"):
            self.assertEqual((self.target / relative).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.target / "backend").exists())
        self.assertEqual(self.current.resolve(), self.base)
        self.assertEqual(self.candidate_inspections, 2)
        self.assertNotIn("synthetic-secret", self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue())["services_restarted"], False)

    def test_activation_only_selects_after_validation_without_service_commands(self):
        self.invoke(activate=True)
        self.assertEqual(self.current.resolve(), self.target)
        self.assertTrue(json.loads(self.output.getvalue())["selected"])

    def test_application_pair_only_changes_the_two_explicit_images(self):
        self.invoke(frontend_image=self.frontend_candidate, frontend_image_id=self.frontend_identity)
        manifest = json.loads((self.target / "release-images.json").read_text())
        expected = dict(self.images,
                        backend={"reference": self.candidate, "id": self.identity},
                        frontend={"reference": self.frontend_candidate, "id": self.frontend_identity})
        self.assertEqual(manifest, expected)
        self.assertEqual((self.target / ".env").read_text(),
                         self.environment.replace(self.images["backend"]["reference"], self.candidate)
                         .replace(self.images["frontend"]["reference"], self.frontend_candidate))
        self.assertEqual(json.loads((self.target / "release-policy.json").read_text()), self.policy)
        for relative in (*prepare.SNAPSHOT_FILES, ".local/office-viewer.env"):
            self.assertEqual((self.target / relative).read_bytes(), (self.base / relative).read_bytes())
        self.assertEqual(self.current.resolve(), self.base)
        self.assertEqual(sum(args == ["docker", "image", "inspect", self.frontend_candidate]
                             for args, _, _ in self.calls), 2)

    def test_frontend_pair_and_reference_are_checked_before_private_state_queries(self):
        for options in (
            {"frontend_image": self.frontend_candidate},
            {"frontend_image_id": self.frontend_identity},
            {"frontend_image": self.frontend_candidate, "frontend_image_id": "sha256:short"},
            *({"frontend_image": ref, "frontend_image_id": self.frontend_identity} for ref in
              ("datalab-frontend", "datalab-frontend:latest", "datalab-frontend:LATEST",
               "datalab-backend:v1", "registry.example/datalab-frontend:v1", "--help")),
        ):
            with self.subTest(options=options), self.assertRaises(guard.DeploymentError):
                self.invoke(**options)
        self.mock_command.assert_not_called()

    def test_frontend_sha_mismatch_cannot_create_or_activate_a_snapshot(self):
        self.image_override[self.frontend_candidate] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(guard.DeploymentError, "Frontend candidate image"):
            self.invoke(frontend_image=self.frontend_candidate, frontend_image_id=self.frontend_identity, activate=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.current.resolve(), self.base)

    def test_frontend_tag_drift_after_snapshot_cannot_activate(self):
        def mutate(args, cwd):
            if cwd == self.target:
                self.image_override[self.frontend_candidate] = "sha256:" + "c" * 64
        self.command_hook = mutate
        with self.assertRaisesRegex(guard.DeploymentError, "Local image identity"):
            self.invoke(frontend_image=self.frontend_candidate, frontend_image_id=self.frontend_identity, activate=True)
        self.assertEqual(self.current.resolve(), self.base)
        self.assertTrue(self.target.is_dir())

    def test_pairing_and_repository_override_rejected_before_any_queries(self):
        for options in ({"backend_image_id": None}, {"backend_image": None}, {"from_current": None},
                        {"repository": self.base}, {"from_current": "../current-one"},
                        {"backend_image_id": "sha256:short"}, {"backend_image_id": "sha256:" + "A" * 64}):
            with self.subTest(options=options), self.assertRaises(guard.DeploymentError):
                self.invoke(**options)
        self.mock_command.assert_not_called()

    def test_foreign_implicit_latest_or_option_like_references_are_rejected(self):
        for ref in ("datalab-backend", "datalab-backend:latest", "datalab-backend:LATEST", "datalab-frontend:v9",
                    "registry.example/datalab-backend:v9", "datalab-backend-other:v9", "--help", "datalab-backend:v9\n"):
            with self.subTest(ref=ref), self.assertRaises(guard.DeploymentError):
                self.invoke(backend_image=ref)
        self.mock_command.assert_not_called()

    def test_stale_current_detected_inside_lock_before_private_read(self):
        newer = self.releases / "newer"
        newer.mkdir()
        (newer / ".datalab-release").touch()
        real_lock = guard.deployment_lock
        @contextmanager
        def concurrent_publisher():
            with real_lock() as descriptor:
                self.current.unlink()
                self.current.symlink_to(newer)
                yield descriptor
        with patch.object(guard, "deployment_lock", concurrent_publisher), self.assertRaisesRegex(guard.DeploymentError, "Current release changed"):
            self.invoke(activate=True)
        self.mock_command.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.current.resolve(), newer)

    def test_candidate_sha_and_unchanged_image_sha_must_match(self):
        for reference in (self.candidate, self.images["frontend"]["reference"], self.images["sandbox"]["reference"]):
            self.image_override = {reference: "sha256:" + "b" * 64}
            with self.subTest(reference=reference), self.assertRaises(guard.DeploymentError):
                self.invoke()
            self.assertFalse(self.target.exists())
        self.assertEqual(self.current.resolve(), self.base)

    def test_missing_or_changed_volume_never_provisions_or_copies_data(self):
        self.volumes.pop()
        with self.assertRaisesRegex(guard.DeploymentError, "Missing persistent volume"):
            self.invoke()
        self.assertFalse(self.target.exists())

    def test_changed_running_image_or_mount_blocks_snapshot(self):
        original = copy.deepcopy(self.containers)
        for change in (lambda items: items[0].update(Image="sha256:" + "c" * 64),
                       lambda items: items[0]["State"].update(Running=False),
                       lambda items: items[0]["Mounts"][0].update(Name="datalab-other"),
                       lambda items: items[0]["Mounts"].pop()):
            self.containers = copy.deepcopy(original)
            change(self.containers)
            with self.subTest(change=change), self.assertRaises(guard.DeploymentError):
                self.invoke()
            self.assertFalse(self.target.exists())

    def test_private_metadata_symlink_is_rejected(self):
        for relative in (".env", ".local/office-viewer.env", "docker-compose.yml"):
            original = self.base / relative
            contents = original.read_bytes()
            outside = self.root / "outside-private"
            outside.write_bytes(contents)
            original.unlink()
            original.symlink_to(outside)
            with self.subTest(relative=relative), self.assertRaises(guard.DeploymentError):
                self.invoke()
            original.unlink()
            original.write_bytes(contents)
        self.mock_command.assert_not_called()

    def test_current_changed_during_docker_checks_cannot_be_overwritten(self):
        newer = self.releases / "newer"
        newer.mkdir()
        (newer / ".datalab-release").touch()
        def switch(args, cwd):
            if args[:3] == ["docker", "volume", "inspect"]:
                self.current.unlink()
                self.current.symlink_to(newer)
        self.command_hook = switch
        with self.assertRaisesRegex(guard.DeploymentError, "Current release changed"):
            self.invoke(activate=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.current.resolve(), newer)

    def test_candidate_tag_changed_after_snapshot_cannot_be_selected(self):
        def mutate(args, cwd):
            if cwd == self.target:
                self.image_override[self.candidate] = "sha256:" + "d" * 64
        self.command_hook = mutate
        with self.assertRaisesRegex(guard.DeploymentError, "Local image identity"):
            self.invoke(activate=True)
        self.assertEqual(self.current.resolve(), self.base)
        self.assertTrue(self.target.is_dir())  # Unselected evidence is retained.

    def test_existing_or_dangling_target_is_never_overwritten(self):
        self.target.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(guard.DeploymentError, "Release already exists"):
            self.invoke()
        self.mock_command.assert_not_called()

    def test_cli_passes_explicit_scope_and_identity_without_shell_overrides(self):
        argv = ["prepare.py", "--name", "candidate-two", "--from-current", "current-one",
                "--backend-image", self.candidate, "--backend-image-id", self.identity]
        with patch.object(sys, "argv", argv), patch.object(prepare, "prepare") as invoke:
            self.assertEqual(prepare.main(), 0)
        invoke.assert_called_once_with("candidate-two", activate=False, from_current="current-one",
                                       backend_image=self.candidate, backend_image_id=self.identity,
                                       frontend_image=None, frontend_image_id=None)

    def test_cli_passes_both_application_identities(self):
        argv = ["prepare.py", "--name", "candidate-two", "--from-current", "current-one",
                "--backend-image", self.candidate, "--backend-image-id", self.identity,
                "--frontend-image", self.frontend_candidate, "--frontend-image-id", self.frontend_identity]
        with patch.object(sys, "argv", argv), patch.object(prepare, "prepare") as invoke:
            self.assertEqual(prepare.main(), 0)
        invoke.assert_called_once_with("candidate-two", activate=False, from_current="current-one",
                                       backend_image=self.candidate, backend_image_id=self.identity,
                                       frontend_image=self.frontend_candidate, frontend_image_id=self.frontend_identity)


if __name__ == '__main__':
    unittest.main()
