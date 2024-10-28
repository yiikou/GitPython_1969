# Copyright (C) 2008, 2009 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

from itertools import chain
import os.path as osp
from pathlib import Path
import tempfile

from gitdb.exc import BadName

from git import (
    Commit,
    GitCommandError,
    GitConfigParser,
    Head,
    RefLog,
    Reference,
    RemoteReference,
    SymbolicReference,
    TagReference,
)
from git.objects.tag import TagObject
import git.refs as refs
from git.util import Actor

from test.lib import TestBase, with_rw_repo


class TestRefs(TestBase):
    def test_from_path(self):
        # Should be able to create any reference directly.
        for ref_type in (Reference, Head, TagReference, RemoteReference):
            for name in ("rela_name", "path/rela_name"):
                full_path = ref_type.to_full_path(name)
                instance = ref_type.from_path(self.rorepo, full_path)
                assert isinstance(instance, ref_type)
            # END for each name
        # END for each type

        # Invalid path.
        self.assertRaises(ValueError, TagReference, self.rorepo, "refs/invalid/tag")
        # Works without path check.
        TagReference(self.rorepo, "refs/invalid/tag", check_path=False)

    def test_tag_base(self):
        tag_object_refs = []
        for tag in self.rorepo.tags:
            assert "refs/tags" in tag.path
            assert tag.name
            assert isinstance(tag.commit, Commit)
            if tag.tag is not None:
                tag_object_refs.append(tag)
                tagobj = tag.tag
                # Have no dict.
                self.assertRaises(AttributeError, setattr, tagobj, "someattr", 1)
                assert isinstance(tagobj, TagObject)
                assert tagobj.tag == tag.name
                assert isinstance(tagobj.tagger, Actor)
                assert isinstance(tagobj.tagged_date, int)
                assert isinstance(tagobj.tagger_tz_offset, int)
                assert tagobj.message
                assert tag.object == tagobj
                # Can't assign the object.
                self.assertRaises(AttributeError, setattr, tag, "object", tagobj)
            # END if we have a tag object
        # END for tag in repo-tags
        assert tag_object_refs
        assert isinstance(self.rorepo.tags["0.1.5"], TagReference)

    def test_tags_author(self):
        tag = self.rorepo.tags[0]
        tagobj = tag.tag
        assert isinstance(tagobj.tagger, Actor)
        tagger_name = tagobj.tagger.name
        assert tagger_name == "Michael Trier"

    def test_tags(self):
        # Tag refs can point to tag objects or to commits.
        s = set()
        ref_count = 0
        for ref in chain(self.rorepo.tags, self.rorepo.heads):
            ref_count += 1
            assert isinstance(ref, refs.Reference)
            assert str(ref) == ref.name
            assert repr(ref)
            assert ref == ref
            assert not ref != ref
            s.add(ref)
        # END for each ref
        assert len(s) == ref_count
        assert len(s | s) == ref_count

    @with_rw_repo("HEAD", bare=False)
    def test_heads(self, rwrepo):
        for head in rwrepo.heads:
            assert head.name
            assert head.path
            assert "refs/heads" in head.path
            prev_object = head.object
            cur_object = head.object
            assert prev_object == cur_object  # Represent the same git object...
            assert prev_object is not cur_object  # ...but are different instances.

            with head.config_writer() as writer:
                tv = "testopt"
                writer.set_value(tv, 1)
                assert writer.get_value(tv) == 1
            assert head.config_reader().get_value(tv) == 1
            with head.config_writer() as writer:
                writer.remove_option(tv)

            # After the clone, we might still have a tracking branch setup.
            head.set_tracking_branch(None)
            assert head.tracking_branch() is None
            remote_ref = rwrepo.remotes[0].refs[0]
            assert head.set_tracking_branch(remote_ref) is head
            assert head.tracking_branch() == remote_ref
            head.set_tracking_branch(None)
            assert head.tracking_branch() is None

            special_name = "feature#123"
            special_name_remote_ref = SymbolicReference.create(rwrepo, "refs/remotes/origin/%s" % special_name)
            gp_tracking_branch = rwrepo.create_head("gp_tracking#123")
            special_name_remote_ref = rwrepo.remotes[0].refs[special_name]  # Get correct type.
            gp_tracking_branch.set_tracking_branch(special_name_remote_ref)
            TBranch = gp_tracking_branch.tracking_branch()
            if TBranch is not None:
                assert TBranch.path == special_name_remote_ref.path

            git_tracking_branch = rwrepo.create_head("git_tracking#123")
            rwrepo.git.branch("-u", special_name_remote_ref.name, git_tracking_branch.name)
            TBranch = gp_tracking_branch.tracking_branch()
            if TBranch is not None:
                assert TBranch.name == special_name_remote_ref.name
        # END for each head

        # Verify REFLOG gets altered.
        head = rwrepo.head
        cur_head = head.ref
        cur_commit = cur_head.commit
        pcommit = cur_head.commit.parents[0].parents[0]
        hlog_len = len(head.log())
        blog_len = len(cur_head.log())
        assert head.set_reference(pcommit, "detached head") is head
        # One new log-entry.
        thlog = head.log()
        assert len(thlog) == hlog_len + 1
        assert thlog[-1].oldhexsha == cur_commit.hexsha
        assert thlog[-1].newhexsha == pcommit.hexsha

        # The ref didn't change though.
        assert len(cur_head.log()) == blog_len

        # head changes once again, cur_head doesn't change.
        head.set_reference(cur_head, "reattach head")
        assert len(head.log()) == hlog_len + 2
        assert len(cur_head.log()) == blog_len

        # Adjusting the head-ref also adjust the head, so both reflogs are altered.
        cur_head.set_commit(pcommit, "changing commit")
        assert len(cur_head.log()) == blog_len + 1
        assert len(head.log()) == hlog_len + 3

        # With automatic dereferencing.
        assert head.set_commit(cur_commit, "change commit once again") is head
        assert len(head.log()) == hlog_len + 4
        assert len(cur_head.log()) == blog_len + 2

        # A new branch has just a single entry.
        other_head = Head.create(rwrepo, "mynewhead", pcommit, logmsg="new head created")
        log = other_head.log()
        assert len(log) == 1
        assert log[0].oldhexsha == pcommit.NULL_HEX_SHA
        assert log[0].newhexsha == pcommit.hexsha

    @with_rw_repo("HEAD", bare=False)
    def test_set_tracking_branch_with_import(self, rwrepo):
        # Prepare included config file.
        included_config = osp.join(rwrepo.git_dir, "config.include")
        with GitConfigParser(included_config, read_only=False) as writer:
            writer.set_value("test", "value", "test")
        assert osp.exists(included_config)

        with rwrepo.config_writer() as writer:
            writer.set_value("include", "path", included_config)

        for head in rwrepo.heads:
            head.set_tracking_branch(None)
            assert head.tracking_branch() is None
            remote_ref = rwrepo.remotes[0].refs[0]
            assert head.set_tracking_branch(remote_ref) is head
            assert head.tracking_branch() == remote_ref
            head.set_tracking_branch(None)
            assert head.tracking_branch() is None

    def test_refs(self):
        types_found = set()
        for ref in self.rorepo.refs:
            types_found.add(type(ref))
        assert len(types_found) >= 3

    def test_is_valid(self):
        assert not Reference(self.rorepo, "refs/doesnt/exist").is_valid()
        assert self.rorepo.head.is_valid()
        assert self.rorepo.head.reference.is_valid()
        assert not SymbolicReference(self.rorepo, "hellothere").is_valid()

    def test_orig_head(self):
        assert type(self.rorepo.head.orig_head()) is SymbolicReference

    @with_rw_repo("0.1.6")
    def test_head_checkout_detached_head(self, rw_repo):
        res = rw_repo.remotes.origin.refs.master.checkout()
        assert isinstance(res, SymbolicReference)
        assert res.name == "HEAD"

    @with_rw_repo("0.1.6")
    def test_head_reset(self, rw_repo):
        cur_head = rw_repo.head
        old_head_commit = cur_head.commit
        new_head_commit = cur_head.ref.commit.parents[0]
        cur_head.reset(new_head_commit, index=True)  # index only
        assert cur_head.reference.commit == new_head_commit

        self.assertRaises(ValueError, cur_head.reset, new_head_commit, index=False, working_tree=True)
        new_head_commit = new_head_commit.parents[0]
        cur_head.reset(new_head_commit, index=True, working_tree=True)  # index + wt
        assert cur_head.reference.commit == new_head_commit

        # Paths - make sure we have something to do.
        rw_repo.index.reset(old_head_commit.parents[0])
        cur_head.reset(cur_head, paths="test")
        cur_head.reset(new_head_commit, paths="lib")
        # Hard resets with paths don't work; it's all or nothing.
        self.assertRaises(
            GitCommandError,
            cur_head.reset,
            new_head_commit,
            working_tree=True,
            paths="lib",
        )

        # We can do a mixed reset, and then checkout from the index though.
        cur_head.reset(new_head_commit)
        rw_repo.index.checkout(["lib"], force=True)

        # Now that we have a write write repo, change the HEAD reference - it's like
        # "git-reset --soft".
        heads = rw_repo.heads
        assert heads
        for head in heads:
            cur_head.reference = head
            assert cur_head.reference == head
            assert isinstance(cur_head.reference, Head)
            assert cur_head.commit == head.commit
            assert not cur_head.is_detached
        # END for each head

        # Detach.
        active_head = heads[0]
        curhead_commit = active_head.commit
        cur_head.reference = curhead_commit
        assert cur_head.commit == curhead_commit
        assert cur_head.is_detached
        self.assertRaises(TypeError, getattr, cur_head, "reference")

        # Tags are references, hence we can point to them.
        some_tag = rw_repo.tags[0]
        cur_head.reference = some_tag
        assert not cur_head.is_detached
        assert cur_head.commit == some_tag.commit
        assert isinstance(cur_head.reference, TagReference)

        # Put HEAD back to a real head, otherwise everything else fails.
        cur_head.reference = active_head

        # Type check.
        self.assertRaises(ValueError, setattr, cur_head, "reference", "that")

        # Head handling.
        commit = "HEAD"
        prev_head_commit = cur_head.commit
        for count, new_name in enumerate(("my_new_head", "feature/feature1")):
            actual_commit = commit + "^" * count
            new_head = Head.create(rw_repo, new_name, actual_commit)
            assert new_head.is_detached
            assert cur_head.commit == prev_head_commit
            assert isinstance(new_head, Head)
            # Already exists, but has the same value, so it's fine.
            Head.create(rw_repo, new_name, new_head.commit)

            # It's not fine with a different value.
            self.assertRaises(OSError, Head.create, rw_repo, new_name, new_head.commit.parents[0])

            # Force it.
            new_head = Head.create(rw_repo, new_name, actual_commit, force=True)
            old_path = new_head.path
            old_name = new_head.name

            assert new_head.rename("hello").name == "hello"
            assert new_head.rename("hello/world").name == "hello/world"
            assert new_head.rename(old_name).name == old_name and new_head.path == old_path

            # Rename with force.
            tmp_head = Head.create(rw_repo, "tmphead")
            self.assertRaises(GitCommandError, tmp_head.rename, new_head)
            tmp_head.rename(new_head, force=True)
            assert tmp_head == new_head and tmp_head.object == new_head.object

            logfile = RefLog.path(tmp_head)
            assert osp.isfile(logfile)
            Head.delete(rw_repo, tmp_head)
            # Deletion removes the log as well.
            assert not osp.isfile(logfile)
            heads = rw_repo.heads
            assert tmp_head not in heads and new_head not in heads
            # Force on deletion testing would be missing here, code looks okay though. ;)
        # END for each new head name
        self.assertRaises(TypeError, RemoteReference.create, rw_repo, "some_name")

        # Tag ref.
        tag_name = "5.0.2"
        TagReference.create(rw_repo, tag_name)
        self.assertRaises(GitCommandError, TagReference.create, rw_repo, tag_name)
        light_tag = TagReference.create(rw_repo, tag_name, "HEAD~1", force=True)
        assert isinstance(light_tag, TagReference)
        assert light_tag.name == tag_name
        assert light_tag.commit == cur_head.commit.parents[0]
        assert light_tag.tag is None

        # Tag with tag object.
        other_tag_name = "releases/1.0.2RC"
        msg = "my mighty tag\nsecond line"
        obj_tag = TagReference.create(rw_repo, other_tag_name, message=msg)
        assert isinstance(obj_tag, TagReference)
        assert obj_tag.name == other_tag_name
        assert obj_tag.commit == cur_head.commit
        assert obj_tag.tag is not None

        TagReference.delete(rw_repo, light_tag, obj_tag)
        tags = rw_repo.tags
        assert light_tag not in tags and obj_tag not in tags

        # Remote deletion.
        remote_refs_so_far = 0
        remotes = rw_repo.remotes
        assert remotes
        for remote in remotes:
            refs = remote.refs

            # If a HEAD exists, it must be deleted first. Otherwise it might end up
            # pointing to an invalid ref it the ref was deleted before.
            remote_head_name = "HEAD"
            if remote_head_name in refs:
                RemoteReference.delete(rw_repo, refs[remote_head_name])
                del refs[remote_head_name]
            # END handle HEAD deletion

            RemoteReference.delete(rw_repo, *refs)
            remote_refs_so_far += len(refs)
            for ref in refs:
                assert ref.remote_name == remote.name
        # END for each ref to delete
        assert remote_refs_so_far

        for remote in remotes:
            # Remotes without references should produce an empty list.
            self.assertEqual(remote.refs, [])
        # END for each remote

        # Change where the active head points to.
        if cur_head.is_detached:
            cur_head.reference = rw_repo.heads[0]

        head = cur_head.reference
        old_commit = head.commit
        head.commit = old_commit.parents[0]
        assert head.commit == old_commit.parents[0]
        assert head.commit == cur_head.commit
        head.commit = old_commit

        # Setting a non-commit as commit fails, but succeeds as object.
        head_tree = head.commit.tree
        self.assertRaises(ValueError, setattr, head, "commit", head_tree)
        assert head.commit == old_commit  # And the ref did not change.
        # We allow heads to point to any object.
        head.object = head_tree
        assert head.object == head_tree
        # Cannot query tree as commit.
        self.assertRaises(TypeError, getattr, head, "commit")

        # Set the commit directly using the head. This would never detach the head.
        assert not cur_head.is_detached
        head.object = old_commit
        cur_head.reference = head.commit
        assert cur_head.is_detached
        parent_commit = head.commit.parents[0]
        assert cur_head.is_detached
        cur_head.commit = parent_commit
        assert cur_head.is_detached and cur_head.commit == parent_commit

        cur_head.reference = head
        assert not cur_head.is_detached
        cur_head.commit = parent_commit
        assert not cur_head.is_detached
        assert head.commit == parent_commit

        # Test checkout.
        active_branch = rw_repo.active_branch
        for head in rw_repo.heads:
            checked_out_head = head.checkout()
            assert checked_out_head == head
        # END for each head to checkout

        # Check out with branch creation.
        new_head = active_branch.checkout(b="new_head")
        assert active_branch != rw_repo.active_branch
        assert new_head == rw_repo.active_branch

        # Checkout with force as we have a changed a file.
        # Clear file.
        open(new_head.commit.tree.blobs[-1].abspath, "w").close()
        assert len(new_head.commit.diff(None))

        # Create a new branch that is likely to touch the file we changed.
        far_away_head = rw_repo.create_head("far_head", "HEAD~100")
        self.assertRaises(GitCommandError, far_away_head.checkout)
        assert active_branch == active_branch.checkout(force=True)
        assert rw_repo.head.reference != far_away_head

        # Test reference creation.
        partial_ref = "sub/ref"
        full_ref = "refs/%s" % partial_ref
        ref = Reference.create(rw_repo, partial_ref)
        assert ref.path == full_ref
        assert ref.object == rw_repo.head.commit

        self.assertRaises(OSError, Reference.create, rw_repo, full_ref, "HEAD~20")
        # It works if it is at the same spot though and points to the same reference.
        assert Reference.create(rw_repo, full_ref, "HEAD").path == full_ref
        Reference.delete(rw_repo, full_ref)

        # Recreate the reference using a full_ref.
        ref = Reference.create(rw_repo, full_ref)
        assert ref.path == full_ref
        assert ref.object == rw_repo.head.commit

        # Recreate using force.
        ref = Reference.create(rw_repo, partial_ref, "HEAD~1", force=True)
        assert ref.path == full_ref
        assert ref.object == rw_repo.head.commit.parents[0]

        # Rename it.
        orig_obj = ref.object
        for name in ("refs/absname", "rela_name", "feature/rela_name"):
            ref_new_name = ref.rename(name)
            assert isinstance(ref_new_name, Reference)
            assert name in ref_new_name.path
            assert ref_new_name.object == orig_obj
            assert ref_new_name == ref
        # END for each name type

        # References that don't exist trigger an error if we want to access them.
        self.assertRaises(ValueError, getattr, Reference(rw_repo, "refs/doesntexist"), "commit")

        # Exists, fail unless we force.
        ex_ref_path = far_away_head.path
        self.assertRaises(OSError, ref.rename, ex_ref_path)
        # If it points to the same commit it works.
        far_away_head.commit = ref.commit
        ref.rename(ex_ref_path)
        assert ref.path == ex_ref_path and ref.object == orig_obj
        assert ref.rename(ref.path).path == ex_ref_path  # rename to same name

        # Create symbolic refs.
        symref_path = "symrefs/sym"
        symref = SymbolicReference.create(rw_repo, symref_path, cur_head.reference)
        assert symref.path == symref_path
        assert symref.reference == cur_head.reference

        self.assertRaises(
            OSError,
            SymbolicReference.create,
            rw_repo,
            symref_path,
            cur_head.reference.commit,
        )
        # It works if the new ref points to the same reference.
        assert SymbolicReference.create(rw_repo, symref.path, symref.reference).path == symref.path
        SymbolicReference.delete(rw_repo, symref)
        # Would raise if the symref wouldn't have been deleted (probably).
        symref = SymbolicReference.create(rw_repo, symref_path, cur_head.reference)

        # Test symbolic references which are not at default locations like HEAD or
        # FETCH_HEAD - they may also be at spots in refs of course.
        symbol_ref_path = "refs/symbol_ref"
        symref = SymbolicReference(rw_repo, symbol_ref_path)
        assert symref.path == symbol_ref_path
        symbol_ref_abspath = osp.join(rw_repo.git_dir, symref.path)

        # Set it.
        symref.reference = new_head
        assert symref.reference == new_head
        assert osp.isfile(symbol_ref_abspath)
        assert symref.commit == new_head.commit

        for name in ("absname", "folder/rela_name"):
            symref_new_name = symref.rename(name)
            assert isinstance(symref_new_name, SymbolicReference)
            assert name in symref_new_name.path
            assert symref_new_name.reference == new_head
            assert symref_new_name == symref
            assert not symref.is_detached
        # END for each ref

        # Create a new non-head ref just to be sure we handle it even if packed.
        Reference.create(rw_repo, full_ref)

        # Test ref listing - make sure we have packed refs.
        rw_repo.git.pack_refs(all=True, prune=True)
        heads = rw_repo.heads
        assert heads
        assert new_head in heads
        assert active_branch in heads
        assert rw_repo.tags

        # We should be able to iterate all symbolic refs as well - in that case we
        # should expect only symbolic references to be returned.
        for symref in SymbolicReference.iter_items(rw_repo):
            assert not symref.is_detached

        # When iterating references, we can get references and symrefs when deleting all
        # refs, I'd expect them to be gone! Even from the packed ones.
        # For this to work, we must not be on any branch.
        rw_repo.head.reference = rw_repo.head.commit
        deleted_refs = set()
        for ref in Reference.iter_items(rw_repo):
            if ref.is_detached:
                ref.delete(rw_repo, ref)
                deleted_refs.add(ref)
            # END delete ref
        # END for each ref to iterate and to delete
        assert deleted_refs

        for ref in Reference.iter_items(rw_repo):
            if ref.is_detached:
                assert ref not in deleted_refs
        # END for each ref

        # Reattach head - head will not be returned if it is not a symbolic ref.
        rw_repo.head.reference = Head.create(rw_repo, "master")

        # At least the head should still exist.
        assert osp.isfile(osp.join(rw_repo.git_dir, "HEAD"))
        refs = list(SymbolicReference.iter_items(rw_repo))
        assert len(refs) == 1

        # Test creation of new refs from scratch.
        for path in ("basename", "dir/somename", "dir2/subdir/basename"):
            # REFERENCES
            ############
            fpath = Reference.to_full_path(path)
            ref_fp = Reference.from_path(rw_repo, fpath)
            assert not ref_fp.is_valid()
            ref = Reference(rw_repo, fpath)
            assert ref == ref_fp

            # Can be created by assigning a commit.
            ref.commit = rw_repo.head.commit
            assert ref.is_valid()

            # If the assignment raises, the ref doesn't exist.
            Reference.delete(ref.repo, ref.path)
            assert not ref.is_valid()
            self.assertRaises(ValueError, setattr, ref, "commit", "nonsense")
            assert not ref.is_valid()

            # I am sure I had my reason to make it a class method at first, but now it
            # doesn't make so much sense anymore, want an instance method as well. See:
            # http://byronimo.lighthouseapp.com/projects/51787-gitpython/tickets/27
            Reference.delete(ref.repo, ref.path)
            assert not ref.is_valid()

            ref.object = rw_repo.head.commit
            assert ref.is_valid()

            Reference.delete(ref.repo, ref.path)
            assert not ref.is_valid()
            self.assertRaises(ValueError, setattr, ref, "object", "nonsense")
            assert not ref.is_valid()

        # END for each path

    @with_rw_repo("0.1.6")
    def test_tag_message(self, rw_repo):
        tag_ref = TagReference.create(rw_repo, "test-message-1", message="test")
        assert tag_ref.tag.message == "test"

        tag_ref = TagReference.create(rw_repo, "test-message-2", logmsg="test")
        assert tag_ref.tag.message == "test"

        tag_ref = TagReference.create(
            rw_repo,
            "test-message-3",
            # Logmsg should take precedence over "message".
            message="test1",
            logmsg="test2",
        )
        assert tag_ref.tag.message == "test2"

    def test_dereference_recursive(self):
        # For now, just test the HEAD.
        assert SymbolicReference.dereference_recursive(self.rorepo, "HEAD")

    def test_reflog(self):
        assert isinstance(self.rorepo.heads.master.log(), RefLog)

    def test_refs_outside_repo(self):
        # Create a file containing a valid reference outside the repository. Attempting
        # to access it should raise an exception, due to it containing a parent
        # directory reference ('..'). This tests for CVE-2023-41040.
        git_dir = Path(self.rorepo.git_dir)
        repo_parent_dir = git_dir.parent.parent
        with tempfile.NamedTemporaryFile(dir=repo_parent_dir) as ref_file:
            ref_file.write(b"91b464cd624fe22fbf54ea22b85a7e5cca507cfe")
            ref_file.flush()
            ref_file_name = Path(ref_file.name).name
            self.assertRaises(BadName, self.rorepo.commit, f"../../{ref_file_name}")

    def test_validity_ref_names(self):
        """Ensure ref names are checked for validity.

        This is based on the rules specified in:
        https://git-scm.com/docs/git-check-ref-format/#_description
        """
        check_ref = SymbolicReference._check_ref_name_valid

        # Rule 1
        self.assertRaises(ValueError, check_ref, ".ref/begins/with/dot")
        self.assertRaises(ValueError, check_ref, "ref/component/.begins/with/dot")
        self.assertRaises(ValueError, check_ref, "ref/ends/with/a.lock")
        self.assertRaises(ValueError, check_ref, "ref/component/ends.lock/with/period_lock")

        # Rule 2
        check_ref("valid_one_level_refname")

        # Rule 3
        self.assertRaises(ValueError, check_ref, "ref/contains/../double/period")

        # Rule 4
        for c in " ~^:":
            self.assertRaises(ValueError, check_ref, f"ref/contains/invalid{c}/character")
        for code in range(0, 32):
            self.assertRaises(ValueError, check_ref, f"ref/contains/invalid{chr(code)}/ASCII/control_character")
        self.assertRaises(ValueError, check_ref, f"ref/contains/invalid{chr(127)}/ASCII/control_character")

        # Rule 5
        for c in "*?[":
            self.assertRaises(ValueError, check_ref, f"ref/contains/invalid{c}/character")

        # Rule 6
        self.assertRaises(ValueError, check_ref, "/ref/begins/with/slash")
        self.assertRaises(ValueError, check_ref, "ref/ends/with/slash/")
        self.assertRaises(ValueError, check_ref, "ref/contains//double/slash/")

        # Rule 7
        self.assertRaises(ValueError, check_ref, "ref/ends/with/dot.")

        # Rule 8
        self.assertRaises(ValueError, check_ref, "ref/contains@{/at_brace")

        # Rule 9
        self.assertRaises(ValueError, check_ref, "@")

        # Rule 10
        self.assertRaises(ValueError, check_ref, "ref/contain\\s/backslash")

        # Valid reference name should not raise.
        check_ref("valid/ref/name")
