# Copyright (C) 2008, 2009 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

import copy
from datetime import datetime
from io import BytesIO
import os.path as osp
import re
import sys
import time
from unittest.mock import Mock

from gitdb import IStream

from git import Actor, Commit, Repo
from git.objects.util import tzoffset, utc
from git.repo.fun import touch

from test.lib import (
    StringProcessAdapter,
    TestBase,
    fixture_path,
    with_rw_directory,
    with_rw_repo,
)


class TestCommitSerialization(TestBase):
    def assert_commit_serialization(self, rwrepo, commit_id, print_performance_info=False):
        """Traverse all commits in the history of commit identified by commit_id and
        check if the serialization works.

        :param print_performance_info: If True, we will show how fast we are.
        """
        ns = 0  # Number of serializations.
        nds = 0  # Number of deserializations.

        st = time.time()
        for cm in rwrepo.commit(commit_id).traverse():
            nds += 1

            # Assert that we deserialize commits correctly, hence we get the same
            # sha on serialization.
            stream = BytesIO()
            cm._serialize(stream)
            ns += 1
            streamlen = stream.tell()
            stream.seek(0)

            istream = rwrepo.odb.store(IStream(Commit.type, streamlen, stream))
            self.assertEqual(istream.hexsha, cm.hexsha.encode("ascii"))

            nc = Commit(
                rwrepo,
                Commit.NULL_BIN_SHA,
                cm.tree,
                cm.author,
                cm.authored_date,
                cm.author_tz_offset,
                cm.committer,
                cm.committed_date,
                cm.committer_tz_offset,
                cm.message,
                cm.parents,
                cm.encoding,
            )

            self.assertEqual(nc.parents, cm.parents)
            stream = BytesIO()
            nc._serialize(stream)
            ns += 1
            streamlen = stream.tell()
            stream.seek(0)

            # Reuse istream.
            istream.size = streamlen
            istream.stream = stream
            istream.binsha = None
            nc.binsha = rwrepo.odb.store(istream).binsha

            # If it worked, we have exactly the same contents!
            self.assertEqual(nc.hexsha, cm.hexsha)
        # END check commits
        elapsed = time.time() - st

        if print_performance_info:
            print(
                "Serialized %i and deserialized %i commits in %f s ( (%f, %f) commits / s"
                % (ns, nds, elapsed, ns / elapsed, nds / elapsed),
                file=sys.stderr,
            )
        # END handle performance info


