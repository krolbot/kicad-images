"""Source-only checks: no Docker, KiCad, desktop session or production files."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
import venv
from unittest.mock import patch
import zipfile

import pcm


class PCMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.home = self.root / "home"
        self.stock = self.root / "stock"
        self.stock.mkdir()
        for kind in ["sym", "fp"]:
            (self.stock / f"{kind}-lib-table").write_text(f"({kind}_lib_table\n)\n")
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config"),
                                          "XDG_DATA_HOME": str(self.home / ".local/share"),
                                          "XDG_CACHE_HOME": str(self.home / ".cache"),
                                          "KICAD_CACHE_HOME": "", "KICAD10_3RD_PARTY": "",
                                          "KICAD_CONFIG_HOME": "", "KICAD_DOCUMENTS_HOME": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def archive(self, extra=None, release=None):
        metadata = {"identifier": "org.example.test", "name": "Test fixture", "type": "plugin",
                    "versions": [{"version": "1.0.0", "status": "stable", "kicad_version": "10.0"}]}
        metadata["versions"][0].update(release or {})
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("metadata.json", json.dumps(metadata))
            archive.writestr("plugins/__init__.py", "# inert test fixture\n")
            archive.writestr("symbols/Test.kicad_sym", "test fixture, not a CAD project")
            if extra:
                archive.writestr(*extra)
        raw = stream.getvalue()
        spec = {"identifier": metadata["identifier"], "version": "1.0.0",
                "download_sha256": hashlib.sha256(raw).hexdigest()}
        return raw, spec

    def prepare(self):
        raw, spec = self.archive()
        entry = pcm.unpack(raw, spec, self.bundle)
        (self.bundle / "installed_packages.json").write_text(json.dumps({"packages": [entry]}))
        return entry

    def test_real_extraction_and_non_destructive_repeated_seed(self):
        self.prepare()
        pcm.seed(self.bundle, self.home, self.stock)
        data = self.home / ".local/share/kicad/10.0/3rdparty"
        plugin = data / "plugins/org_example_test/__init__.py"
        self.assertTrue(plugin.exists())
        table = self.home / ".config/kicad/10.0/sym-lib-table"
        self.assertIn("PCM_Test", table.read_text())
        registry = table.parent / "installed_packages.json"
        entries = json.loads(registry.read_text())["packages"]
        self.assertEqual(entries[0]["package"]["identifier"], "org.example.test")
        self.assertIsInstance(entries[0]["install_timestamp"], int)
        plugin.write_text("# user-edited content\n")
        original = registry.read_bytes()
        table.write_text("user-owned table, left byte-for-byte intact")
        pcm.seed(self.bundle, self.home, self.stock)
        self.assertEqual(registry.read_bytes(), original)
        self.assertEqual(plugin.read_text(), "# user-edited content\n")
        self.assertEqual(table.read_text(), "user-owned table, left byte-for-byte intact")

    def test_rejects_hash_mismatch(self):
        raw, spec = self.archive()
        spec["download_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Checksum"):
            pcm.unpack(raw, spec, self.bundle)
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_compatibility_and_patch_version_boundaries(self):
        for release in [{"kicad_version": "10.0.0", "kicad_version_max": "10.0"},
                        {"kicad_version": "9.0", "platforms": ["linux"]}]:
            self.assertTrue(pcm.compatible(release, "10.0.7"))
        for release in [{"kicad_version": "10.0.9"}, {"kicad_version_max": "9.99"},
                        {"platforms": ["windows"]}]:
            raw, spec = self.archive(release=release)
            with self.subTest(release=release), self.assertRaisesRegex(ValueError, "Incompatible"):
                pcm.unpack(raw, spec, self.bundle)
            self.assertEqual(list(self.bundle.iterdir()), [])

    def test_ipc_environment_relocation_and_preserving_user_settings(self):
        self.prepare()
        identifier = "org.example.runtime"
        source = self.bundle / "python-environments" / identifier
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(source)
        (self.bundle / "ipc_environments.json").write_text(json.dumps({
            identifier: {"identifier": "org.example.test", "version": "1.0.0"}}))
        (self.bundle / "compatibility_aliases.json").write_text(json.dumps(["KICAD8_3RD_PARTY"]))
        pcm.seed(self.bundle, self.home, self.stock)
        common_path = self.home / ".config/kicad/10.0/kicad_common.json"
        common = json.loads(common_path.read_text())
        self.assertTrue(common["api"]["enable_server"])
        self.assertEqual(common["api"]["interpreter_path"], "/usr/bin/python3")
        target = self.home / ".cache/kicad/10.0/python-environments" / identifier
        prefix = subprocess.check_output([str(target / "bin/python"), "-c", "import sys; print(sys.prefix)"], text=True).strip()
        self.assertEqual(prefix, str(target))
        common["api"] = {"enable_server": False, "interpreter_path": "/user/python"}
        common["environment"]["vars"]["KICAD8_3RD_PARTY"] = "/user/legacy"
        common_path.write_text(json.dumps(common))
        original = common_path.read_bytes()
        (target / "user.txt").write_text("preserve")
        pcm.seed(self.bundle, self.home, self.stock)
        self.assertEqual(common_path.read_bytes(), original)
        self.assertEqual((target / "user.txt").read_text(), "preserve")

    def test_rejects_traversal_absolute_and_symlink_members_before_extraction(self):
        link = zipfile.ZipInfo("plugins/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extra in [("plugins/../../escape", "bad"), ("/escape", "bad"),
                      ("plugins/dir\\escape", "bad"), (link, "/outside")]:
            with self.subTest(member=str(extra[0])):
                raw, spec = self.archive(extra)
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    pcm.unpack(raw, spec, self.bundle)
                self.assertEqual(list(self.bundle.iterdir()), [])

    def test_preserves_manual_unregistered_plugin_and_broken_registry(self):
        self.prepare()
        plugin = self.home / ".local/share/kicad/10.0/3rdparty/plugins/org_example_test"
        plugin.mkdir(parents=True)
        (plugin / "custom").write_text("keep")
        pcm.seed(self.bundle, self.home, self.stock)
        registry = self.home / ".config/kicad/10.0/installed_packages.json"
        self.assertEqual(json.loads(registry.read_text())["packages"], [])
        self.assertEqual((plugin / "custom").read_text(), "keep")
        registry.write_text("broken existing registry")
        with self.assertRaises(json.JSONDecodeError):
            pcm.seed(self.bundle, self.home, self.stock)
        self.assertEqual(registry.read_text(), "broken existing registry")

    def test_rejects_invalid_package_identifier(self):
        for identifier in ["../escape", "/absolute", "org/example", ".."]:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                pcm.disk_id(identifier)


class SourceContractTests(unittest.TestCase):
    def test_locked_bundle_and_unified_recipe(self):
        root = Path(__file__).parent
        lock = json.loads((root / "addons.lock.json").read_text())
        packages = lock["packages"]
        version = lock["kicad_version"]
        self.assertEqual(pcm.unpack.__defaults__, (version,))
        self.assertTrue(packages)
        ids = [p["identifier"] for p in packages]
        self.assertEqual(len(ids), len(set(ids)))
        for spec in packages:
            pcm.disk_id(spec["identifier"])
            self.assertRegex(spec["download_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(spec["download_url"].startswith("https://"))
            self.assertTrue(spec["version"])
            self.assertTrue(pcm.compatible(spec, version), spec["identifier"])
        recipe = (root / "Dockerfile").read_text()
        self.assertEqual(len(re.findall(r"^FROM ", recipe, re.M)), 1)
        self.assertIn("linuxserver/baseimage-selkies:ubunturesolute", recipe)
        self.assertIn(f"ARG KICAD_PACKAGE_VERSION={version}~ubuntu26.04.1", recipe)
        self.assertIn(f"KICAD_VERSION={version}", recipe)
        workflow = (root / ".github/workflows/build.yml").read_text()
        self.assertIn(f"ghcr.io/krolbot/kicad-gui:{version}-", workflow)
        self.assertIn("@sha256:", recipe)
        self.assertFalse((root / "Dockerfile.gui").exists())
        self.assertIn("requirements-mcp.txt", recipe)
        self.assertIn("pcm.py build", recipe)
        requirements = (root / "requirements-tools.txt").read_text().splitlines()
        expected = {"kikit", "pcbdraw", "skidl", "wireviz", "kicost", "kibot", "kiauto", "kidiff", "kibom", "setuptools"}
        self.assertEqual({line.split("==")[0] for line in requirements if line and not line.startswith("#")}, expected)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9_-]+==[0-9.]+", line)
                            for line in requirements if line and not line.startswith("#")))


if __name__ == "__main__":
    unittest.main()
