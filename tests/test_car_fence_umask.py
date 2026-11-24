"""Real umask-002 coordinates with explicit private/shared account records."""
from contextlib import ExitStack
import grp
import os
import pwd
import shlex
import stat
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from floati.car_fence import car_acceptance_guard
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.temp_roots import REAL_TEMP_ROOT


def private_group_records(case, gid):
    """Model Ubuntu's user-private group; never depend on macOS's shared staff."""
    owner = pwd.struct_passwd(("fixture-owner", "x", os.geteuid(), gid,
                               "", "/unused", "/bin/false"))
    group = grp.struct_group(("fixture-private", "x", gid, []))
    stack = ExitStack()
    case.addCleanup(stack.close)
    database = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
    case.addCleanup(database.cleanup)
    case.nsswitch = Path(database.name) / "nsswitch.conf"
    case.nsswitch.write_bytes(b"passwd: files systemd\ngroup: files systemd\n")
    case.group_configuration = Path(database.name) / "group.conf"
    case.group_configuration.write_bytes(b"# no supplementary group grants\n\n")
    stack.enter_context(mock.patch("floati.car_fence._NSSWITCH_PATH",
                                   case.nsswitch, create=True))
    stack.enter_context(mock.patch("floati.car_fence._PAM_GROUP_CONFIGURATION_PATH",
                                   case.group_configuration, create=True))
    stack.enter_context(mock.patch("floati.car_fence.sys", create=True,
                                   new=mock.Mock(platform="linux")))
    stack.enter_context(mock.patch("floati.car_fence.pwd", create=True,
        new=mock.Mock(getpwuid=mock.Mock(return_value=owner),
                      getpwall=mock.Mock(return_value=[owner]))))
    stack.enter_context(mock.patch("floati.car_fence.grp", create=True,
        new=mock.Mock(getgrgid=mock.Mock(return_value=group))))
    return owner, group


