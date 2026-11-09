import importlib.util
import contextlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_gadget", ROOT / "scripts/build-gadget.py")
gadget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gadget)


class TargetTests(unittest.TestCase):
    def test_local_references_preserve_repository_and_tag(self):
        for name in ["gadget:dev", "demo/my-gadget:Dev_1", "demo/my__gadget--extra:v1"]:
            with self.subTest(name=name):
                expected = gadget.REGISTRY + "/" + name
                self.assertEqual(gadget.image_reference(name), expected)
                self.assertEqual(gadget.image_reference(expected), expected)

    def test_invalid_or_external_registry_targets_are_rejected(self):
        for name in ["gadget", "demo/Upper:dev", "demo/gadget:bad tag", "demo/gadget:$(whoami)",
                     "ghcr.io/demo/gadget:dev", "localhost/demo/gadget:dev", "../gadget:dev",
                     "https://registry.example/gadget:dev", "gadget:" + "x" * 129]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                gadget.image_reference(name)
        self.assertEqual(gadget.image_reference("gadget:" + "x" * 128),
                         gadget.REGISTRY + "/gadget:" + "x" * 128)

    def test_linux_node_architecture_ignores_windows_cordoned_and_unready_nodes(self):
        def node(architecture, operating_system="linux", ready=True, cordoned=False):
            return {"metadata": {"labels": {"kubernetes.io/os": operating_system,
                                            "kubernetes.io/arch": architecture}},
                    "spec": {"unschedulable": cordoned},
                    "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
        nodes = [node("amd64", "windows"), node("amd64", cordoned=True),
                 node("amd64", ready=False), node("s390x"), node("arm64")]
        self.assertEqual(gadget.node_architecture({"items": nodes}), "arm64")
        with self.assertRaises(ValueError):
            gadget.node_architecture({"items": nodes[:-1]})

    def test_checksum_must_match_the_requested_architecture_archive(self):
        digest = "ab" * 32
        self.assertEqual(gadget.sha256_digest(f"{digest.upper()} *ig-arm64.tgz\n", "ig-arm64.tgz"), digest)
        for contents in [f"{digest} ig-amd64.tgz", f"{'g' * 64} ig-arm64.tgz", ""]:
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                gadget.sha256_digest(contents, "ig-arm64.tgz")


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.tools = self.root / "ig.tar.gz"
        with tarfile.open(self.tools, "w:gz") as archive:
            member = tarfile.TarInfo("ig")
            member.size = 4
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(b"\x7fELF"))
        self.upload = self.root / "upload.tar"

    def test_no_global_context_is_used_without_explicit_cluster_selection(self):
        arguments = ["build-gadget.py", str(self.source), "--tag", "demo/test:dev"]
        with patch.object(sys, "argv", arguments), patch.dict(os.environ, {"KUBECONFIG": ""}), \
             patch.object(gadget.subprocess, "check_output") as cluster, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            gadget.main()
        self.assertNotEqual(error.exception.code, 0)
        cluster.assert_not_called()

    def test_cluster_credentials_must_be_outside_the_uploaded_tree(self):
        configuration = self.source / "kubeconfig"
        configuration.write_text("private cluster credentials")
        for explicit in (True, False):
            arguments = ["build-gadget.py", str(self.source), "--tag", "demo/test:dev"]
            if explicit:
                arguments += ["--kubeconfig", str(configuration)]
            with self.subTest(explicit=explicit), patch.object(sys, "argv", arguments), \
                 patch.dict(os.environ, {"KUBECONFIG": "" if explicit else str(configuration)}), \
                 patch.object(gadget.subprocess, "check_output") as cluster, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                gadget.main()
            self.assertNotEqual(error.exception.code, 0)
            cluster.assert_not_called()

    def symlink(self, target, path):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            self.skipTest(f"Host does not permit symlink fixtures: {error}")

    def test_internal_absolute_links_work_after_relocation_and_git_is_not_uploaded(self):
        headers = self.source / "include"
        headers.mkdir()
        (headers / "event.h").write_text("struct event;\n")
        self.symlink(headers / "event.h", self.source / "event.h")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("private checkout configuration")
        gadget.source_archive(self.source, self.tools, self.upload)
        relocated = self.root / "relocated"
        with tarfile.open(self.upload) as archive:
            archive.extractall(relocated, filter="data")
        link = relocated / "source" / "event.h"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), relocated / "source" / "include" / "event.h")
        self.assertFalse((relocated / "source" / ".git").exists())

    def test_source_symlinks_cannot_upload_files_outside_the_selected_tree(self):
        private = self.root / "private.h"
        private.write_text("not a build input")
        self.symlink(private, self.source / "event.h")
        with self.assertRaises(ValueError):
            gadget.source_archive(self.source, self.tools, self.upload)

    def test_directory_reparse_points_cannot_escape_source(self):
        external = self.root / "external"
        external.mkdir()
        self.symlink(external, self.source / "headers")
        member = tarfile.TarInfo("source/headers")
        member.type = tarfile.DIRTYPE
        with self.assertRaises(ValueError):
            gadget.source_member(self.source, member)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixtures require a Unix host")
    def test_named_pipes_are_rejected_instead_of_blocking_source_upload(self):
        os.mkfifo(self.source / "input")
        with self.assertRaises(ValueError):
            gadget.source_archive(self.source, self.tools, self.upload)

    def test_release_archive_links_are_not_accepted_as_executables(self):
        with tarfile.open(self.tools, "w:gz") as archive:
            member = tarfile.TarInfo("ig")
            member.type = tarfile.SYMTYPE
            member.linkname = "/bin/sh"
            archive.addfile(member)
        with self.assertRaises(ValueError):
            gadget.source_archive(self.source, self.tools, self.upload)


if __name__ == "__main__":
    unittest.main()
