# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

import contextlib
import gc
import os
import os.path as osp
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import mock, skipUnless

import pytest

import git
from git.cmd import Git
from git.config import GitConfigParser, cp
from git.exc import (
    GitCommandError,
    InvalidGitRepositoryError,
    RepositoryDirtyError,
    UnsafeOptionError,
    UnsafeProtocolError,
)
from git.objects.submodule.base import Submodule
from git.objects.submodule.root import RootModule, RootUpdateProgress
from git.repo.fun import find_submodule_git_dir, touch
from git.util import HIDE_WINDOWS_KNOWN_ERRORS, join_path_native, to_native_path_linux

from test.lib import TestBase, with_rw_directory, with_rw_repo


@contextlib.contextmanager
def _patch_git_config(name, value):
    """Temporarily add a git config name-value pair, using environment variables."""
    pair_index = int(os.getenv("GIT_CONFIG_COUNT", "0"))

    # This is recomputed each time the context is entered, for compatibility with
    # existing GIT_CONFIG_* environment variables, even if changed in this process.
    patcher = mock.patch.dict(
        os.environ,
        {
            "GIT_CONFIG_COUNT": str(pair_index + 1),
            f"GIT_CONFIG_KEY_{pair_index}": name,
            f"GIT_CONFIG_VALUE_{pair_index}": value,
        },
    )

    with patcher:
        yield


class TestRootProgress(RootUpdateProgress):
    """Just prints messages, for now without checking the correctness of the states"""

    def update(self, op, cur_count, max_count, message=""):
        print(op, cur_count, max_count, message)


prog = TestRootProgress()