class CarFenceUmaskTests(unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        # Existing user-created home: preserve the original private-group property.
        (self.home / "fleet").mkdir()
        self.root = FloatiRoot.open_direct_home(self.home / "fleet", create=True)
        self.work = self.root.tenant_home / "work"
        self.lock = self.work / "car-acceptance.lock"
        self.owner, self.group = private_group_records(self,
            self.root.tenant_home.stat().st_gid)

    def create_coordinates(self):
        self.work.mkdir()
        self.lock.touch()
        self.assertEqual(0o775, stat.S_IMODE(self.root.tenant_home.stat().st_mode))
        self.assertEqual(0o775, stat.S_IMODE(self.work.stat().st_mode))
        self.assertEqual(0o664, stat.S_IMODE(self.lock.stat().st_mode))

    def assert_refused(self, *, observe=False):
        with self.assertRaises(ProtocolRefusal) as caught:
            with car_acceptance_guard(self.root, observe=observe):
                self.fail("unsafe car coordinate admitted")
        self.assertEqual("car_acceptance_fence_invalid", caught.exception.code)
        return caught.exception

    def test_directory_service_sources_refuse_even_with_partial_owner_only_records(self):
        self.create_coordinates()
        for source in ("sss", "ldap", "winbind", "compat", "db", "unknown"):
            for entry in ("passwd", "group"):
                with self.subTest(source=source, entry=entry):
                    self.nsswitch.write_bytes((
                        b"passwd: files systemd\ngroup: files systemd\n").replace(
                            (entry + ": files systemd").encode("ascii"),
                            (entry + ": files systemd " + source).encode("ascii")))
                    self.assert_refused()

    def test_local_account_sources_allow_private_group(self):
        self.create_coordinates()
        for sources in ("files", "systemd", "files systemd", "systemd files"):
            with self.subTest(sources=sources):
                self.nsswitch.write_bytes((
                    "# local accounts\npasswd: " + sources + " # comment\n" +
                    "group:\t" + sources + "\nhosts: files dns\n").encode("ascii"))
                with car_acceptance_guard(self.root):
                    pass

    def test_remote_or_malformed_initgroups_refuses_partial_local_membership(self):
        self.create_coordinates()
        local = b"passwd: files systemd\ngroup: files systemd\n"
        for entry in (b"initgroups: sss\n", b"initgroups: ldap\n",
                      b"initgroups: files systemd sss\n", b"initgroups:\n",
                      b"initgroups files\n", b"initgroups: files: systemd\n",
                      b"initgroups: files [SUCCESS=return]\n",
                      b"initgroups: files\ninitgroups: systemd\n",
                      b"initgroups: files\ninitgroups sss\n",
                      b"initgroups: files\rsss\n",
                      b"initgroups: files\vldap\n"):
            with self.subTest(entry=entry):
                self.nsswitch.write_bytes(local + entry)
                self.assert_refused()

    def test_optional_local_initgroups_preserves_primary_private_group(self):
        self.create_coordinates()
        local = b"passwd: files systemd\ngroup: files systemd\n"
        for entry in (b"", b"initgroups: files\n", b"initgroups: systemd\n",
                      b"initgroups: files systemd\n",
                      b"initgroups:\tsystemd files # local only\n"):
            with self.subTest(entry=entry):
                self.nsswitch.write_bytes(local + entry)
                for observe in (False, True):
                    with car_acceptance_guard(self.root, observe=observe):
                        pass

    def test_effective_default_group_rules_refuse_without_a_scanned_pam_load(self):
        self.create_coordinates()
        # No PAM stack testimony is supplied: a load elsewhere must not let
        # the effective default escape account-database testimony.
        self.group_configuration.write_bytes(b"*;*;*;Al0000-2400;shared\n")
        for observe in (False, True):
            with self.subTest(observe=observe):
                self.assert_refused(observe=observe)

    def test_stock_ubuntu_empty_default_group_rules_accept(self):
        self.create_coordinates()
        for configuration in (b"", b"\n\t\n # no grants\n# documentation\n",
                              b" \t\r\n\t# no grants\r\n"):
            with self.subTest(configuration=configuration):
                self.group_configuration.write_bytes(configuration)
                for observe in (False, True):
                    with car_acceptance_guard(self.root, observe=observe):
                        pass

    def test_unicode_whitespace_cannot_hide_effective_default_group_rules(self):
        self.create_coordinates()
        whitespace_cases = tuple(
            (f"whitespace-{index}", whitespace)
            for index, whitespace in enumerate(
                (b"\v", b"\f", "\u00a0".encode("utf-8"), "\u2028".encode("utf-8")))
        )
        damage_cases = tuple((case, None) for case in
                             ("missing", "symlink", "directory", "fifo", "unreadable"))
        target = self.group_configuration.with_name("empty.conf")
        target.write_bytes(b"# no grants\n")
        original_open = os.open

        def unreadable(path, *args, **kwargs):
            if path == self.group_configuration:
                raise PermissionError("unreadable group configuration")
            return original_open(path, *args, **kwargs)

        for case, whitespace in whitespace_cases + damage_cases:
            for observe in (False, True):
                with self.subTest(case=case, observe=observe), ExitStack() as stack:
                    self.group_configuration.write_bytes(b"# no grants\n")
                    if whitespace is not None:
                        self.group_configuration.write_bytes(whitespace + b"# no grants\n")
                    elif case == "unreadable":
                        stack.enter_context(mock.patch("floati.car_fence.os.open",
                                                       side_effect=unreadable))
                    else:
                        self.group_configuration.unlink()
                        if case == "symlink":
                            self.group_configuration.symlink_to(target)
                        elif case == "directory":
                            self.group_configuration.mkdir()
                        elif case == "fifo":
                            os.mkfifo(self.group_configuration)
                    try:
                        self.assert_refused(observe=observe)
                    finally:
                        if case == "directory":
                            self.group_configuration.rmdir()
                        else:
                            self.group_configuration.unlink(missing_ok=True)

    def test_missing_malformed_or_unreadable_database_refuses(self):
        self.create_coordinates()
        for data in (b"", b"passwd: files\n", b"group: files\n",
                     b"passwd:\ngroup: files\n", b"passwd files\ngroup: files\n",
                     b"passwd: files\ngroup: \xff\n",
                     b"passwd: files\npasswd: files\ngroup: files\n",
                     b"passwd: files\ngroup: files\ngroup: sss\n",
                     b"passwd: files\ngroup: files\npasswd sss\n",
                     b"passwd: files [SUCCESS=return]\ngroup: files\n",
                     b"passwd: files [sss\ngroup: files\n"):
            with self.subTest(data=data):
                self.nsswitch.write_bytes(data)
                self.assert_refused()
        self.nsswitch.unlink()
        self.assert_refused()
        self.nsswitch.mkdir()
        self.assert_refused()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("unreadable")):
            self.assert_refused()

    def test_non_lf_whitespace_cannot_hide_a_remote_source(self):
        self.create_coordinates()
        for separator in (b"\r", b"\v", b"\f", "\u2028".encode("utf-8")):
            with self.subTest(separator=separator):
                self.nsswitch.write_bytes(b"passwd: files" + separator + b"sss\ngroup: files\n")
                self.assert_refused()

    def test_non_linux_platforms_refuse_group_write(self):
        self.create_coordinates()
        for platform in ("darwin", "win32", "freebsd14"):
            with self.subTest(platform=platform):
                with mock.patch("floati.car_fence.sys", create=True,
                                new=mock.Mock(platform=platform)):
                    self.assert_refused()

    def test_unsafe_existing_tenant_names_remedy_without_changing_mode(self):
        self.nsswitch.write_bytes(b"passwd: files sss\ngroup: files\n")
        for mode in (0o775, 0o777):
            with self.subTest(mode=oct(mode)):
                self.root.tenant_home.chmod(mode)
                root = FloatiRoot.open_direct_home(self.root.tenant_home, create=True)
                self.assertEqual(mode, stat.S_IMODE(root.tenant_home.stat().st_mode))
                failure = self.assert_refused()
                self.assertIn(str(root.tenant_home), failure.detail)
                self.assertEqual("chmod 700 " + shlex.quote(str(root.tenant_home)), failure.remedy)
                self.assertEqual(mode, stat.S_IMODE(root.tenant_home.stat().st_mode))
                self.assertFalse(self.work.exists())

    def test_floati_created_homes_are_private_under_umask_002(self):
        for root in (FloatiRoot.open_direct_home(self.home / "created", create=True),
                     FloatiRoot.open(self.home / "namespace", "created")):
            with self.subTest(home=root.tenant_home):
                self.assertEqual(0o700, stat.S_IMODE(root.tenant_home.stat().st_mode))
                # Explicit modes work even without enumerable accounts.
                with mock.patch("floati.car_fence.sys", create=True,
                                new=mock.Mock(platform="darwin")):
                    with car_acceptance_guard(root):
                        pass

    def test_private_primary_group_accepts_real_umask_002_coordinates(self):
        self.create_coordinates()
        before = [path.stat() for path in (self.root.tenant_home, self.work, self.lock)]
        for observe in (False, True):
            with car_acceptance_guard(self.root, observe=observe) as present:
                self.assertTrue(present)
        self.assertEqual([(s.st_ino, s.st_mode) for s in before],
            [(path.stat().st_ino, path.stat().st_mode)
             for path in (self.root.tenant_home, self.work, self.lock)])

    def test_private_tenant_allows_floati_to_create_private_work_and_lock(self):
        with car_acceptance_guard(self.root):
            pass
        self.assertEqual(0o700, stat.S_IMODE(self.work.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(self.lock.stat().st_mode))

    def test_world_write_refuses_at_each_coordinate_even_with_private_group(self):
        self.create_coordinates()
        for path in (self.root.tenant_home, self.work, self.lock):
            mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(coordinate=path.name):
                path.chmod(mode | 0o002)
                try:
                    self.assert_refused()
                finally:
                    path.chmod(mode)

    def test_supplementary_foreign_group_member_refuses(self):
        self.create_coordinates()
        shared = grp.struct_group((self.group.gr_name, "x", self.group.gr_gid,
                                   [self.owner.pw_name, "another-user"]))
        with mock.patch("floati.car_fence.grp.getgrgid", return_value=shared):
            self.assert_refused()

    def test_other_users_primary_group_refuses_even_with_empty_member_list(self):
        self.create_coordinates()
        other = pwd.struct_passwd(("another-user", "x", os.geteuid() + 1,
                                  self.owner.pw_gid, "", "/unused", "/bin/false"))
        with mock.patch("floati.car_fence.pwd.getpwall", return_value=[self.owner, other]):
            self.assert_refused()

    def test_unresolvable_group_or_owner_refuses(self):
        self.create_coordinates()
        for lookup in ("floati.car_fence.grp.getgrgid", "floati.car_fence.pwd.getpwuid",
                       "floati.car_fence.pwd.getpwall"):
            for failure in (KeyError("unresolved"), OSError("unavailable")):
                with self.subTest(lookup=lookup, failure=type(failure).__name__):
                    with mock.patch(lookup, side_effect=failure):
                        self.assert_refused()

    def test_missing_owner_in_account_enumeration_refuses(self):
        self.create_coordinates()
        with mock.patch("floati.car_fence.pwd.getpwall", return_value=[]):
            self.assert_refused()

    def test_inconsistent_account_identity_refuses(self):
        self.create_coordinates()
        candidates = (
            ("floati.car_fence.grp.getgrgid", grp.struct_group(
                ("wrong-group", "x", self.group.gr_gid + 1, []))),
            ("floati.car_fence.pwd.getpwuid", pwd.struct_passwd(
                (self.owner.pw_name, "x", os.geteuid() + 1, self.owner.pw_gid,
                 "", "/unused", "/bin/false"))),
            ("floati.car_fence.pwd.getpwall", [pwd.struct_passwd(
                ("wrong-name", "x", os.geteuid(), self.owner.pw_gid,
                 "", "/unused", "/bin/false"))]),
        )
        for lookup, value in candidates:
            with self.subTest(lookup=lookup):
                with mock.patch(lookup, return_value=value):
                    self.assert_refused()

    def test_owner_only_secondary_group_refuses_at_each_coordinate(self):
        self.create_coordinates()
        owner = pwd.struct_passwd((self.owner.pw_name, "x", os.geteuid(),
            self.group.gr_gid + 1, "", "/unused", "/bin/false"))
        self.group.gr_mem.append(owner.pw_name)
        with mock.patch("floati.car_fence.pwd.getpwuid", return_value=owner),\
             mock.patch("floati.car_fence.pwd.getpwall", return_value=[owner]):
            coordinates = (self.root.tenant_home, self.work, self.lock)
            for path in coordinates:
                path.chmod(0o600 if path == self.lock else 0o700)
            for path in coordinates:
                with self.subTest(coordinate=path.name):
                    mode = stat.S_IMODE(path.stat().st_mode)
                    path.chmod(mode | 0o020)
                    try:
                        self.assert_refused()
                    finally:
                        path.chmod(mode)

    def test_unestablished_owner_membership_refuses(self):
        self.create_coordinates()
        owner = pwd.struct_passwd((self.owner.pw_name, "x", os.geteuid(),
            self.group.gr_gid + 1, "", "/unused", "/bin/false"))
        with mock.patch("floati.car_fence.pwd.getpwuid", return_value=owner),\
             mock.patch("floati.car_fence.pwd.getpwall", return_value=[owner]):
            self.assert_refused()

    def test_private_modes_need_no_account_lookup(self):
        self.root.tenant_home.chmod(0o700)
        with mock.patch("floati.car_fence.grp.getgrgid", side_effect=AssertionError),\
             mock.patch("floati.car_fence.pwd.getpwuid", side_effect=AssertionError),\
             mock.patch("floati.car_fence.pwd.getpwall", side_effect=AssertionError):
            with car_acceptance_guard(self.root):
                pass

    def test_foreign_owner_refuses_even_when_group_is_private(self):
        self.create_coordinates()
        original = os.fstat
        tenant = self.root.tenant_home.stat()

        def foreign(descriptor):
            info = original(descriptor)
            if (info.st_dev, info.st_ino) == (tenant.st_dev, tenant.st_ino):
                values = list(info)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return info

        with mock.patch("floati.car_fence.os.fstat", side_effect=foreign):
            self.assert_refused()

    def test_symlink_and_fifo_refuse_even_when_group_is_private(self):
        self.work.mkdir()
        target = self.work / "target"
        target.touch()
        self.lock.symlink_to(target)
        self.assert_refused()
        self.lock.unlink()
        os.mkfifo(self.lock, 0o660)
        self.assert_refused()

    def test_group_becoming_shared_during_operation_is_refused_on_exit(self):
        self.create_coordinates()
        with self.assertRaises(ProtocolRefusal) as caught:
            with car_acceptance_guard(self.root):
                self.group.gr_mem.append("another-user")
        self.assertEqual("car_acceptance_fence_invalid", caught.exception.code)


class AdhocCarUmaskTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)
        probe = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(probe.cleanup)
        private_group_records(self, os.stat(probe.name).st_gid)
        super().setUp()

    def test_adhoc_profile_accepts_private_group_without_changing_umask(self):
        self.assertEqual(0o700, stat.S_IMODE(self.root.tenant_home.stat().st_mode))
        self.assertEqual("temporary", self.profile()["node_id"])
        self.assert_no_provider_effects()
