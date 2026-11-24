"""Owner authentication is scratch-only and a required live receipt coordinate."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.temp_roots import REAL_TEMP_ROOT
from tests import test_zcode_conformance_receipt as conformance_tests


ROOT = Path(__file__).resolve().parents[1]


class ZcodeOwnerSigninTests(unittest.TestCase):
    def packaged_cli(self, directory, version="3.16.3"):
        """Provider bootstrap precedes login parsing in the packaged CLI."""
        app = directory / "ZCode.app"
        resources = app / "Contents/Resources"
        entry = resources / "glm/zcode.cjs"
        entry.parent.mkdir(parents=True)
        entry.write_text(
            "import json, os, pathlib, sys\n"
            "entry = pathlib.Path(__file__).resolve()\n"
            "override = os.environ.get('ZCODE_BUILTIN_PROVIDER_CONFIG_FILE')\n"
            "candidates = [pathlib.Path(override)] if override else [\n"
            "    entry.parent / 'provider/zcode-builtin.json',\n"
            "    entry.parent / '../../../../../config/provider/zcode-builtin.json']\n"
            "if not any(path.is_file() for path in candidates):\n"
            "    print('cannot locate CLI ZCode Built-in Provider Config', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "if sys.argv[1:] == ['login', 'floati-preflight', '--no-browser', '--json']:\n"
            "    print('Error: Usage: zcode login [zai|bigmodel] [--no-browser]', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "assert sys.argv[1:] == ['login', 'zai', '--no-browser', '--json']\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "assert pathlib.Path.cwd() == home\n"
            "print(json.dumps({'status': 'ready', 'credentialsPath': str(home / 'credentials.json'),\n"
            "                  'configPath': str(home / 'provider_config.json')}))\n"
        )
        config = resources / "config/provider/zcode-builtin.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"fixture": true}\n')
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version + ".fixture",
        }))
        node = directory / "node"
        node.write_text('#!/bin/sh\nexec /usr/bin/python3 -B "$@"\n')
        node.chmod(0o700)
        return node, entry, config

    def instrument(self):
        spec = importlib.util.spec_from_file_location(
            "measure_zcode_owner_signin", ROOT / "scripts/measure_zcode_conformance.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def capability(self, module, name):
        value = getattr(module, name, None)
        self.assertTrue(callable(value), "scratch owner sign-in capability is missing: " + name)
        return value

    def test_fake_signin_enables_native_success_only_with_owner_signin_receipt(self):
        module = self.instrument()
        self.capability(module, "validate_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            def signin(home):
                self.assertEqual(str(home), os.environ["HOME"])
                self.assertNotIn("ZCODE_DATA_BASE_DIR", os.environ)
                credential = home / ".zcode/v2/credentials.json"
                credential.parent.mkdir(parents=True)
                credential.write_text("fake credential - never retain these bytes")
                (credential.parent / "provider_config.json").write_text("{}")
                return {"status": "ready", "credentialsPath": str(credential),
                        "configPath": str(credential.parent / "provider_config.json")}
            command = conformance_tests.ZcodeConformanceReceiptTests().fake(directory)
            fake = Path(command[-1])
            code = fake.read_text()
            fake.write_text(code.replace(
                "prompt = sys.argv", "assert (pathlib.Path(os.environ['HOME']) / '.zcode/v2/credentials.json').is_file()\nprompt = sys.argv", 1))
            receipt = module.capture(directory / "capture.json",
                                     command=command,
                                     owner_signin=signin, timeout=20)
            self.assertEqual("ok", receipt["classification"]["status"])
            self.assertFalse(receipt["classification"]["live_conformance"])
            self.assertEqual("owner", receipt["owner_signin"]["actor"])
            self.assertEqual("scratch", receipt["owner_signin"]["home_kind"])
            self.assertEqual(receipt["invocation"]["home"], receipt["owner_signin"]["home"])
            self.assertEqual("removed", receipt["cleanup"]["credential_material"]["state"])
            self.assertNotIn("fake credential - never retain these bytes", json.dumps(receipt))
            native = copy.deepcopy(receipt)
            native["mode"] = "owner-live"
            native["invocation"]["argv"] = [*module.DEFAULT_COMMAND, "--json", "--no-color", "--prompt", "scratch task"]
            native["owner_signin"]["argv"] = [*module.DEFAULT_COMMAND, "login", "zai", "--no-browser", "--json"]
            native["owner_signin"]["flow"] = "native_login_zai"
            self.assertTrue(module.classify_receipt(native)["live_conformance"])
            upgraded = copy.deepcopy(native)
            canonical = ["/opt/homebrew/Cellar/node/99.0.0/bin/node",
                         "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"]
            upgraded["native_entry"] = {"declared_command": list(module.DEFAULT_COMMAND),
                "resolved_command": canonical, "version": "4.0.0",
                "builtin_provider_config": "/Applications/ZCode.app/Contents/Resources/config/provider/zcode-builtin.json"}
            upgraded["invocation"]["argv"][:2] = canonical
            upgraded["owner_signin"]["argv"][:2] = canonical
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("replay read installed app")),\
                    mock.patch("subprocess.Popen", side_effect=AssertionError("replay launched native CLI")):
                self.assertTrue(module.classify_receipt(upgraded)["live_conformance"],
                                "a captured native upgrade remains replayable after the installed version changes")
            upgraded["owner_signin"]["argv"][0] = "/other/node"
            self.assertFalse(module.classify_receipt(upgraded)["live_conformance"])
            for mutation in ("missing", "actor", "home", "cleanup"):
                with self.subTest(mutation=mutation):
                    invalid = copy.deepcopy(native)
                    if mutation == "missing":
                        invalid.pop("owner_signin")
                    elif mutation == "cleanup":
                        invalid["cleanup"]["credential_material"] = {"state": "cannot-measure", "code": "credential_removal_unproved"}
                    else:
                        invalid["owner_signin"][mutation] = "other"
                    self.assertFalse(module.classify_receipt(invalid)["live_conformance"])

    def test_any_reported_credential_path_outside_scratch_home_is_refused(self):
        module = self.instrument()
        validate = self.capability(module, "validate_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            home = directory / "home"
            home.mkdir()
            inside = home / "credentials.json"
            inside.write_text("inside")
            outside = directory / "outside-credentials.json"
            outside.write_text("outside")
            for paths in ([str(outside)], [str(inside), str(outside)],
                          [str(home / "../outside-credentials.json")]):
                with self.subTest(paths=paths):
                    with self.assertRaisesRegex(ValueError, "credential_outside_scratch_home"):
                        validate(home, paths)
            self.assertEqual("outside", outside.read_text())

    def test_symlink_or_hardlink_to_external_credentials_is_refused_without_reading(self):
        module = self.instrument()
        validate = self.capability(module, "validate_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            home = directory / "home"
            home.mkdir()
            outside = directory / "outside.json"
            outside.write_text("never read this")
            link = home / "credentials.json"
            link.symlink_to(outside)
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("credential bytes read")):
                with self.assertRaisesRegex(ValueError, "credential_outside_scratch_home"):
                    validate(home, [str(link)])
            link.unlink()
            os.link(outside, link)
            with self.assertRaisesRegex(ValueError, "credential_shared_inode"):
                validate(home, [str(link)])

    def test_outside_path_from_signin_refuses_before_measured_invocation(self):
        module = self.instrument()
        self.capability(module, "validate_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            outside = directory / "credentials.json"
            outside.write_text("keep foreign credential")
            def signin(home):
                return {"status": "ready", "credentialsPath": str(outside)}
            receipt = module.capture(directory / "refused.json", command=["/must/not/launch"],
                                     owner_signin=signin, timeout=20)
            self.assertEqual("credential_outside_scratch_home", receipt["failure"]["code"])
            self.assertEqual({}, receipt["invocation"])
            self.assertFalse(receipt["classification"]["live_conformance"])
            self.assertEqual("keep foreign credential", outside.read_text())

    def test_removal_failure_is_recorded_as_typed_absence(self):
        module = self.instrument()
        cleanup = self.capability(module, "remove_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            credential = home / "credentials.json"
            credential.write_text("fake")
            with mock.patch("shutil.rmtree", side_effect=PermissionError("denied")):
                evidence = cleanup(home)
            self.assertEqual("cannot-measure", evidence["state"])
            self.assertEqual("credential_removal_unproved", evidence["code"])
            self.assertTrue(credential.exists())

    def test_reported_credential_or_config_directory_is_refused(self):
        module = self.instrument()
        validate = self.capability(module, "validate_scratch_credentials")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            with self.assertRaisesRegex(ValueError, "credential_file_not_ordinary"):
                validate(home, [str(home)])

    def test_native_signin_uses_exact_scratch_flow_and_retains_only_path_metadata(self):
        module = self.instrument()
        signin = self.capability(module, "native_owner_signin")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            home = Path(temporary)
            ready = {"status": "ready", "credentialsPath": str(home / "credentials.json"),
                     "configPath": str(home / "provider_config.json"),
                     "user": {"email": "private@example.invalid"}, "secret": "never-save"}
            process = mock.Mock(returncode=0)
            process.communicate.return_value = (json.dumps(ready), None)
            process.poll.return_value = 0
            observed = {}
            def spawn(argv, **kwargs):
                observed.update(argv=argv, kwargs=kwargs, environment=dict(os.environ))
                return process
            with mock.patch.object(module.sys.stdin, "isatty", return_value=True),\
                    mock.patch.object(module.sys.stderr, "isatty", return_value=True),\
                    mock.patch.object(module.subprocess, "Popen", side_effect=spawn),\
                    mock.patch.dict(os.environ, {"ZCODE_DATA_BASE_DIR": "/foreign", "ZCODE_CREDENTIAL_SECRET": "foreign"}):
                with module.clean_environment(home):
                    result = signin(home)
            self.assertEqual({key: ready[key] for key in ("status", "credentialsPath", "configPath")}, result)
            self.assertNotIn("private@example.invalid", json.dumps(result))
            self.assertNotIn("never-save", json.dumps(result))
            expected_command = [str(Path(part).resolve()) for part in module.DEFAULT_COMMAND]
            self.assertEqual([*expected_command, "login", "zai", "--no-browser", "--json"], observed["argv"])
            self.assertEqual(home, observed["kwargs"]["cwd"])
            self.assertIsNone(observed["kwargs"]["stderr"], "authorization URL belongs only on owner's terminal")
            self.assertEqual(str(home), observed["environment"]["HOME"])
            self.assertNotIn("ZCODE_DATA_BASE_DIR", observed["environment"])
            self.assertNotIn("ZCODE_CREDENTIAL_SECRET", observed["environment"])
            process.stdout.close.assert_called_once()

    def test_native_signin_boots_packaged_provider_from_the_declared_entry(self):
        module = self.instrument()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            node, entry, config = self.packaged_cli(directory)
            home = directory / "home"
            home.mkdir()
            before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
            with mock.patch.object(module, "DEFAULT_COMMAND", (str(node), str(entry))),\
                    mock.patch.object(module.sys.stdin, "isatty", return_value=True),\
                    mock.patch.object(module.sys.stderr, "isatty", return_value=True):
                with module.clean_environment(home):
                    try:
                        result = module.native_owner_signin(home)
                    except module.ScratchCredentialRefusal as refusal:
                        self.fail("the declared packaged native entry must reach ready with its "
                                  "bundled provider configuration: " + refusal.code)
                    self.assertNotIn("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", os.environ,
                                     "the supported override belongs only to the child")
            self.assertEqual("ready", result["status"])
            self.assertEqual(str(home / "credentials.json"), result["credentialsPath"])
            self.assertEqual(before, {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()})

    def test_native_preflight_resolves_upgraded_entry_at_use_and_records_version(self):
        module = self.instrument()
        preflight = self.capability(module, "preflight_native_entry")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            declared_node = directory / "declared-node"
            declared_entry = directory / "declared-entry.cjs"
            declared = (str(declared_node), str(declared_entry))
            for version in ("3.16.3", "4.0.0"):
                with self.subTest(version=version):
                    install = directory / ("installed-" + version)
                    node, entry, config = self.packaged_cli(install, version)
                    for link, target in ((declared_node, node), (declared_entry, entry)):
                        if link.is_symlink():
                            link.unlink()
                        link.symlink_to(target)
                    home = directory / ("home-" + version)
                    home.mkdir()
                    with mock.patch.object(module, "DEFAULT_COMMAND", declared):
                        result = preflight(home)
                    self.assertEqual("ready", result["state"], result)
                    self.assertEqual(1, result["exit_code"],
                                     "invalid-provider refusal proves bootstrap, not sign-in")
                    native = result["native_entry"]
                    self.assertEqual(list(declared), native["declared_command"])
                    self.assertEqual([str(node), str(entry)], native["resolved_command"])
                    self.assertEqual(version, native["version"])
                    self.assertEqual(str(config), native["builtin_provider_config"])
                    self.assertFalse((home / "credentials.json").exists())
                    self.assertFalse((home / "provider_config.json").exists())

    def test_native_signin_failure_or_malformed_ready_never_returns_private_output(self):
        module = self.instrument()
        signin = self.capability(module, "native_owner_signin")
        cases = [(1, "private-output", "owner_signin_failed"),
                 (0, "private-output", "owner_signin_result_malformed"),
                 (0, "[]", "owner_signin_result_malformed"),
                 (0, '{"status":"pending"}', "owner_signin_not_ready"),
                 (0, '{"status":"ready","credentialsPath":false}', "owner_signin_result_malformed")]
        with mock.patch.object(module.sys.stdin, "isatty", return_value=True),\
                mock.patch.object(module.sys.stderr, "isatty", return_value=True):
            for code, output, reason in cases:
                with self.subTest(reason=reason, output=output):
                    process = mock.Mock(returncode=code)
                    process.communicate.return_value = (output, None)
                    process.poll.return_value = code
                    with mock.patch.object(module.subprocess, "Popen", return_value=process):
                        with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
                            signin(Path("/unused/home"))
                    process.stdout.close.assert_called_once()

    def test_native_signin_deadline_reaps_its_child_and_returns_typed_absence(self):
        module = self.instrument()
        signin = self.capability(module, "native_owner_signin")
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("native-login", 360)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("native-login", 5), -9]
        with mock.patch.object(module.sys.stdin, "isatty", return_value=True),\
                mock.patch.object(module.sys.stderr, "isatty", return_value=True),\
                mock.patch.object(module.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "^owner_signin_deadline$"):
                signin(Path("/unused/home"))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(2, process.wait.call_count)
        process.stdout.close.assert_called_once()