class TestSubmodule(TestBase):
    def tearDown(self):
        gc.collect()

    k_subm_current = "c15a6e1923a14bc760851913858a3942a4193cdb"
    k_subm_changed = "394ed7006ee5dc8bddfd132b64001d5dfc0ffdd3"
    k_no_subm_tag = "0.1.6"

    def _do_base_tests(self, rwrepo):
        """Perform all tests in the given repository, it may be bare or nonbare"""
        # Manual instantiation.
        smm = Submodule(rwrepo, "\0" * 20)
        # Name needs to be set in advance.
        self.assertRaises(AttributeError, getattr, smm, "name")

        # Iterate - 1 submodule.
        sms = Submodule.list_items(rwrepo, self.k_subm_current)
        assert len(sms) == 1
        sm = sms[0]

        # At a different time, there is None.
        assert len(Submodule.list_items(rwrepo, self.k_no_subm_tag)) == 0

        assert sm.path == "git/ext/gitdb"
        assert sm.path != sm.name  # In our case, we have ids there, which don't equal the path.
        assert sm.url.endswith("github.com/gitpython-developers/gitdb.git")
        assert sm.branch_path == "refs/heads/master"  # the default ...
        assert sm.branch_name == "master"
        assert sm.parent_commit == rwrepo.head.commit
        # Size is always 0.
        assert sm.size == 0
        # The module is not checked-out yet.
        self.assertRaises(InvalidGitRepositoryError, sm.module)

        # ...which is why we can't get the branch either - it points into the module()
        # repository.
        self.assertRaises(InvalidGitRepositoryError, getattr, sm, "branch")

        # branch_path works, as it's just a string.
        assert isinstance(sm.branch_path, str)

        # Some commits earlier we still have a submodule, but it's at a different
        # commit.
        smold = next(Submodule.iter_items(rwrepo, self.k_subm_changed))
        assert smold.binsha != sm.binsha
        assert smold != sm  # the name changed

        # Force it to reread its information.
        del smold._url
        smold.url == sm.url  # noqa: B015  # FIXME: Should this be an assertion?

        # Test config_reader/writer methods.
        sm.config_reader()
        new_smclone_path = None  # Keep custom paths for later.
        new_csmclone_path = None  #
        if rwrepo.bare:
            with self.assertRaises(InvalidGitRepositoryError):
                with sm.config_writer() as cw:
                    pass
        else:
            with sm.config_writer() as writer:
                # For faster checkout, set the url to the local path.
                new_smclone_path = Git.polish_url(osp.join(self.rorepo.working_tree_dir, sm.path))
                writer.set_value("url", new_smclone_path)
                writer.release()
                assert sm.config_reader().get_value("url") == new_smclone_path
                assert sm.url == new_smclone_path
        # END handle bare repo
        smold.config_reader()

        # Cannot get a writer on historical submodules.
        if not rwrepo.bare:
            with self.assertRaises(ValueError):
                with smold.config_writer():
                    pass
        # END handle bare repo

        # Make the old into a new - this doesn't work as the name changed.
        self.assertRaises(ValueError, smold.set_parent_commit, self.k_subm_current)
        # the sha is properly updated
        smold.set_parent_commit(self.k_subm_changed + "~1")
        assert smold.binsha != sm.binsha

        # Raises if the sm didn't exist in new parent - it keeps its parent_commit
        # unchanged.
        self.assertRaises(ValueError, smold.set_parent_commit, self.k_no_subm_tag)

        # TODO: Test that, if a path is in the .gitmodules file, but not in the index,
        # then it raises.

        # TEST UPDATE
        ##############
        # Module retrieval is not always possible.
        if rwrepo.bare:
            self.assertRaises(InvalidGitRepositoryError, sm.module)
            self.assertRaises(InvalidGitRepositoryError, sm.remove)
            self.assertRaises(InvalidGitRepositoryError, sm.add, rwrepo, "here", "there")
        else:
            # It's not checked out in our case.
            self.assertRaises(InvalidGitRepositoryError, sm.module)
            assert not sm.module_exists()

            # Currently there is only one submodule.
            assert len(list(rwrepo.iter_submodules())) == 1
            assert sm.binsha != "\0" * 20

            # TEST ADD
            ###########
            # Preliminary tests.
            # Adding existing returns exactly the existing.
            sma = Submodule.add(rwrepo, sm.name, sm.path)
            assert sma.path == sm.path

            # No url and no module at path fails.
            self.assertRaises(ValueError, Submodule.add, rwrepo, "newsubm", "pathtorepo", url=None)

            # CONTINUE UPDATE
            #################

            # Let's update it - it's a recursive one too.
            newdir = osp.join(sm.abspath, "dir")
            os.makedirs(newdir)

            # Update fails if the path already exists non-empty.
            self.assertRaises(OSError, sm.update)
            os.rmdir(newdir)

            # Dry-run does nothing.
            sm.update(dry_run=True, progress=prog)
            assert not sm.module_exists()

            assert sm.update() is sm
            sm_repopath = sm.path  # Cache for later.
            assert sm.module_exists()
            assert isinstance(sm.module(), git.Repo)
            assert sm.module().working_tree_dir == sm.abspath

            # INTERLEAVE ADD TEST
            #####################
            # url must match the one in the existing repository (if submodule name
            # suggests a new one) or we raise.
            self.assertRaises(
                ValueError,
                Submodule.add,
                rwrepo,
                "newsubm",
                sm.path,
                "git://someurl/repo.git",
            )

            # CONTINUE UPDATE
            #################
            # We should have setup a tracking branch, which is also active.
            assert sm.module().head.ref.tracking_branch() is not None

            # Delete the whole directory and re-initialize.
            assert len(sm.children()) != 0
            # shutil.rmtree(sm.abspath)
            sm.remove(force=True, configuration=False)
            assert len(sm.children()) == 0
            # Dry-run does nothing.
            sm.update(dry_run=True, recursive=False, progress=prog)
            assert len(sm.children()) == 0

            sm.update(recursive=False)
            assert len(list(rwrepo.iter_submodules())) == 2
            assert len(sm.children()) == 1  # It's not checked out yet.
            csm = sm.children()[0]
            assert not csm.module_exists()
            csm_repopath = csm.path

            # Adjust the path of the submodules module to point to the local
            # destination.
            new_csmclone_path = Git.polish_url(osp.join(self.rorepo.working_tree_dir, sm.path, csm.path))
            with csm.config_writer() as writer:
                writer.set_value("url", new_csmclone_path)
            assert csm.url == new_csmclone_path

            # Dry-run does nothing.
            assert not csm.module_exists()
            sm.update(recursive=True, dry_run=True, progress=prog)
            assert not csm.module_exists()

            # Update recursively again.
            sm.update(recursive=True)
            assert csm.module_exists()

            # Tracking branch once again.
            assert csm.module().head.ref.tracking_branch() is not None

            # This flushed in a sub-submodule.
            assert len(list(rwrepo.iter_submodules())) == 2

            # Reset both heads to the previous version, verify that to_latest_revision
            # works.
            smods = (sm.module(), csm.module())
            for repo in smods:
                repo.head.reset("HEAD~2", working_tree=1)
            # END for each repo to reset

            # Dry-run does nothing.
            self.assertRaises(
                RepositoryDirtyError,
                sm.update,
                recursive=True,
                dry_run=True,
                progress=prog,
            )
            sm.update(recursive=True, dry_run=True, progress=prog, force=True)
            for repo in smods:
                assert repo.head.commit != repo.head.ref.tracking_branch().commit
            # END for each repo to check

            self.assertRaises(RepositoryDirtyError, sm.update, recursive=True, to_latest_revision=True)
            sm.update(recursive=True, to_latest_revision=True, force=True)
            for repo in smods:
                assert repo.head.commit == repo.head.ref.tracking_branch().commit
            # END for each repo to check
            del smods

            # If the head is detached, it still works (but warns).
            smref = sm.module().head.ref
            sm.module().head.ref = "HEAD~1"
            # If there is no tracking branch, we get a warning as well.
            csm_tracking_branch = csm.module().head.ref.tracking_branch()
            csm.module().head.ref.set_tracking_branch(None)
            sm.update(recursive=True, to_latest_revision=True)

            # to_latest_revision changes the child submodule's commit, it needs an
            # update now.
            csm.set_parent_commit(csm.repo.head.commit)

            # Undo the changes.
            sm.module().head.ref = smref
            csm.module().head.ref.set_tracking_branch(csm_tracking_branch)

            # REMOVAL OF REPOSITORY
            #######################
            # Must delete something.
            self.assertRaises(ValueError, csm.remove, module=False, configuration=False)

            # module() is supposed to point to gitdb, which has a child-submodule whose
            # URL is still pointing to GitHub. To save time, we will change it to:
            csm.set_parent_commit(csm.repo.head.commit)
            with csm.config_writer() as cw:
                cw.set_value("url", self._small_repo_url())
            csm.repo.index.commit("adjusted URL to point to local source, instead of the internet")

            # We have modified the configuration, hence the index is dirty, and the
            # deletion will fail.
            # NOTE: As we did a few updates in the meanwhile, the indices were reset.
            # Hence we create some changes.
            csm.set_parent_commit(csm.repo.head.commit)
            with sm.config_writer() as writer:
                writer.set_value("somekey", "somevalue")
            with csm.config_writer() as writer:
                writer.set_value("okey", "ovalue")
            self.assertRaises(InvalidGitRepositoryError, sm.remove)
            # If we remove the dirty index, it would work.
            sm.module().index.reset()
            # Still, we have the file modified.
            self.assertRaises(InvalidGitRepositoryError, sm.remove, dry_run=True)
            sm.module().index.reset(working_tree=True)

            # Enforce the submodule to be checked out at the right spot as well.
            csm.update()
            assert csm.module_exists()
            assert csm.exists()
            assert osp.isdir(csm.module().working_tree_dir)

            # This would work.
            assert sm.remove(force=True, dry_run=True) is sm
            assert sm.module_exists()
            sm.remove(force=True, dry_run=True)
            assert sm.module_exists()

            # But... we have untracked files in the child submodule.
            fn = join_path_native(csm.module().working_tree_dir, "newfile")
            with open(fn, "w") as fd:
                fd.write("hi")
            self.assertRaises(InvalidGitRepositoryError, sm.remove)

            # Forcibly delete the child repository.
            prev_count = len(sm.children())
            self.assertRaises(ValueError, csm.remove, force=True)
            # We removed sm, which removed all submodules. However, the instance we
            # have still points to the commit prior to that, where it still existed.
            csm.set_parent_commit(csm.repo.commit(), check=False)
            assert not csm.exists()
            assert not csm.module_exists()
            assert len(sm.children()) == prev_count
            # Now we have a changed index, as configuration was altered.
            # Fix this.
            sm.module().index.reset(working_tree=True)

            # Now delete only the module of the main submodule.
            assert sm.module_exists()
            sm.remove(configuration=False, force=True)
            assert sm.exists()
            assert not sm.module_exists()
            assert sm.config_reader().get_value("url")

            # Delete the rest.
            sm_path = sm.path
            sm.remove()
            assert not sm.exists()
            assert not sm.module_exists()
            self.assertRaises(ValueError, getattr, sm, "path")

            assert len(rwrepo.submodules) == 0

            # ADD NEW SUBMODULE
            ###################
            # Add a simple remote repo - trailing slashes are no problem.
            smid = "newsub"
            osmid = "othersub"
            nsm = Submodule.add(
                rwrepo,
                smid,
                sm_repopath,
                new_smclone_path + "/",
                None,
                no_checkout=True,
            )
            assert nsm.name == smid
            assert nsm.module_exists()
            assert nsm.exists()
            # It's not checked out.
            assert not osp.isfile(join_path_native(nsm.module().working_tree_dir, Submodule.k_modules_file))
            assert len(rwrepo.submodules) == 1

            # Add another submodule, but into the root, not as submodule.
            osm = Submodule.add(rwrepo, osmid, csm_repopath, new_csmclone_path, Submodule.k_head_default)
            assert osm != nsm
            assert osm.module_exists()
            assert osm.exists()
            assert osp.isfile(join_path_native(osm.module().working_tree_dir, "setup.py"))

            assert len(rwrepo.submodules) == 2

            # Commit the changes, just to finalize the operation.
            rwrepo.index.commit("my submod commit")
            assert len(rwrepo.submodules) == 2

            # Needs update, as the head changed.
            # It thinks it's in the history of the repo otherwise.
            nsm.set_parent_commit(rwrepo.head.commit)
            osm.set_parent_commit(rwrepo.head.commit)

            # MOVE MODULE
            #############
            # Invalid input.
            self.assertRaises(ValueError, nsm.move, "doesntmatter", module=False, configuration=False)

            # Renaming to the same path does nothing.
            assert nsm.move(sm_path) is nsm

            # Rename a module.
            nmp = join_path_native("new", "module", "dir") + "/"  # New module path.
            pmp = nsm.path
            assert nsm.move(nmp) is nsm
            nmp = nmp[:-1]  # Cut last /
            nmpl = to_native_path_linux(nmp)
            assert nsm.path == nmpl
            assert rwrepo.submodules[0].path == nmpl

            mpath = "newsubmodule"
            absmpath = join_path_native(rwrepo.working_tree_dir, mpath)
            open(absmpath, "w").write("")
            self.assertRaises(ValueError, nsm.move, mpath)
            os.remove(absmpath)

            # Now it works, as we just move it back.
            nsm.move(pmp)
            assert nsm.path == pmp
            assert rwrepo.submodules[0].path == pmp

            # REMOVE 'EM ALL
            ################
            # If a submodule's repo has no remotes, it can't be added without an
            # explicit url.
            osmod = osm.module()

            osm.remove(module=False)
            for remote in osmod.remotes:
                remote.remove(osmod, remote.name)
            assert not osm.exists()
            self.assertRaises(ValueError, Submodule.add, rwrepo, osmid, csm_repopath, url=None)
        # END handle bare mode

        # Error if there is no submodule file here.
        self.assertRaises(
            IOError,
            Submodule._config_parser,
            rwrepo,
            rwrepo.commit(self.k_no_subm_tag),
            True,
        )

    # ACTUALLY skipped by git.util.rmtree (in local onerror function), called via
    # git.objects.submodule.base.Submodule.remove at "method(mp)", line 1011.
    #
    # @skipIf(HIDE_WINDOWS_KNOWN_ERRORS,
    #         "FIXME: fails with: PermissionError: [WinError 32] The process cannot access the file because"
    #         "it is being used by another process: "
    #         "'C:\\Users\\ankostis\\AppData\\Local\\Temp\\tmp95c3z83bnon_bare_test_base_rw\\git\\ext\\gitdb\\gitdb\\ext\\smmap'")  # noqa: E501
    @with_rw_repo(k_subm_current)
    def test_base_rw(self, rwrepo):
        self._do_base_tests(rwrepo)

    @with_rw_repo(k_subm_current, bare=True)
    def test_base_bare(self, rwrepo):
        self._do_base_tests(rwrepo)

    @pytest.mark.xfail(
        sys.platform == "cygwin",
        reason="Cygwin GitPython can't find submodule SHA",
        raises=ValueError,
    )
    @pytest.mark.xfail(
        HIDE_WINDOWS_KNOWN_ERRORS,
        reason=(
            '"The process cannot access the file because it is being used by another process"'
            + " on first call to rm.update"
        ),
        raises=PermissionError,
    )
    @with_rw_repo(k_subm_current, bare=False)
    def test_root_module(self, rwrepo):
        # Can query everything without problems.
        rm = RootModule(self.rorepo)
        assert rm.module() is self.rorepo

        # Try attributes.
        rm.binsha
        rm.mode
        rm.path
        assert rm.name == rm.k_root_name
        assert rm.parent_commit == self.rorepo.head.commit
        rm.url
        rm.branch

        assert len(rm.list_items(rm.module())) == 1
        rm.config_reader()
        with rm.config_writer():
            pass

        # Deep traversal gitdb / async.
        rsmsp = [sm.path for sm in rm.traverse()]
        assert len(rsmsp) >= 2  # gitdb and async [and smmap], async being a child of gitdb.

        # Cannot set the parent commit as root module's path didn't exist.
        self.assertRaises(ValueError, rm.set_parent_commit, "HEAD")

        # TEST UPDATE
        #############
        # Set up a commit that removes existing, adds new and modifies existing
        # submodules.
        rm = RootModule(rwrepo)
        assert len(rm.children()) == 1

        # Modify path without modifying the index entry.
        # (Which is what the move method would do properly.)
        # ==================================================
        sm = rm.children()[0]
        pp = "path/prefix"
        fp = join_path_native(pp, sm.path)
        prep = sm.path
        assert not sm.module_exists()  # It was never updated after rwrepo's clone.

        # Ensure we clone from a local source.
        with sm.config_writer() as writer:
            writer.set_value("url", Git.polish_url(osp.join(self.rorepo.working_tree_dir, sm.path)))

        # Dry-run does nothing.
        sm.update(recursive=False, dry_run=True, progress=prog)
        assert not sm.module_exists()

        sm.update(recursive=False)
        assert sm.module_exists()
        with sm.config_writer() as writer:
            # Change path to something with prefix AFTER url change.
            writer.set_value("path", fp)

        # Update doesn't fail, because list_items ignores the wrong path in such
        # situations.
        rm.update(recursive=False)

        # Move it properly - doesn't work as it its path currently points to an
        # indexentry which doesn't exist (move it to some path, it doesn't matter here).
        self.assertRaises(InvalidGitRepositoryError, sm.move, pp)
        # Reset the path(cache) to where it was, now it works.
        sm.path = prep
        sm.move(fp, module=False)  # Leave it at the old location.

        assert not sm.module_exists()
        cpathchange = rwrepo.index.commit("changed sm path")  # Finally we can commit.

        # Update puts the module into place.
        rm.update(recursive=False, progress=prog)
        sm.set_parent_commit(cpathchange)
        assert sm.module_exists()

        # Add submodule.
        # ==============
        nsmn = "newsubmodule"
        nsmp = "submrepo"
        subrepo_url = Git.polish_url(osp.join(self.rorepo.working_tree_dir, rsmsp[0], rsmsp[1]))
        nsm = Submodule.add(rwrepo, nsmn, nsmp, url=subrepo_url)
        csmadded = rwrepo.index.commit("Added submodule").hexsha  # Make sure we don't keep the repo reference.
        nsm.set_parent_commit(csmadded)
        assert nsm.module_exists()
        # In our case, the module should not exist, which happens if we update a parent
        # repo and a new submodule comes into life.
        nsm.remove(configuration=False, module=True)
        assert not nsm.module_exists() and nsm.exists()

        # Dry-run does nothing.
        rm.update(recursive=False, dry_run=True, progress=prog)

        # Otherwise it will work.
        rm.update(recursive=False, progress=prog)
        assert nsm.module_exists()

        # Remove submodule - the previous one.
        # ====================================
        sm.set_parent_commit(csmadded)
        smp = sm.abspath
        assert not sm.remove(module=False).exists()
        assert osp.isdir(smp)  # Module still exists.
        csmremoved = rwrepo.index.commit("Removed submodule")

        # An update will remove the module.
        # Not in dry_run.
        rm.update(recursive=False, dry_run=True, force_remove=True)
        assert osp.isdir(smp)

        # When removing submodules, we may get new commits as nested submodules are
        # auto-committing changes to allow deletions without force, as the index would
        # be dirty otherwise.
        # QUESTION: Why does this seem to work in test_git_submodule_compatibility() ?
        self.assertRaises(InvalidGitRepositoryError, rm.update, recursive=False, force_remove=False)
        rm.update(recursive=False, force_remove=True)
        assert not osp.isdir(smp)

        # 'Apply work' to the nested submodule and ensure this is not removed/altered
        # during updates. We need to commit first, otherwise submodule.update wouldn't
        # have a reason to change the head.
        touch(osp.join(nsm.module().working_tree_dir, "new-file"))
        # We cannot expect is_dirty to even run as we wouldn't reset a head to the same
        # location.
        assert nsm.module().head.commit.hexsha == nsm.hexsha
        nsm.module().index.add([nsm])
        nsm.module().index.commit("added new file")
        rm.update(recursive=False, dry_run=True, progress=prog)  # Would not change head, and thus doesn't fail.
        # Everything we can do from now on will trigger the 'future' check, so no
        # is_dirty() check will even run. This would only run if our local branch is in
        # the past and we have uncommitted changes.

        prev_commit = nsm.module().head.commit
        rm.update(recursive=False, dry_run=False, progress=prog)
        assert prev_commit == nsm.module().head.commit, "head shouldn't change, as it is in future of remote branch"

        # this kills the new file
        rm.update(recursive=True, progress=prog, force_reset=True)
        assert prev_commit != nsm.module().head.commit, "head changed, as the remote url and its commit changed"

        # Change url...
        # =============
        # ...to the first repository. This way we have a fast checkout, and a completely
        # different repository at the different url.
        nsm.set_parent_commit(csmremoved)
        nsmurl = Git.polish_url(osp.join(self.rorepo.working_tree_dir, rsmsp[0]))
        with nsm.config_writer() as writer:
            writer.set_value("url", nsmurl)
        csmpathchange = rwrepo.index.commit("changed url")
        nsm.set_parent_commit(csmpathchange)

        # Now nsm head is in the future of the tracked remote branch.
        prev_commit = nsm.module().head.commit
        # dry-run does nothing
        rm.update(recursive=False, dry_run=True, progress=prog)
        assert nsm.module().remotes.origin.url != nsmurl

        rm.update(recursive=False, progress=prog, force_reset=True)
        assert nsm.module().remotes.origin.url == nsmurl
        assert prev_commit != nsm.module().head.commit, "Should now point to gitdb"
        assert len(rwrepo.submodules) == 1
        assert not rwrepo.submodules[0].children()[0].module_exists(), "nested submodule should not be checked out"

        # Add the submodule's changed commit to the index, which is what the user would
        # do. Beforehand, update our instance's binsha with the new one.
        nsm.binsha = nsm.module().head.commit.binsha
        rwrepo.index.add([nsm])

        # Change branch.
        # ==============
        # We only have one branch, so we switch to a virtual one, and back to the
        # current one to trigger the difference.
        cur_branch = nsm.branch
        nsmm = nsm.module()
        prev_commit = nsmm.head.commit
        for branch in ("some_virtual_branch", cur_branch.name):
            with nsm.config_writer() as writer:
                writer.set_value(Submodule.k_head_option, git.Head.to_full_path(branch))
            csmbranchchange = rwrepo.index.commit("changed branch to %s" % branch)
            nsm.set_parent_commit(csmbranchchange)
        # END for each branch to change

        # Let's remove our tracking branch to simulate some changes.
        nsmmh = nsmm.head
        assert nsmmh.ref.tracking_branch() is None  # Never set it up until now.
        assert not nsmmh.is_detached

        # Dry-run does nothing.
        rm.update(recursive=False, dry_run=True, progress=prog)
        assert nsmmh.ref.tracking_branch() is None

        # The real thing does.
        rm.update(recursive=False, progress=prog)

        assert nsmmh.ref.tracking_branch() is not None
        assert not nsmmh.is_detached

        # Recursive update.
        # =================
        # Finally we recursively update a module, just to run the code at least once
        # remove the module so that it has more work.
        assert len(nsm.children()) >= 1  # Could include smmap.
        assert nsm.exists() and nsm.module_exists() and len(nsm.children()) >= 1
        # Ensure we pull locally only.
        nsmc = nsm.children()[0]
        with nsmc.config_writer() as writer:
            writer.set_value("url", subrepo_url)
        rm.update(recursive=True, progress=prog, dry_run=True)  # Just to run the code.
        rm.update(recursive=True, progress=prog)

        # gitdb: has either 1 or 2 submodules depending on the version.
        assert len(nsm.children()) >= 1 and nsmc.module_exists()

    def test_iter_items_from_nonexistent_hash(self):
        it = Submodule.iter_items(self.rorepo, "b4ecbfaa90c8be6ed6d9fb4e57cc824663ae15b4")
        with self.assertRaisesRegex(ValueError, r"\bcould not be resolved\b"):
            next(it)

    def test_iter_items_from_invalid_hash(self):
        """Check legacy behavaior on BadName (also applies to IOError, i.e. OSError)."""
        it = Submodule.iter_items(self.rorepo, "xyz")
        with self.assertRaises(StopIteration) as ctx:
            next(it)
        self.assertIsNone(ctx.exception.value)

    @with_rw_repo(k_no_subm_tag, bare=False)
    def test_first_submodule(self, rwrepo):
        assert len(list(rwrepo.iter_submodules())) == 0

        for sm_name, sm_path in (
            ("first", "submodules/first"),
            ("second", osp.join(rwrepo.working_tree_dir, "submodules/second")),
        ):
            sm = rwrepo.create_submodule(sm_name, sm_path, rwrepo.git_dir, no_checkout=True)
            assert sm.exists() and sm.module_exists()
            rwrepo.index.commit("Added submodule " + sm_name)
        # END for each submodule path to add

        self.assertRaises(ValueError, rwrepo.create_submodule, "fail", osp.expanduser("~"))
        self.assertRaises(
            ValueError,
            rwrepo.create_submodule,
            "fail-too",
            rwrepo.working_tree_dir + osp.sep,
        )

    @with_rw_directory
    def test_add_empty_repo(self, rwdir):
        empty_repo_dir = osp.join(rwdir, "empty-repo")

        parent = git.Repo.init(osp.join(rwdir, "parent"))
        git.Repo.init(empty_repo_dir)

        for checkout_mode in range(2):
            name = "empty" + str(checkout_mode)
            self.assertRaises(
                ValueError,
                parent.create_submodule,
                name,
                name,
                url=empty_repo_dir,
                no_checkout=checkout_mode and True or False,
            )
        # END for each checkout mode

    @with_rw_directory
    @_patch_git_config("protocol.file.allow", "always")
    def test_list_only_valid_submodules(self, rwdir):
        repo_path = osp.join(rwdir, "parent")
        repo = git.Repo.init(repo_path)
        repo.git.submodule("add", self._small_repo_url(), "module")
        repo.index.commit("add submodule")

        assert len(repo.submodules) == 1

        # Delete the directory from submodule.
        submodule_path = osp.join(repo_path, "module")
        shutil.rmtree(submodule_path)
        repo.git.add([submodule_path])
        repo.index.commit("remove submodule")

        repo = git.Repo(repo_path)
        assert len(repo.submodules) == 0

    @pytest.mark.xfail(
        HIDE_WINDOWS_KNOWN_ERRORS,
        reason=(
            '"The process cannot access the file because it is being used by another process"'
            + " on first call to sm.move"
        ),
        raises=PermissionError,
    )
    @with_rw_directory
    @_patch_git_config("protocol.file.allow", "always")
    def test_git_submodules_and_add_sm_with_new_commit(self, rwdir):
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        parent.git.submodule("add", self._small_repo_url(), "module")
        parent.index.commit("added submodule")

        assert len(parent.submodules) == 1
        sm = parent.submodules[0]

        assert sm.exists() and sm.module_exists()

        clone = git.Repo.clone_from(
            self._small_repo_url(),
            osp.join(parent.working_tree_dir, "existing-subrepository"),
        )
        sm2 = parent.create_submodule("nongit-file-submodule", clone.working_tree_dir)
        assert len(parent.submodules) == 2

        for _ in range(2):
            for init in (False, True):
                sm.update(init=init)
                sm2.update(init=init)
            # END for each init state
        # END for each iteration

        sm.move(sm.path + "_moved")
        sm2.move(sm2.path + "_moved")

        parent.index.commit("moved submodules")

        with sm.config_writer() as writer:
            writer.set_value("user.email", "example@example.com")
            writer.set_value("user.name", "me")
        smm = sm.module()
        fp = osp.join(smm.working_tree_dir, "empty-file")
        with open(fp, "w"):
            pass
        smm.git.add(Git.polish_url(fp))
        smm.git.commit(m="new file added")

        # Submodules are retrieved from the current commit's tree, therefore we can't
        # really get a new submodule object pointing to the new submodule commit.
        sm_too = parent.submodules["module_moved"]
        assert parent.head.commit.tree[sm.path].binsha == sm.binsha
        assert sm_too.binsha == sm.binsha, "cached submodule should point to the same commit as updated one"

        added_bies = parent.index.add([sm])  # Added base-index-entries.
        assert len(added_bies) == 1
        parent.index.commit("add same submodule entry")
        commit_sm = parent.head.commit.tree[sm.path]
        assert commit_sm.binsha == added_bies[0].binsha
        assert commit_sm.binsha == sm.binsha

        sm_too.binsha = sm_too.module().head.commit.binsha
        added_bies = parent.index.add([sm_too])
        assert len(added_bies) == 1
        parent.index.commit("add new submodule entry")
        commit_sm = parent.head.commit.tree[sm.path]
        assert commit_sm.binsha == added_bies[0].binsha
        assert commit_sm.binsha == sm_too.binsha
        assert sm_too.binsha != sm.binsha

    @pytest.mark.xfail(
        HIDE_WINDOWS_KNOWN_ERRORS,
        reason='"The process cannot access the file because it is being used by another process" on call to sm.move',
        raises=PermissionError,
    )
    @with_rw_directory
    def test_git_submodule_compatibility(self, rwdir):
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_path = join_path_native("submodules", "intermediate", "one")
        sm = parent.create_submodule("mymodules/myname", sm_path, url=self._small_repo_url())
        parent.index.commit("added submodule")

        def assert_exists(sm, value=True):
            assert sm.exists() == value
            assert sm.module_exists() == value

        # END assert_exists

        # As git is backwards compatible itself, it would still recognize what we do
        # here... unless we really muss it up. That's the only reason why the test is
        # still here...
        assert len(parent.git.submodule().splitlines()) == 1

        module_repo_path = osp.join(sm.module().working_tree_dir, ".git")
        assert module_repo_path.startswith(osp.join(parent.working_tree_dir, sm_path))
        if not sm._need_gitfile_submodules(parent.git):
            assert osp.isdir(module_repo_path)
            assert not sm.module().has_separate_working_tree()
        else:
            assert osp.isfile(module_repo_path)
            assert sm.module().has_separate_working_tree()
            assert find_submodule_git_dir(module_repo_path) is not None, "module pointed to by .git file must be valid"
        # END verify submodule 'style'

        # Test move.
        new_sm_path = join_path_native("submodules", "one")
        sm.move(new_sm_path)
        assert_exists(sm)

        # Add additional submodule level.
        csm = sm.module().create_submodule(
            "nested-submodule",
            join_path_native("nested-submodule", "working-tree"),
            url=self._small_repo_url(),
        )
        sm.module().index.commit("added nested submodule")
        sm_head_commit = sm.module().commit()
        assert_exists(csm)

        # Fails because there are new commits, compared to the remote we cloned from.
        self.assertRaises(InvalidGitRepositoryError, sm.remove, dry_run=True)
        assert_exists(sm)
        assert sm.module().commit() == sm_head_commit
        assert_exists(csm)

        # Rename nested submodule.
        # This name would move itself one level deeper - needs special handling
        # internally.
        new_name = csm.name + "/mine"
        assert csm.rename(new_name).name == new_name
        assert_exists(csm)
        assert csm.repo.is_dirty(index=True, working_tree=False), "index must contain changed .gitmodules file"
        csm.repo.index.commit("renamed module")

        # keep_going evaluation.
        rsm = parent.submodule_update()
        assert_exists(sm)
        assert_exists(csm)
        with csm.config_writer().set_value("url", "bar"):
            pass
        csm.repo.index.commit("Have to commit submodule change for algorithm to pick it up")
        assert csm.url == "bar"

        self.assertRaises(
            Exception,
            rsm.update,
            recursive=True,
            to_latest_revision=True,
            progress=prog,
        )
        assert_exists(csm)
        rsm.update(recursive=True, to_latest_revision=True, progress=prog, keep_going=True)

        # remove
        sm_module_path = sm.module().git_dir

        for dry_run in (True, False):
            sm.remove(dry_run=dry_run, force=True)
            assert_exists(sm, value=dry_run)
            assert osp.isdir(sm_module_path) == dry_run
        # END for each dry-run mode

    @with_rw_directory
    def test_ignore_non_submodule_file(self, rwdir):
        parent = git.Repo.init(rwdir)

        smp = osp.join(rwdir, "module")
        os.mkdir(smp)

        with open(osp.join(smp, "a"), "w", encoding="utf-8") as f:
            f.write("test\n")

        with open(osp.join(rwdir, ".gitmodules"), "w", encoding="utf-8") as f:
            f.write('[submodule "a"]\n')
            f.write("    path = module\n")
            f.write("    url = https://github.com/chaconinc/DbConnector\n")

        parent.git.add(Git.polish_url(osp.join(smp, "a")))
        parent.git.add(Git.polish_url(osp.join(rwdir, ".gitmodules")))

        parent.git.commit(message="test")

        assert len(parent.submodules) == 0

    @with_rw_directory
    def test_remove_norefs(self, rwdir):
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "mymodules/myname"
        sm = parent.create_submodule(sm_name, sm_name, url=self._small_repo_url())
        assert sm.exists()

        parent.index.commit("Added submodule")

        assert sm.repo is parent  # yoh was surprised since expected sm repo!!
        # So created a new instance for submodule.
        smrepo = git.Repo(osp.join(rwdir, "parent", sm.path))
        # Adding a remote without fetching so would have no references.
        smrepo.create_remote("special", "git@server-shouldnotmatter:repo.git")
        # And we should be able to remove it just fine.
        sm.remove()
        assert not sm.exists()

    @with_rw_directory
    def test_rename(self, rwdir):
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "mymodules/myname"
        sm = parent.create_submodule(sm_name, sm_name, url=self._small_repo_url())
        parent.index.commit("Added submodule")

        assert sm.rename(sm_name) is sm and sm.name == sm_name
        assert not sm.repo.is_dirty(index=True, working_tree=False, untracked_files=False)

        # This is needed to work around a PermissionError on Windows, resembling others,
        # except new in Python 3.12. (*Maybe* this could be due to changes in CPython's
        # garbage collector detailed in https://github.com/python/cpython/issues/97922.)
        if sys.platform == "win32" and sys.version_info >= (3, 12):
            gc.collect()

        new_path = "renamed/myname"
        assert sm.move(new_path).name == new_path

        new_sm_name = "shortname"
        assert sm.rename(new_sm_name) is sm
        assert sm.repo.is_dirty(index=True, working_tree=False, untracked_files=False)
        assert sm.exists()

        sm_mod = sm.module()
        if osp.isfile(osp.join(sm_mod.working_tree_dir, ".git")) == sm._need_gitfile_submodules(parent.git):
            assert sm_mod.git_dir.endswith(join_path_native(".git", "modules", new_sm_name))

    @with_rw_directory
    def test_branch_renames(self, rw_dir):
        # Set up initial sandbox:
        # The parent repo has one submodule, which has all the latest changes.
        source_url = self._small_repo_url()
        sm_source_repo = git.Repo.clone_from(source_url, osp.join(rw_dir, "sm-source"), b="master")
        parent_repo = git.Repo.init(osp.join(rw_dir, "parent"))
        sm = parent_repo.create_submodule(
            "mysubmodule",
            "subdir/submodule",
            sm_source_repo.working_tree_dir,
            branch="master",
        )
        parent_repo.index.commit("added submodule")
        assert sm.exists()

        # Create feature branch with one new commit in submodule source.
        sm_fb = sm_source_repo.create_head("feature")
        sm_fb.checkout()
        new_file = touch(osp.join(sm_source_repo.working_tree_dir, "new-file"))
        sm_source_repo.index.add([new_file])
        sm.repo.index.commit("added new file")

        # Change designated submodule checkout branch to the new upstream feature
        # branch.
        with sm.config_writer() as smcw:
            smcw.set_value("branch", sm_fb.name)
        assert sm.repo.is_dirty(index=True, working_tree=False)
        sm.repo.index.commit("changed submodule branch to '%s'" % sm_fb)

        # Verify submodule update with feature branch that leaves currently checked out
        # branch in it's past.
        sm_mod = sm.module()
        prev_commit = sm_mod.commit()
        assert sm_mod.head.ref.name == "master"
        assert parent_repo.submodule_update()
        assert sm_mod.head.ref.name == sm_fb.name
        assert sm_mod.commit() == prev_commit, "Without to_latest_revision, we don't change the commit"

        assert parent_repo.submodule_update(to_latest_revision=True)
        assert sm_mod.head.ref.name == sm_fb.name
        assert sm_mod.commit() == sm_fb.commit

        # Create new branch which is in our past, and thus seemingly unrelated to the
        # currently checked out one.
        # To make it even 'harder', we shall fork and create a new commit.
        sm_pfb = sm_source_repo.create_head("past-feature", commit="HEAD~20")
        sm_pfb.checkout()
        sm_source_repo.index.add([touch(osp.join(sm_source_repo.working_tree_dir, "new-file"))])
        sm_source_repo.index.commit("new file added, to past of '%r'" % sm_fb)

        # Change designated submodule checkout branch to a new commit in its own past.
        with sm.config_writer() as smcw:
            smcw.set_value("branch", sm_pfb.path)
        sm.repo.index.commit("changed submodule branch to '%s'" % sm_pfb)

        # Test submodule updates - must fail if submodule is dirty.
        touch(osp.join(sm_mod.working_tree_dir, "unstaged file"))
        # This doesn't fail as our own submodule binsha didn't change, and the reset is
        # only triggered if to_latest_revision is True.
        parent_repo.submodule_update(to_latest_revision=False)
        assert sm_mod.head.ref.name == sm_pfb.name, "should have been switched to past head"
        assert sm_mod.commit() == sm_fb.commit, "Head wasn't reset"

        self.assertRaises(RepositoryDirtyError, parent_repo.submodule_update, to_latest_revision=True)
        parent_repo.submodule_update(to_latest_revision=True, force_reset=True)
        assert sm_mod.commit() == sm_pfb.commit, "Now head should have been reset"
        assert sm_mod.head.ref.name == sm_pfb.name

    @skipUnless(sys.platform == "win32", "Specifically for Windows.")
    def test_to_relative_path_with_super_at_root_drive(self):
        class Repo:
            working_tree_dir = "D:\\"

        super_repo = Repo()
        submodule_path = "D:\\submodule_path"
        relative_path = Submodule._to_relative_path(super_repo, submodule_path)
        msg = '_to_relative_path should be "submodule_path" but was "%s"' % relative_path
        assert relative_path == "submodule_path", msg

    @pytest.mark.xfail(
        reason="for some unknown reason the assertion fails, even though it in fact is working in more common setup",
        raises=AssertionError,
    )
    @with_rw_directory
    def test_depth(self, rwdir):
        parent = git.Repo.init(osp.join(rwdir, "test_depth"))
        sm_name = "mymodules/myname"
        sm_depth = 1
        sm = parent.create_submodule(sm_name, sm_name, url=self._small_repo_url(), depth=sm_depth)
        self.assertEqual(len(list(sm.module().iter_commits())), sm_depth)

    @with_rw_directory
    def test_update_clone_multi_options_argument(self, rwdir):
        # Arrange
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "foo"
        sm_url = self._small_repo_url()
        sm_branch = "refs/heads/master"
        sm_hexsha = git.Repo(self._small_repo_url()).head.commit.hexsha
        sm = Submodule(
            parent,
            bytes.fromhex(sm_hexsha),
            name=sm_name,
            path=sm_name,
            url=sm_url,
            branch_path=sm_branch,
        )

        # Act
        sm.update(init=True, clone_multi_options=["--config core.eol=true"], allow_unsafe_options=True)

        # Assert
        sm_config = GitConfigParser(file_or_files=osp.join(parent.git_dir, "modules", sm_name, "config"))
        self.assertTrue(sm_config.get_value("core", "eol"))

    @with_rw_directory
    def test_update_no_clone_multi_options_argument(self, rwdir):
        # Arrange
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "foo"
        sm_url = self._small_repo_url()
        sm_branch = "refs/heads/master"
        sm_hexsha = git.Repo(self._small_repo_url()).head.commit.hexsha
        sm = Submodule(
            parent,
            bytes.fromhex(sm_hexsha),
            name=sm_name,
            path=sm_name,
            url=sm_url,
            branch_path=sm_branch,
        )

        # Act
        sm.update(init=True)

        # Assert
        sm_config = GitConfigParser(file_or_files=osp.join(parent.git_dir, "modules", sm_name, "config"))
        with self.assertRaises(cp.NoOptionError):
            sm_config.get_value("core", "eol")

    @with_rw_directory
    def test_add_clone_multi_options_argument(self, rwdir):
        # Arrange
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "foo"

        # Act
        Submodule.add(
            parent,
            sm_name,
            sm_name,
            url=self._small_repo_url(),
            clone_multi_options=["--config core.eol=true"],
            allow_unsafe_options=True,
        )

        # Assert
        sm_config = GitConfigParser(file_or_files=osp.join(parent.git_dir, "modules", sm_name, "config"))
        self.assertTrue(sm_config.get_value("core", "eol"))

    @with_rw_directory
    def test_add_no_clone_multi_options_argument(self, rwdir):
        # Arrange
        parent = git.Repo.init(osp.join(rwdir, "parent"))
        sm_name = "foo"

        # Act
        Submodule.add(parent, sm_name, sm_name, url=self._small_repo_url())

        # Assert
        sm_config = GitConfigParser(file_or_files=osp.join(parent.git_dir, "modules", sm_name, "config"))
        with self.assertRaises(cp.NoOptionError):
            sm_config.get_value("core", "eol")

    @with_rw_repo("HEAD")
    def test_submodule_add_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::/foo",
            ]
            for url in urls:
                with self.assertRaises(UnsafeProtocolError):
                    Submodule.add(rw_repo, "new", "new", url)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_add_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::/foo",
            ]
            for url in urls:
                # The URL will be allowed into the command, but the command will fail
                # since we don't have that protocol enabled in the Git config file.
                with self.assertRaises(GitCommandError):
                    Submodule.add(rw_repo, "new", "new", url, allow_unsafe_protocols=True)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_add_unsafe_options(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                f"--upload-pack='touch {tmp_file}'",
                f"-u 'touch {tmp_file}'",
                "--config=protocol.ext.allow=always",
                "-c protocol.ext.allow=always",
            ]
            for unsafe_option in unsafe_options:
                with self.assertRaises(UnsafeOptionError):
                    Submodule.add(rw_repo, "new", "new", str(tmp_dir), clone_multi_options=[unsafe_option])
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_add_unsafe_options_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                f"--upload-pack='touch {tmp_file}'",
                f"-u 'touch {tmp_file}'",
            ]
            for unsafe_option in unsafe_options:
                # The options will be allowed, but the command will fail.
                with self.assertRaises(GitCommandError):
                    Submodule.add(
                        rw_repo,
                        "new",
                        "new",
                        str(tmp_dir),
                        clone_multi_options=[unsafe_option],
                        allow_unsafe_options=True,
                    )
                assert not tmp_file.exists()

            unsafe_options = [
                "--config=protocol.ext.allow=always",
                "-c protocol.ext.allow=always",
            ]
            for unsafe_option in unsafe_options:
                with self.assertRaises(GitCommandError):
                    Submodule.add(
                        rw_repo,
                        "new",
                        "new",
                        str(tmp_dir),
                        clone_multi_options=[unsafe_option],
                        allow_unsafe_options=True,
                    )

    @with_rw_repo("HEAD")
    def test_submodule_update_unsafe_url(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::/foo",
            ]
            for url in urls:
                submodule = Submodule(rw_repo, b"\0" * 20, name="new", path="new", url=url)
                with self.assertRaises(UnsafeProtocolError):
                    submodule.update()
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_update_unsafe_url_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            urls = [
                f"ext::sh -c touch% {tmp_file}",
                "fd::/foo",
            ]
            for url in urls:
                submodule = Submodule(rw_repo, b"\0" * 20, name="new", path="new", url=url)
                # The URL will be allowed into the command, but the command will fail
                # since we don't have that protocol enabled in the Git config file.
                with self.assertRaises(GitCommandError):
                    submodule.update(allow_unsafe_protocols=True)
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_update_unsafe_options(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                f"--upload-pack='touch {tmp_file}'",
                f"-u 'touch {tmp_file}'",
                "--config=protocol.ext.allow=always",
                "-c protocol.ext.allow=always",
            ]
            submodule = Submodule(rw_repo, b"\0" * 20, name="new", path="new", url=str(tmp_dir))
            for unsafe_option in unsafe_options:
                with self.assertRaises(UnsafeOptionError):
                    submodule.update(clone_multi_options=[unsafe_option])
                assert not tmp_file.exists()

    @with_rw_repo("HEAD")
    def test_submodule_update_unsafe_options_allowed(self, rw_repo):
        with tempfile.TemporaryDirectory() as tdir:
            tmp_dir = Path(tdir)
            tmp_file = tmp_dir / "pwn"
            unsafe_options = [
                f"--upload-pack='touch {tmp_file}'",
                f"-u 'touch {tmp_file}'",
            ]
            submodule = Submodule(rw_repo, b"\0" * 20, name="new", path="new", url=str(tmp_dir))
            for unsafe_option in unsafe_options:
                # The options will be allowed, but the command will fail.
                with self.assertRaises(GitCommandError):
                    submodule.update(clone_multi_options=[unsafe_option], allow_unsafe_options=True)
                assert not tmp_file.exists()

            unsafe_options = [
                "--config=protocol.ext.allow=always",
                "-c protocol.ext.allow=always",
            ]
            submodule = Submodule(rw_repo, b"\0" * 20, name="new", path="new", url=str(tmp_dir))
            for unsafe_option in unsafe_options:
                with self.assertRaises(GitCommandError):
                    submodule.update(clone_multi_options=[unsafe_option], allow_unsafe_options=True)
