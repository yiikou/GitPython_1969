# Copyright (C) 2008, 2009, 2016 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

from itertools import product
import re

import ddt

from git.exc import (
    InvalidGitRepositoryError,
    WorkTreeRepositoryUnsupported,
    NoSuchPathError,
    CommandError,
    GitCommandNotFound,
    GitCommandError,
    CheckoutError,
    CacheError,
    UnmergedEntriesError,
    HookExecutionError,
    RepositoryDirtyError,
)
from git.util import remove_password_if_present

from test.lib import TestBase


_cmd_argvs = (
    ("cmd",),
    ("θνιψοδε",),
    ("θνιψοδε", "normal", "argvs"),
    ("cmd", "ελληνικα", "args"),
    ("θνιψοδε", "κι", "αλλα", "strange", "args"),
    ("θνιψοδε", "κι", "αλλα", "non-unicode", "args"),
    (
        "git",
        "clone",
        "-v",
        "https://fakeuser:fakepassword1234@fakerepo.example.com/testrepo",
    ),
)
_causes_n_substrings = (
    (None, None),
    (7, "exit code(7)"),
    ("Some string", "'Some string'"),
    ("παλιο string", "'παλιο string'"),
    (Exception("An exc."), "Exception('An exc.')"),
    (Exception("Κακια exc."), "Exception('Κακια exc.')"),
    (object(), "<object object at "),
)

_streams_n_substrings = (
    None,
    "stream",
    "ομορφο stream",
)


@ddt.ddt
class TExc(TestBase):
    def test_ExceptionsHaveBaseClass(self):
        from git.exc import GitError

        self.assertIsInstance(GitError(), Exception)

        exception_classes = [
            InvalidGitRepositoryError,
            WorkTreeRepositoryUnsupported,
            NoSuchPathError,
            CommandError,
            GitCommandNotFound,
            GitCommandError,
            CheckoutError,
            CacheError,
            UnmergedEntriesError,
            HookExecutionError,
            RepositoryDirtyError,
        ]
        for ex_class in exception_classes:
            self.assertTrue(issubclass(ex_class, GitError))

    @ddt.data(*list(product(_cmd_argvs, _causes_n_substrings, _streams_n_substrings)))
    def test_CommandError_unicode(self, case):
        argv, (cause, subs), stream = case
        cls = CommandError
        c = cls(argv, cause)
        s = str(c)

        self.assertIsNotNone(c._msg)
        self.assertIn("  cmdline: ", s)

        for a in remove_password_if_present(argv):
            self.assertIn(a, s)

        if not cause:
            self.assertIn("failed!", s)
        else:
            self.assertIn(" failed due to:", s)

            if subs is not None:
                # Substrings (must) already contain opening `'`.
                subs = r"(?<!')%s(?!')" % re.escape(subs)
                self.assertRegex(s, subs)

        if not stream:
            c = cls(argv, cause)
            s = str(c)
            self.assertNotIn("  stdout:", s)
            self.assertNotIn("  stderr:", s)
        else:
            c = cls(argv, cause, stream)
            s = str(c)
            self.assertIn("  stderr:", s)
            self.assertIn(stream, s)

            c = cls(argv, cause, None, stream)
            s = str(c)
            self.assertIn("  stdout:", s)
            self.assertIn(stream, s)

            c = cls(argv, cause, stream, stream + "no2")
            s = str(c)
            self.assertIn("  stderr:", s)
            self.assertIn(stream, s)
            self.assertIn("  stdout:", s)
            self.assertIn(stream + "no2", s)

    @ddt.data(
        (["cmd1"], None),
        (["cmd1"], "some cause"),
        (["cmd1"], Exception()),
    )
    def test_GitCommandNotFound(self, init_args):
        argv, cause = init_args
        c = GitCommandNotFound(argv, cause)
        s = str(c)

        self.assertIn(argv[0], s)
        if cause:
            self.assertIn(" not found due to: ", s)
            self.assertIn(str(cause), s)
        else:
            self.assertIn(" not found!", s)

    @ddt.data(
        (["cmd1"], None),
        (["cmd1"], "some cause"),
        (["cmd1", "https://fakeuser@fakerepo.example.com/testrepo"], Exception()),
    )
    def test_GitCommandError(self, init_args):
        argv, cause = init_args
        c = GitCommandError(argv, cause)
        s = str(c)

        for arg in remove_password_if_present(argv):
            self.assertIn(arg, s)
        if cause:
            self.assertIn(" failed due to: ", s)
            self.assertIn(str(cause), s)
        else:
            self.assertIn(" failed!", s)

    @ddt.data(
        (["cmd1"], None),
        (["cmd1"], "some cause"),
        (["cmd1"], Exception()),
    )
    def test_HookExecutionError(self, init_args):
        argv, cause = init_args
        c = HookExecutionError(argv, cause)
        s = str(c)

        self.assertIn(argv[0], s)
        if cause:
            self.assertTrue(s.startswith("Hook("), s)
            self.assertIn(str(cause), s)
        else:
            self.assertIn(" failed!", s)
