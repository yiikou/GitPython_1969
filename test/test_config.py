# Copyright (C) 2008, 2009 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

import glob
import io
import os
import os.path as osp
import sys
from unittest import mock

import pytest

from git import GitConfigParser
from git.config import _OMD, cp
from git.util import rmfile

from test.lib import SkipTest, TestCase, fixture_path, with_rw_directory

_tc_lock_fpaths = osp.join(osp.dirname(__file__), "fixtures/*.lock")


def _rm_lock_files():
    for lfp in glob.glob(_tc_lock_fpaths):
        rmfile(lfp)


class TestBase(TestCase):
    def setUp(self):
        _rm_lock_files()

    def tearDown(self):
        for lfp in glob.glob(_tc_lock_fpaths):
            if osp.isfile(lfp):
                raise AssertionError("Previous TC left hanging git-lock file: {}".format(lfp))

    def _to_memcache(self, file_path):
        with open(file_path, "rb") as fp:
            sio = io.BytesIO(fp.read())
        sio.name = file_path
        return sio

    def test_read_write(self):
        # The writer must create the exact same file as the one read before.
        for filename in ("git_config", "git_config_global"):
            file_obj = self._to_memcache(fixture_path(filename))
            with GitConfigParser(file_obj, read_only=False) as w_config:
                w_config.read()  # Enforce reading.
                assert w_config._sections
                w_config.write()  # Enforce writing.

                # We stripped lines when reading, so the results differ.
                assert file_obj.getvalue()
                self.assertEqual(
                    file_obj.getvalue(),
                    self._to_memcache(fixture_path(filename)).getvalue(),
                )

                # Creating an additional config writer must fail due to exclusive
                # access.
                with self.assertRaises(IOError):
                    GitConfigParser(file_obj, read_only=False)

                # Should still have a lock and be able to make changes.
                assert w_config._lock._has_lock()

                # Changes should be written right away.
                sname = "my_section"
                oname = "mykey"
                val = "myvalue"
                w_config.add_section(sname)
                assert w_config.has_section(sname)
                w_config.set(sname, oname, val)
                assert w_config.has_option(sname, oname)
                assert w_config.get(sname, oname) == val

                sname_new = "new_section"
                oname_new = "new_key"
                ival = 10
                w_config.set_value(sname_new, oname_new, ival)
                assert w_config.get_value(sname_new, oname_new) == ival

                file_obj.seek(0)
                r_config = GitConfigParser(file_obj, read_only=True)
                assert r_config.has_section(sname)
                assert r_config.has_option(sname, oname)
                assert r_config.get(sname, oname) == val
        # END for each filename

    def test_includes_order(self):
        with GitConfigParser(list(map(fixture_path, ("git_config", "git_config_global")))) as r_config:
            r_config.read()  # Enforce reading.
            # Simple inclusions, again checking them taking precedence.
            assert r_config.get_value("sec", "var0") == "value0_included"
            # This one should take the git_config_global value since included values
            # must be considered as soon as they get them.
            assert r_config.get_value("diff", "tool") == "meld"
            try:
                # FIXME: Split this assertion out somehow and mark it xfail (or fix it).
                assert r_config.get_value("sec", "var1") == "value1_main"
            except AssertionError as e:
                raise SkipTest("Known failure -- included values are not in effect right away") from e

    @with_rw_directory
    def test_lock_reentry(self, rw_dir):
        fpl = osp.join(rw_dir, "l")
        gcp = GitConfigParser(fpl, read_only=False)
        with gcp as cw:
            cw.set_value("include", "some_value", "a")
        # Entering again locks the file again...
        with gcp as cw:
            cw.set_value("include", "some_other_value", "b")
            # ...so creating an additional config writer must fail due to exclusive
            # access.
            with self.assertRaises(IOError):
                GitConfigParser(fpl, read_only=False)
        # but work when the lock is removed
        with GitConfigParser(fpl, read_only=False):
            assert osp.exists(fpl)
            # Reentering with an existing lock must fail due to exclusive access.
            with self.assertRaises(IOError):
                gcp.__enter__()

    def test_multi_line_config(self):
        file_obj = self._to_memcache(fixture_path("git_config_with_comments"))
        with GitConfigParser(file_obj, read_only=False) as config:
            ev = "ruby -e '\n"
            ev += "		system %(git), %(merge-file), %(--marker-size=%L), %(%A), %(%O), %(%B)\n"
            ev += "		b = File.read(%(%A))\n"
            ev += "		b.sub!(/^<+ .*\\nActiveRecord::Schema\\.define.:version => (\\d+). do\\n=+\\nActiveRecord::Schema\\."  # noqa: E501
            ev += "define.:version => (\\d+). do\\n>+ .*/) do\n"
            ev += "		  %(ActiveRecord::Schema.define(:version => #{[$1, $2].max}) do)\n"
            ev += "		end\n"
            ev += "		File.open(%(%A), %(w)) {|f| f.write(b)}\n"
            ev += "		exit 1 if b.include?(%(<)*%L)'"
            self.assertEqual(config.get('merge "railsschema"', "driver"), ev)
            self.assertEqual(
                config.get("alias", "lg"),
                "log --graph --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr)%Creset'"
                " --abbrev-commit --date=relative",
            )
            self.assertEqual(len(config.sections()), 23)

    def test_config_value_with_trailing_new_line(self):
        config_content = b'[section-header]\nkey:"value\n"'
        config_file = io.BytesIO(config_content)
        config_file.name = "multiline_value.config"

        git_config = GitConfigParser(config_file)
        git_config.read()  # This should not throw an exception

    def test_base(self):
        path_repo = fixture_path("git_config")
        path_global = fixture_path("git_config_global")
        r_config = GitConfigParser([path_repo, path_global], read_only=True)
        assert r_config.read_only
        num_sections = 0
        num_options = 0

        # Test reader methods.
        assert r_config._is_initialized is False
        for section in r_config.sections():
            num_sections += 1
            for option in r_config.options(section):
                num_options += 1
                val = r_config.get(section, option)
                val_typed = r_config.get_value(section, option)
                assert isinstance(val_typed, (bool, int, float, str))
                assert val
                assert "\n" not in option
                assert "\n" not in val

                # Writing must fail.
                with self.assertRaises(IOError):
                    r_config.set(section, option, None)
                with self.assertRaises(IOError):
                    r_config.remove_option(section, option)
            # END for each option
            with self.assertRaises(IOError):
                r_config.remove_section(section)
        # END for each section
        assert num_sections and num_options
        assert r_config._is_initialized is True

        # Get value which doesn't exist, with default.
        default = "my default value"
        assert r_config.get_value("doesnt", "exist", default) == default

        # It raises if there is no default though.
        with self.assertRaises(cp.NoSectionError):
            r_config.get_value("doesnt", "exist")

    @with_rw_directory
    def test_config_include(self, rw_dir):
        def write_test_value(cw, value):
            cw.set_value(value, "value", value)

        def check_test_value(cr, value):
            assert cr.get_value(value, "value") == value

        # PREPARE CONFIG FILE A
        fpa = osp.join(rw_dir, "a")
        with GitConfigParser(fpa, read_only=False) as cw:
            write_test_value(cw, "a")

            fpb = osp.join(rw_dir, "b")
            fpc = osp.join(rw_dir, "c")
            cw.set_value("include", "relative_path_b", "b")
            cw.set_value("include", "doesntexist", "foobar")
            cw.set_value("include", "relative_cycle_a_a", "a")
            cw.set_value("include", "absolute_cycle_a_a", fpa)
        assert osp.exists(fpa)

        # PREPARE CONFIG FILE B
        with GitConfigParser(fpb, read_only=False) as cw:
            write_test_value(cw, "b")
            cw.set_value("include", "relative_cycle_b_a", "a")
            cw.set_value("include", "absolute_cycle_b_a", fpa)
            cw.set_value("include", "relative_path_c", "c")
            cw.set_value("include", "absolute_path_c", fpc)

        # PREPARE CONFIG FILE C
        with GitConfigParser(fpc, read_only=False) as cw:
            write_test_value(cw, "c")

        with GitConfigParser(fpa, read_only=True) as cr:
            for tv in ("a", "b", "c"):
                check_test_value(cr, tv)
            # END for each test to verify
            assert len(cr.items("include")) == 8, "Expected all include sections to be merged"

        # Test writable config writers - assure write-back doesn't involve includes.
        with GitConfigParser(fpa, read_only=False, merge_includes=True) as cw:
            tv = "x"
            write_test_value(cw, tv)

        with GitConfigParser(fpa, read_only=True) as cr:
            with self.assertRaises(cp.NoSectionError):
                check_test_value(cr, tv)

        # But can make it skip includes altogether, and thus allow write-backs.
        with GitConfigParser(fpa, read_only=False, merge_includes=False) as cw:
            write_test_value(cw, tv)

        with GitConfigParser(fpa, read_only=True) as cr:
            check_test_value(cr, tv)

    @pytest.mark.xfail(
        sys.platform == "win32",
        reason='Second config._has_includes() assertion fails (for "config is included if path is matching git_dir")',
        raises=AssertionError,
    )
    @with_rw_directory
    def test_conditional_includes_from_git_dir(self, rw_dir):
        # Initiate repository path.
        git_dir = osp.join(rw_dir, "target1", "repo1")
        os.makedirs(git_dir)

        # Initiate mocked repository.
        repo = mock.Mock(git_dir=git_dir)

        # Initiate config files.
        path1 = osp.join(rw_dir, "config1")
        path2 = osp.join(rw_dir, "config2")
        template = '[includeIf "{}:{}"]\n    path={}\n'

        with open(path1, "w") as stream:
            stream.write(template.format("gitdir", git_dir, path2))

        # Ensure that config is ignored if no repo is set.
        with GitConfigParser(path1) as config:
            assert not config._has_includes()
            assert config._included_paths() == []

        # Ensure that config is included if path is matching git_dir.
        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

        # Ensure that config is ignored if case is incorrect.
        with open(path1, "w") as stream:
            stream.write(template.format("gitdir", git_dir.upper(), path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert not config._has_includes()
            assert config._included_paths() == []

        # Ensure that config is included if case is ignored.
        with open(path1, "w") as stream:
            stream.write(template.format("gitdir/i", git_dir.upper(), path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

        # Ensure that config is included with path using glob pattern.
        with open(path1, "w") as stream:
            stream.write(template.format("gitdir", "**/repo1", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

        # Ensure that config is ignored if path is not matching git_dir.
        with open(path1, "w") as stream:
            stream.write(template.format("gitdir", "incorrect", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert not config._has_includes()
            assert config._included_paths() == []

        # Ensure that config is included if path in hierarchy.
        with open(path1, "w") as stream:
            stream.write(template.format("gitdir", "target1/", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

    @with_rw_directory
    def test_conditional_includes_from_branch_name(self, rw_dir):
        # Initiate mocked branch.
        branch = mock.Mock()
        type(branch).name = mock.PropertyMock(return_value="/foo/branch")

        # Initiate mocked repository.
        repo = mock.Mock(active_branch=branch)

        # Initiate config files.
        path1 = osp.join(rw_dir, "config1")
        path2 = osp.join(rw_dir, "config2")
        template = '[includeIf "onbranch:{}"]\n    path={}\n'

        # Ensure that config is included is branch is correct.
        with open(path1, "w") as stream:
            stream.write(template.format("/foo/branch", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

        # Ensure that config is included is branch is incorrect.
        with open(path1, "w") as stream:
            stream.write(template.format("incorrect", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert not config._has_includes()
            assert config._included_paths() == []

        # Ensure that config is included with branch using glob pattern.
        with open(path1, "w") as stream:
            stream.write(template.format("/foo/**", path2))

        with GitConfigParser(path1, repo=repo) as config:
            assert config._has_includes()
            assert config._included_paths() == [("path", path2)]

    @with_rw_directory
    def test_conditional_includes_from_branch_name_error(self, rw_dir):
        # Initiate mocked repository to raise an error if HEAD is detached.
        repo = mock.Mock()
        type(repo).active_branch = mock.PropertyMock(side_effect=TypeError)

        # Initiate config file.
        path1 = osp.join(rw_dir, "config1")

        # Ensure that config is ignored when active branch cannot be found.
        with open(path1, "w") as stream:
            stream.write('[includeIf "onbranch:foo"]\n    path=/path\n')

        with GitConfigParser(path1, repo=repo) as config:
            assert not config._has_includes()
            assert config._included_paths() == []

    def test_rename(self):
        file_obj = self._to_memcache(fixture_path("git_config"))
        with GitConfigParser(file_obj, read_only=False, merge_includes=False) as cw:
            with self.assertRaises(ValueError):
                cw.rename_section("doesntexist", "foo")
            with self.assertRaises(ValueError):
                cw.rename_section("core", "include")

            nn = "bee"
            assert cw.rename_section("core", nn) is cw
            assert not cw.has_section("core")
            assert len(cw.items(nn)) == 4

    def test_complex_aliases(self):
        file_obj = self._to_memcache(fixture_path(".gitconfig"))
        with GitConfigParser(file_obj, read_only=False) as w_config:
            self.assertEqual(
                w_config.get("alias", "rbi"),
                '"!g() { git rebase -i origin/${1:-master} ; } ; g"',
            )
        self.assertEqual(
            file_obj.getvalue(),
            self._to_memcache(fixture_path(".gitconfig")).getvalue(),
        )

    def test_empty_config_value(self):
        cr = GitConfigParser(fixture_path("git_config_with_empty_value"), read_only=True)

        assert cr.get_value("core", "filemode"), "Should read keys with values"

        with self.assertRaises(cp.NoOptionError):
            cr.get_value("color", "ui")

    def test_get_values_works_without_requiring_any_other_calls_first(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        cr = GitConfigParser(file_obj, read_only=True)
        self.assertEqual(cr.get_values("section0", "option0"), ["value0"])
        file_obj.seek(0)
        cr = GitConfigParser(file_obj, read_only=True)
        self.assertEqual(cr.get_values("section1", "option1"), ["value1a", "value1b"])
        file_obj.seek(0)
        cr = GitConfigParser(file_obj, read_only=True)
        self.assertEqual(cr.get_values("section1", "other_option1"), ["other_value1"])

    def test_multiple_values(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        with GitConfigParser(file_obj, read_only=False) as cw:
            self.assertEqual(cw.get("section0", "option0"), "value0")
            self.assertEqual(cw.get_values("section0", "option0"), ["value0"])
            self.assertEqual(cw.items("section0"), [("option0", "value0")])

            # Where there are multiple values, "get" returns the last.
            self.assertEqual(cw.get("section1", "option1"), "value1b")
            self.assertEqual(cw.get_values("section1", "option1"), ["value1a", "value1b"])
            self.assertEqual(
                cw.items("section1"),
                [("option1", "value1b"), ("other_option1", "other_value1")],
            )
            self.assertEqual(
                cw.items_all("section1"),
                [
                    ("option1", ["value1a", "value1b"]),
                    ("other_option1", ["other_value1"]),
                ],
            )
            with self.assertRaises(KeyError):
                cw.get_values("section1", "missing")

            self.assertEqual(cw.get_values("section1", "missing", 1), [1])
            self.assertEqual(cw.get_values("section1", "missing", "s"), ["s"])

    def test_multiple_values_rename(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        with GitConfigParser(file_obj, read_only=False) as cw:
            cw.rename_section("section1", "section2")
            cw.write()
            file_obj.seek(0)
            cr = GitConfigParser(file_obj, read_only=True)
            self.assertEqual(cr.get_value("section2", "option1"), "value1b")
            self.assertEqual(cr.get_values("section2", "option1"), ["value1a", "value1b"])
            self.assertEqual(
                cr.items("section2"),
                [("option1", "value1b"), ("other_option1", "other_value1")],
            )
            self.assertEqual(
                cr.items_all("section2"),
                [
                    ("option1", ["value1a", "value1b"]),
                    ("other_option1", ["other_value1"]),
                ],
            )

    def test_multiple_to_single(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        with GitConfigParser(file_obj, read_only=False) as cw:
            cw.set_value("section1", "option1", "value1c")

            cw.write()
            file_obj.seek(0)
            cr = GitConfigParser(file_obj, read_only=True)
            self.assertEqual(cr.get_value("section1", "option1"), "value1c")
            self.assertEqual(cr.get_values("section1", "option1"), ["value1c"])
            self.assertEqual(
                cr.items("section1"),
                [("option1", "value1c"), ("other_option1", "other_value1")],
            )
            self.assertEqual(
                cr.items_all("section1"),
                [("option1", ["value1c"]), ("other_option1", ["other_value1"])],
            )

    def test_single_to_multiple(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        with GitConfigParser(file_obj, read_only=False) as cw:
            cw.add_value("section1", "other_option1", "other_value1a")

            cw.write()
            file_obj.seek(0)
            cr = GitConfigParser(file_obj, read_only=True)
            self.assertEqual(cr.get_value("section1", "option1"), "value1b")
            self.assertEqual(cr.get_values("section1", "option1"), ["value1a", "value1b"])
            self.assertEqual(cr.get_value("section1", "other_option1"), "other_value1a")
            self.assertEqual(
                cr.get_values("section1", "other_option1"),
                ["other_value1", "other_value1a"],
            )
            self.assertEqual(
                cr.items("section1"),
                [("option1", "value1b"), ("other_option1", "other_value1a")],
            )
            self.assertEqual(
                cr.items_all("section1"),
                [
                    ("option1", ["value1a", "value1b"]),
                    ("other_option1", ["other_value1", "other_value1a"]),
                ],
            )

    def test_add_to_multiple(self):
        file_obj = self._to_memcache(fixture_path("git_config_multiple"))
        with GitConfigParser(file_obj, read_only=False) as cw:
            cw.add_value("section1", "option1", "value1c")
            cw.write()
            file_obj.seek(0)
            cr = GitConfigParser(file_obj, read_only=True)
            self.assertEqual(cr.get_value("section1", "option1"), "value1c")
            self.assertEqual(cr.get_values("section1", "option1"), ["value1a", "value1b", "value1c"])
            self.assertEqual(
                cr.items("section1"),
                [("option1", "value1c"), ("other_option1", "other_value1")],
            )
            self.assertEqual(
                cr.items_all("section1"),
                [
                    ("option1", ["value1a", "value1b", "value1c"]),
                    ("other_option1", ["other_value1"]),
                ],
            )

    def test_setlast(self):
        # Test directly, not covered by higher-level tests.
        omd = _OMD()
        omd.setlast("key", "value1")
        self.assertEqual(omd["key"], "value1")
        self.assertEqual(omd.getall("key"), ["value1"])
        omd.setlast("key", "value2")
        self.assertEqual(omd["key"], "value2")
        self.assertEqual(omd.getall("key"), ["value2"])