class TestCommit(TestCommitSerialization):
    def test_bake(self):
        commit = self.rorepo.commit("2454ae89983a4496a445ce347d7a41c0bb0ea7ae")
        # Commits have no dict.
        self.assertRaises(AttributeError, setattr, commit, "someattr", 1)
        commit.author  # bake

        self.assertEqual("Sebastian Thiel", commit.author.name)
        self.assertEqual("byronimo@gmail.com", commit.author.email)
        self.assertEqual(commit.author, commit.committer)
        assert isinstance(commit.authored_date, int) and isinstance(commit.committed_date, int)
        assert isinstance(commit.author_tz_offset, int) and isinstance(commit.committer_tz_offset, int)
        self.assertEqual(
            commit.message,
            "Added missing information to docstrings of commit and stats module\n",
        )

    def test_replace_no_changes(self):
        old_commit = self.rorepo.commit("2454ae89983a4496a445ce347d7a41c0bb0ea7ae")
        new_commit = old_commit.replace()

        for attr in old_commit.__slots__:
            assert getattr(new_commit, attr) == getattr(old_commit, attr)

    def test_replace_new_sha(self):
        commit = self.rorepo.commit("2454ae89983a4496a445ce347d7a41c0bb0ea7ae")
        new_commit = commit.replace(message="Added replace method")

        assert new_commit.hexsha == "fc84cbecac1bd4ba4deaac07c1044889edd536e6"
        assert new_commit.message == "Added replace method"

    def test_replace_invalid_attribute(self):
        commit = self.rorepo.commit("2454ae89983a4496a445ce347d7a41c0bb0ea7ae")

        with self.assertRaises(ValueError):
            commit.replace(badattr="This will never work")

    def test_stats(self):
        commit = self.rorepo.commit("33ebe7acec14b25c5f84f35a664803fcab2f7781")
        stats = commit.stats

        def check_entries(d, has_change_type=False):
            assert isinstance(d, dict)
            keys = ("insertions", "deletions", "lines")
            if has_change_type:
                keys += ("change_type",)
            for key in keys:
                assert key in d

        # END assertion helper
        assert stats.files
        assert stats.total

        check_entries(stats.total)
        assert "files" in stats.total

        for _filepath, d in stats.files.items():
            check_entries(d, True)
        # END for each stated file

        # Check that data is parsed properly.
        michael = Actor._from_string("Michael Trier <mtrier@gmail.com>")
        self.assertEqual(commit.author, michael)
        self.assertEqual(commit.committer, michael)
        self.assertEqual(commit.authored_date, 1210193388)
        self.assertEqual(commit.committed_date, 1210193388)
        self.assertEqual(commit.author_tz_offset, 14400, commit.author_tz_offset)
        self.assertEqual(commit.committer_tz_offset, 14400, commit.committer_tz_offset)
        self.assertEqual(commit.message, "initial project\n")

    def test_renames(self):
        commit = self.rorepo.commit("185d847ec7647fd2642a82d9205fb3d07ea71715")
        files = commit.stats.files

        # When a file is renamed, the output of git diff is like "dir/{old => new}"
        # unless we disable rename with --no-renames, which produces two lines,
        # one with the old path deletes and another with the new added.
        self.assertEqual(len(files), 2)

        def check_entries(path, changes):
            expected = {
                ".github/workflows/Future.yml": {
                    "insertions": 57,
                    "deletions": 0,
                    "lines": 57,
                },
                ".github/workflows/test_pytest.yml": {
                    "insertions": 0,
                    "deletions": 55,
                    "lines": 55,
                },
            }
            assert path in expected
            assert isinstance(changes, dict)
            for key in ("insertions", "deletions", "lines"):
                assert changes[key] == expected[path][key]

        for path, changes in files.items():
            check_entries(path, changes)
        # END for each stated file

    def test_unicode_actor(self):
        # Check that we can parse Unicode actors correctly.
        name = "Üäöß ÄußÉ"
        self.assertEqual(len(name), 9)
        special = Actor._from_string("%s <something@this.com>" % name)
        self.assertEqual(special.name, name)
        assert isinstance(special.name, str)

    def test_traversal(self):
        start = self.rorepo.commit("a4d06724202afccd2b5c54f81bcf2bf26dea7fff")
        first = self.rorepo.commit("33ebe7acec14b25c5f84f35a664803fcab2f7781")
        p0 = start.parents[0]
        p1 = start.parents[1]
        p00 = p0.parents[0]
        p10 = p1.parents[0]

        # Basic branch first, depth first.
        dfirst = start.traverse(branch_first=False)
        bfirst = start.traverse(branch_first=True)
        self.assertEqual(next(dfirst), p0)
        self.assertEqual(next(dfirst), p00)

        self.assertEqual(next(bfirst), p0)
        self.assertEqual(next(bfirst), p1)
        self.assertEqual(next(bfirst), p00)
        self.assertEqual(next(bfirst), p10)

        # At some point, both iterations should stop.
        self.assertEqual(list(bfirst)[-1], first)

        stoptraverse = self.rorepo.commit("254d04aa3180eb8b8daf7b7ff25f010cd69b4e7d").traverse(
            ignore_self=0, as_edge=True
        )
        stoptraverse_list = list(stoptraverse)
        for itemtup in stoptraverse_list:
            self.assertIsInstance(itemtup, (tuple)) and self.assertEqual(len(itemtup), 2)  # as_edge=True -> tuple
            src, item = itemtup
            self.assertIsInstance(item, Commit)
            if src:
                self.assertIsInstance(src, Commit)
            else:
                self.assertIsNone(src)  # ignore_self=0 -> first is (None, Commit)

        stoptraverse = self.rorepo.commit("254d04aa3180eb8b8daf7b7ff25f010cd69b4e7d").traverse(as_edge=True)
        self.assertEqual(len(next(stoptraverse)), 2)

        # Ignore self
        self.assertEqual(next(start.traverse(ignore_self=False)), start)

        # Depth
        self.assertEqual(len(list(start.traverse(ignore_self=False, depth=0))), 1)

        # Prune
        self.assertEqual(next(start.traverse(branch_first=1, prune=lambda i, d: i == p0)), p1)

        # Predicate
        self.assertEqual(next(start.traverse(branch_first=1, predicate=lambda i, d: i == p1)), p1)

        # Traversal should stop when the beginning is reached.
        self.assertRaises(StopIteration, next, first.traverse())

        # Parents of the first commit should be empty (as the only parent has a null sha)
        self.assertEqual(len(first.parents), 0)

    def test_iteration(self):
        # We can iterate commits.
        all_commits = Commit.list_items(self.rorepo, self.rorepo.head)
        assert all_commits
        self.assertEqual(all_commits, list(self.rorepo.iter_commits()))

        # This includes merge commits.
        mcomit = self.rorepo.commit("d884adc80c80300b4cc05321494713904ef1df2d")
        assert mcomit in all_commits

        # We can limit the result to paths.
        ltd_commits = list(self.rorepo.iter_commits(paths="CHANGES"))
        assert ltd_commits and len(ltd_commits) < len(all_commits)

        # Show commits of multiple paths, resulting in a union of commits.
        less_ltd_commits = list(Commit.iter_items(self.rorepo, "master", paths=("CHANGES", "AUTHORS")))
        assert len(ltd_commits) < len(less_ltd_commits)

        class Child(Commit):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

        child_commits = list(Child.iter_items(self.rorepo, "master", paths=("CHANGES", "AUTHORS")))
        assert type(child_commits[0]) is Child

    def test_iter_items(self):
        # pretty not allowed.
        self.assertRaises(ValueError, Commit.iter_items, self.rorepo, "master", pretty="raw")

    def test_rev_list_bisect_all(self):
        """
        'git rev-list --bisect-all' returns additional information
        in the commit header.  This test ensures that we properly parse it.
        """
        revs = self.rorepo.git.rev_list(
            "933d23bf95a5bd1624fbcdf328d904e1fa173474",
            first_parent=True,
            bisect_all=True,
        )

        commits = Commit._iter_from_process_or_stream(self.rorepo, StringProcessAdapter(revs.encode("ascii")))
        expected_ids = (
            "7156cece3c49544abb6bf7a0c218eb36646fad6d",
            "1f66cfbbce58b4b552b041707a12d437cc5f400a",
            "33ebe7acec14b25c5f84f35a664803fcab2f7781",
            "933d23bf95a5bd1624fbcdf328d904e1fa173474",
        )
        for sha1, commit in zip(expected_ids, commits):
            self.assertEqual(sha1, commit.hexsha)

    @with_rw_directory
    def test_ambiguous_arg_iteration(self, rw_dir):
        rw_repo = Repo.init(osp.join(rw_dir, "test_ambiguous_arg"))
        path = osp.join(str(rw_repo.working_tree_dir), "master")
        touch(path)
        rw_repo.index.add([path])
        rw_repo.index.commit("initial commit")
        list(rw_repo.iter_commits(rw_repo.head.ref))  # Should fail unless bug is fixed.

    def test_count(self):
        self.assertEqual(self.rorepo.tag("refs/tags/0.1.5").commit.count(), 143)

    def test_list(self):
        # This doesn't work anymore, as we will either attempt getattr with bytes, or
        # compare 20 byte string with actual 20 byte bytes. This usage makes no sense
        # anyway.
        assert isinstance(
            Commit.list_items(self.rorepo, "0.1.5", max_count=5)["5117c9c8a4d3af19a9958677e45cda9269de1541"],
            Commit,
        )

    def test_str(self):
        commit = Commit(self.rorepo, Commit.NULL_BIN_SHA)
        self.assertEqual(Commit.NULL_HEX_SHA, str(commit))

    def test_repr(self):
        commit = Commit(self.rorepo, Commit.NULL_BIN_SHA)
        self.assertEqual('<git.Commit "%s">' % Commit.NULL_HEX_SHA, repr(commit))

    def test_equality(self):
        commit1 = Commit(self.rorepo, Commit.NULL_BIN_SHA)
        commit2 = Commit(self.rorepo, Commit.NULL_BIN_SHA)
        commit3 = Commit(self.rorepo, "\1" * 20)
        self.assertEqual(commit1, commit2)
        self.assertNotEqual(commit2, commit3)

    def test_iter_parents(self):
        # Should return all but ourselves, even if skip is defined.
        c = self.rorepo.commit("0.1.5")
        for skip in (0, 1):
            piter = c.iter_parents(skip=skip)
            first_parent = next(piter)
            assert first_parent != c
            self.assertEqual(first_parent, c.parents[0])
        # END for each

    def test_name_rev(self):
        name_rev = self.rorepo.head.commit.name_rev
        assert isinstance(name_rev, str)

    @with_rw_repo("HEAD", bare=True)
    def test_serialization(self, rwrepo):
        # Create all commits of our repo.
        self.assert_commit_serialization(rwrepo, "0.1.6")

    def test_serialization_unicode_support(self):
        self.assertEqual(Commit.default_encoding.lower(), "utf-8")

        # Create a commit with Unicode in the message, and the author's name.
        # Verify its serialization and deserialization.
        cmt = self.rorepo.commit("0.1.6")
        assert isinstance(cmt.message, str)  # It automatically decodes it as such.
        assert isinstance(cmt.author.name, str)  # Same here.

        cmt.message = "üäêèß"
        self.assertEqual(len(cmt.message), 5)

        cmt.author.name = "äüß"
        self.assertEqual(len(cmt.author.name), 3)

        cstream = BytesIO()
        cmt._serialize(cstream)
        cstream.seek(0)
        assert len(cstream.getvalue())

        ncmt = Commit(self.rorepo, cmt.binsha)
        ncmt._deserialize(cstream)

        self.assertEqual(cmt.author.name, ncmt.author.name)
        self.assertEqual(cmt.message, ncmt.message)
        # Actually, it can't be printed in a shell as repr wants to have ascii only it
        # appears.
        cmt.author.__repr__()

    def test_invalid_commit(self):
        cmt = self.rorepo.commit()
        with open(fixture_path("commit_invalid_data"), "rb") as fd:
            cmt._deserialize(fd)

        self.assertEqual(cmt.author.name, "E.Azer Ko�o�o�oculu", cmt.author.name)
        self.assertEqual(cmt.author.email, "azer@kodfabrik.com", cmt.author.email)

    def test_gpgsig(self):
        cmt = self.rorepo.commit()
        with open(fixture_path("commit_with_gpgsig"), "rb") as fd:
            cmt._deserialize(fd)

        fixture_sig = """-----BEGIN PGP SIGNATURE-----
Version: GnuPG v1.4.11 (GNU/Linux)

iQIcBAABAgAGBQJRk8zMAAoJEG5mS6x6i9IjsTEP/0v2Wx/i7dqyKban6XMIhVdj
uI0DycfXqnCCZmejidzeao+P+cuK/ZAA/b9fU4MtwkDm2USvnIOrB00W0isxsrED
sdv6uJNa2ybGjxBolLrfQcWutxGXLZ1FGRhEvkPTLMHHvVriKoNFXcS7ewxP9MBf
NH97K2wauqA+J4BDLDHQJgADCOmLrGTAU+G1eAXHIschDqa6PZMH5nInetYZONDh
3SkOOv8VKFIF7gu8X7HC+7+Y8k8U0TW0cjlQ2icinwCc+KFoG6GwXS7u/VqIo1Yp
Tack6sxIdK7NXJhV5gAeAOMJBGhO0fHl8UUr96vGEKwtxyZhWf8cuIPOWLk06jA0
g9DpLqmy/pvyRfiPci+24YdYRBua/vta+yo/Lp85N7Hu/cpIh+q5WSLvUlv09Dmo
TTTG8Hf6s3lEej7W8z2xcNZoB6GwXd8buSDU8cu0I6mEO9sNtAuUOHp2dBvTA6cX
PuQW8jg3zofnx7CyNcd3KF3nh2z8mBcDLgh0Q84srZJCPRuxRcp9ylggvAG7iaNd
XMNvSK8IZtWLkx7k3A3QYt1cN4y1zdSHLR2S+BVCEJea1mvUE+jK5wiB9S4XNtKm
BX/otlTa8pNE3fWYBxURvfHnMY4i3HQT7Bc1QjImAhMnyo2vJk4ORBJIZ1FTNIhJ
JzJMZDRLQLFvnzqZuCjE
=przd
-----END PGP SIGNATURE-----"""
        self.assertEqual(cmt.gpgsig, fixture_sig)

        cmt.gpgsig = "<test\ndummy\nsig>"
        assert cmt.gpgsig != fixture_sig

        cstream = BytesIO()
        cmt._serialize(cstream)
        assert re.search(
            r"^gpgsig <test\n dummy\n sig>$",
            cstream.getvalue().decode("ascii"),
            re.MULTILINE,
        )

        self.assert_gpgsig_deserialization(cstream)

        cstream.seek(0)
        cmt.gpgsig = None
        cmt._deserialize(cstream)
        self.assertEqual(cmt.gpgsig, "<test\ndummy\nsig>")

        cmt.gpgsig = None
        cstream = BytesIO()
        cmt._serialize(cstream)
        assert not re.search(r"^gpgsig ", cstream.getvalue().decode("ascii"), re.MULTILINE)

    def assert_gpgsig_deserialization(self, cstream):
        assert "gpgsig" in "precondition: need gpgsig"

        class RepoMock:
            def __init__(self, bytestr):
                self.bytestr = bytestr

            @property
            def odb(self):
                class ODBMock:
                    def __init__(self, bytestr):
                        self.bytestr = bytestr

                    def stream(self, *args):
                        stream = Mock(spec_set=["read"], return_value=self.bytestr)
                        stream.read.return_value = self.bytestr
                        return ("binsha", "typename", "size", stream)

                return ODBMock(self.bytestr)

        repo_mock = RepoMock(cstream.getvalue())
        for field in Commit.__slots__:
            c = Commit(repo_mock, b"x" * 20)
            assert getattr(c, field) is not None

    def test_datetimes(self):
        commit = self.rorepo.commit("4251bd5")
        self.assertEqual(commit.authored_date, 1255018625)
        self.assertEqual(commit.committed_date, 1255026171)
        self.assertEqual(
            commit.authored_datetime,
            datetime(2009, 10, 8, 18, 17, 5, tzinfo=tzoffset(-7200)),
            commit.authored_datetime,
        )
        self.assertEqual(
            commit.authored_datetime,
            datetime(2009, 10, 8, 16, 17, 5, tzinfo=utc),
            commit.authored_datetime,
        )
        self.assertEqual(
            commit.committed_datetime,
            datetime(2009, 10, 8, 20, 22, 51, tzinfo=tzoffset(-7200)),
        )
        self.assertEqual(
            commit.committed_datetime,
            datetime(2009, 10, 8, 18, 22, 51, tzinfo=utc),
            commit.committed_datetime,
        )

    def test_trailers(self):
        KEY_1 = "Hello"
        VALUE_1_1 = "World"
        VALUE_1_2 = "Another-World"
        KEY_2 = "Key"
        VALUE_2 = "Value with inner spaces"

        # Check that the following trailer example is extracted from multiple msg
        # variations.
        TRAILER = f"{KEY_1}: {VALUE_1_1}\n{KEY_2}: {VALUE_2}\n{KEY_1}: {VALUE_1_2}"
        msgs = [
            f"Subject\n\n{TRAILER}\n",
            f"Subject\n  \nSome body of a function\n \n{TRAILER}\n",
            f"Subject\n  \nSome body of a function\n\nnon-key: non-value\n\n{TRAILER}\n",
            (
                # Check when trailer has inconsistent whitespace.
                f"Subject\n  \nSome multiline\n body of a function\n\nnon-key: non-value\n\n"
                f"{KEY_1}:{VALUE_1_1}\n{KEY_2} :      {VALUE_2}\n{KEY_1}:    {VALUE_1_2}\n"
            ),
        ]
        for msg in msgs:
            commit = copy.copy(self.rorepo.commit("master"))
            commit.message = msg
            assert commit.trailers_list == [
                (KEY_1, VALUE_1_1),
                (KEY_2, VALUE_2),
                (KEY_1, VALUE_1_2),
            ]
            assert commit.trailers_dict == {
                KEY_1: [VALUE_1_1, VALUE_1_2],
                KEY_2: [VALUE_2],
            }

        # Check that the trailer stays empty for multiple msg combinations.
        msgs = [
            "Subject\n",
            "Subject\n\nBody with some\nText\n",
            "Subject\n\nBody with\nText\n\nContinuation but\n doesn't contain colon\n",
            "Subject\n\nBody with\nText\n\nContinuation but\n only contains one :\n",
            "Subject\n\nBody with\nText\n\nKey: Value\nLine without colon\n",
            "Subject\n\nBody with\nText\n\nLine without colon\nKey: Value\n",
        ]

        for msg in msgs:
            commit = copy.copy(self.rorepo.commit("master"))
            commit.message = msg
            assert commit.trailers_list == []
            assert commit.trailers_dict == {}

        # Check that only the last key value paragraph is evaluated.
        commit = copy.copy(self.rorepo.commit("master"))
        commit.message = f"Subject\n\nMultiline\nBody\n\n{KEY_1}: {VALUE_1_1}\n\n{KEY_2}: {VALUE_2}\n"
        assert commit.trailers_list == [(KEY_2, VALUE_2)]
        assert commit.trailers_dict == {KEY_2: [VALUE_2]}

    def test_commit_co_authors(self):
        commit = copy.copy(self.rorepo.commit("4251bd5"))
        commit.message = """Commit message

Co-authored-by: Test User 1 <602352+test@users.noreply.github.com>
Co-authored-by: test_user_2 <another_user-email@github.com>
Co_authored_by: test_user_x <test@github.com>
Co-authored-by: test_user_y <test@github.com> text
Co-authored-by: test_user_3 <test_user_3@github.com>"""
        assert commit.co_authors == [
            Actor("Test User 1", "602352+test@users.noreply.github.com"),
            Actor("test_user_2", "another_user-email@github.com"),
            Actor("test_user_3", "test_user_3@github.com"),
        ]
