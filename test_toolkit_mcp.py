"""Runnable source-level boundaries; real KiCad engines are exercised by image smoke."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import toolkit_mcp as toolkit
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource


class ToolkitBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.board = self.project / "fixture.kicad_pcb"
        self.board.write_text("# inert validation fixture, not a usable PCB\n")
        override = patch.object(toolkit, "PROJECT_ROOT", self.root)
        override.start()
        self.addCleanup(override.stop)

    def test_path_escape_and_existing_output_rejected_before_backend(self):
        outside = self.root.parent / "outside.kicad_pcb"
        link = self.root / "linked.kicad_pcb"
        link.symlink_to(outside)
        for value in ["relative.kicad_pcb", str(outside), str(link)]:
            with self.subTest(path=value), self.assertRaises(ToolError):
                toolkit._path(value, ".kicad_pcb")
        existing = self.root / "already-exists"
        existing.mkdir()
        sentinel = existing / "keep"
        sentinel.write_text("user content")
        with self.assertRaisesRegex(ToolError, "already exists"):
            toolkit.generate_interactive_bom(str(self.board), str(existing))
        self.assertEqual(sentinel.read_text(), "user content")

    def test_project_file_download_is_exact_bounded_and_symlink_free(self):
        payload = b"\x00KiCad MCP download\xff\n"
        source = self.project / "space name.bin"
        source.write_bytes(payload)
        result = toolkit.download_project_file(str(source))
        self.assertIsInstance(result.resource, BlobResourceContents)
        resource = result.resource
        assert isinstance(resource, BlobResourceContents)
        self.assertEqual(base64.b64decode(resource.blob, validate=True), payload)
        self.assertEqual(resource.uri, "kicad-project:///project/space%20name.bin")
        self.assertEqual(resource.mime_type, "application/octet-stream")
        self.assertEqual(result.meta, resource.meta)
        assert result.meta is not None
        self.assertEqual(result.meta["path"], str(source))
        self.assertEqual(result.meta["bytes"], len(payload))
        self.assertEqual(result.meta["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.meta["encoding"], "base64")

        wire_result = asyncio.run(toolkit.mcp.call_tool("download_project_file", {"path": str(source)}))
        assert isinstance(wire_result, CallToolResult)
        self.assertFalse(wire_result.is_error)
        self.assertEqual(len(wire_result.content), 1)
        self.assertIsInstance(wire_result.content[0], EmbeddedResource)
        wire_block = wire_result.content[0]
        assert isinstance(wire_block, EmbeddedResource)
        wire_resource = wire_block.resource
        assert isinstance(wire_resource, BlobResourceContents)
        self.assertEqual(base64.b64decode(wire_resource.blob, validate=True), payload)

        outside = self.root.parent / "download-outside.bin"
        outside.write_bytes(payload)
        self.addCleanup(outside.unlink, missing_ok=True)
        file_link = self.project / "file-link.bin"
        file_link.symlink_to(source)
        directory_link = self.root / "directory-link"
        directory_link.symlink_to(self.project, target_is_directory=True)
        rejected = [
            "relative.bin",
            str(outside),
            str(self.root),
            str(self.project),
            str(file_link),
            str(directory_link / source.name),
            str(self.project / ".." / "project" / source.name),
        ]
        for value in rejected:
            with self.subTest(path=value), self.assertRaises(ToolError):
                toolkit.download_project_file(value)

        with patch.object(toolkit, "MAX_PROJECT_FILE_BYTES", len(payload) - 1):
            with self.assertRaisesRegex(ToolError, "download limit"):
                toolkit.download_project_file(str(source))

    def test_project_file_lifecycle_is_guarded_and_symlink_free(self):
        payload = b"project upload\x00\xff"
        uploaded = self.project / "uploaded.bin"
        created = toolkit.upload_project_file(str(uploaded), base64.b64encode(payload).decode())
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(created["sha256"], digest)
        self.assertEqual(uploaded.read_bytes(), payload)
        self.assertEqual(toolkit.stat_project_path(str(uploaded))["sha256"], digest)
        self.assertEqual(toolkit.list_project_files(str(self.project))["count"], 2)

        with self.assertRaisesRegex(ToolError, "changed"):
            toolkit.upload_project_file(str(uploaded), base64.b64encode(b"new").decode(), "0" * 64)
        replaced = toolkit.upload_project_file(str(uploaded), base64.b64encode(b"new").decode(), digest)
        replacement_digest = hashlib.sha256(b"new").hexdigest()
        self.assertEqual(replaced["sha256"], replacement_digest)

        copy = self.project / "copy.bin"
        toolkit.copy_project_path(str(uploaded), str(copy), replacement_digest)
        moved = self.project / "moved.bin"
        toolkit.move_project_path(str(copy), str(moved), replacement_digest)
        self.assertFalse(copy.exists())
        self.assertEqual(moved.read_bytes(), b"new")

        archive = self.project / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("nested/file.txt", "archive")
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        extracted = self.project / "extracted"
        result = toolkit.extract_project_archive(str(archive), str(extracted), archive_digest)
        self.assertEqual(result["entry_count"], 2)
        self.assertEqual(result["file_count"], 1)
        self.assertEqual((extracted / "nested/file.txt").read_text(), "archive")

        generated_archive = self.project / "generated.zip"
        generated = toolkit.create_project_archive(
            str(extracted), str(generated_archive), str(result["sha256"])
        )
        self.assertEqual(generated["file_count"], 1)
        self.assertEqual(generated["sha256"], hashlib.sha256(generated_archive.read_bytes()).hexdigest())
        with zipfile.ZipFile(generated_archive) as bundle:
            self.assertEqual(bundle.read("extracted/nested/file.txt"), b"archive")

        unsafe = self.project / "unsafe.zip"
        with zipfile.ZipFile(unsafe, "w") as bundle:
            bundle.writestr("../escape", "no")
        with self.assertRaisesRegex(ToolError, "Unsafe archive path"):
            toolkit.extract_project_archive(
                str(unsafe), str(self.project / "unsafe-output"), hashlib.sha256(unsafe.read_bytes()).hexdigest()
            )
        self.assertFalse((self.project / "unsafe-output").exists())

        link = self.project / "link"
        link.symlink_to(uploaded)
        with self.assertRaisesRegex(ToolError, "symlink"):
            toolkit.stat_project_path(str(link))
        link.unlink()
        toolkit.delete_project_path(str(moved), replacement_digest, True)
        extracted_digest = result["sha256"]
        assert isinstance(extracted_digest, str)
        toolkit.delete_project_path(str(extracted), extracted_digest, True, recursive=True)
        self.assertFalse(moved.exists())
        self.assertFalse(extracted.exists())

    def test_upload_pins_parent_descriptor_across_symlink_swap(self):
        parent = self.project / "race-parent"
        parent.mkdir()
        detached = self.project / "race-parent-detached"
        outside = self.root.parent / f"outside-{self.root.name}"
        outside.mkdir()
        self.addCleanup(lambda: outside.exists() and shutil.rmtree(outside))
        original_write = toolkit._write_temp_at

        def swap_parent(parent_fd, target_name, content, label):
            parent.rename(detached)
            parent.symlink_to(outside, target_is_directory=True)
            return original_write(parent_fd, target_name, content, label)

        payload = b"descriptor pinned"
        with patch.object(toolkit, "_write_temp_at", side_effect=swap_parent):
            result = toolkit.upload_project_file(
                str(parent / "escaped.bin"), base64.b64encode(payload).decode("ascii")
            )
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual((detached / "escaped.bin").read_bytes(), payload)
        self.assertFalse((outside / "escaped.bin").exists())
        parent.unlink()
        detached.rename(parent)

    def test_upstream_paths_are_project_bounded_and_mutations_require_cas(self):
        outside = self.root.parent / "outside-upstream"
        with self.assertRaisesRegex(ToolError, "inside /projects"):
            asyncio.run(toolkit.mcp.call_tool("create_project", {"directory": str(outside), "name": "escape"}))
        self.assertFalse(outside.exists())

        created = asyncio.run(toolkit.mcp.call_tool(
            "create_project", {"directory": str(self.project), "name": "guarded"}
        ))
        assert isinstance(created, CallToolResult)
        self.assertFalse(created.is_error)
        schematic = self.project / "guarded.kicad_sch"
        digest = hashlib.sha256(schematic.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ToolError, "expected_sha256"):
            asyncio.run(toolkit.mcp.call_tool(
                "add_text", {"text": "CAS", "x": 10, "y": 10, "schematic_path": str(schematic)}
            ))
        with self.assertRaisesRegex(ToolError, "changed"):
            asyncio.run(toolkit.mcp.call_tool(
                "add_text", {"text": "CAS", "x": 10, "y": 10, "schematic_path": str(schematic),
                             "expected_sha256": "0" * 64}
            ))
        edited = asyncio.run(toolkit.mcp.call_tool(
            "add_text", {"text": "CAS", "x": 10, "y": 10, "schematic_path": str(schematic),
                         "expected_sha256": digest}
        ))
        assert isinstance(edited, CallToolResult)
        self.assertFalse(edited.is_error)
        self.assertNotEqual(hashlib.sha256(schematic.read_bytes()).hexdigest(), digest)

        with self.assertRaisesRegex(ToolError, "inside /projects"):
            asyncio.run(toolkit.mcp.call_tool("get_schematic_summary", {"schematic_path": "/etc/passwd"}))

        project_digest = toolkit._project_fingerprint(self.project)
        with self.assertRaisesRegex(ToolError, "expected_sha256"):
            asyncio.run(toolkit.mcp.call_tool("create_sym_lib_table", {
                "directory": str(self.project),
                "entries": [{"name": "Unsafe", "uri": "/etc/passwd"}],
            }))
        with self.assertRaisesRegex(ToolError, "pinned KiCad"):
            asyncio.run(toolkit.mcp.call_tool("create_sym_lib_table", {
                "directory": str(self.project),
                "entries": [{"name": "Unsafe", "uri": "/etc/passwd"}],
                "expected_sha256": project_digest,
            }))
        table = asyncio.run(toolkit.mcp.call_tool("create_sym_lib_table", {
            "directory": str(self.project),
            "entries": [{"name": "Local", "uri": "${KIPRJMOD}/Local.kicad_sym"}],
            "expected_sha256": project_digest,
        }))
        assert isinstance(table, CallToolResult)
        self.assertFalse(table.is_error)
        self.assertIn("${KIPRJMOD}/Local.kicad_sym", (self.project / "sym-lib-table").read_text())

        schemas = {item.name: item.input_schema for item in asyncio.run(toolkit.mcp.list_tools())}
        for name in ("create_sym_lib_table", "flatten_hierarchy", "run_jobset"):
            self.assertIn("expected_sha256", schemas[name]["required"])

    def test_upstream_creation_and_output_publication_pin_parents_and_never_replace(self):
        parent = self.project / "creation-parent"
        parent.mkdir()
        detached = self.project / "creation-parent-detached"
        outside = self.root.parent / f"creation-outside-{self.root.name}"
        outside.mkdir()
        self.addCleanup(lambda: outside.exists() and shutil.rmtree(outside))
        original_publish = toolkit._publish_staged_file_at
        swapped = False

        def swap_creation_parent(source, parent_fd, target_name):
            nonlocal swapped
            if not swapped:
                parent.rename(detached)
                parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_publish(source, parent_fd, target_name)

        create = toolkit.mcp._tool_manager._tools["create_project"].fn
        with patch.object(toolkit, "_publish_staged_file_at", side_effect=swap_creation_parent):
            create(directory=str(parent), name="safe")
        self.assertTrue((detached / "safe.kicad_pro").is_file())
        self.assertTrue((detached / "safe.kicad_sch").is_file())
        self.assertFalse((outside / "safe.kicad_pro").exists())
        parent.unlink()
        detached.rename(parent)

        output_parent = self.project / "outputs"
        output_parent.mkdir()
        output = output_parent / "autoroute"
        calls = 0

        def fake_autoroute(*, output_dir, **_):
            nonlocal calls
            calls += 1
            Path(output_dir).mkdir()
            Path(output_dir, "routed.kicad_pcb").write_text("(kicad_pcb)")
            Path(output_dir, "drc.json").write_text("{}")
            return {"drc_violations": 0, "drc_unconnected": 0,
                    "path": str(Path(output_dir, "routed.kicad_pcb"))}

        autoroute = toolkit.mcp._tool_manager._tools["autoroute_pcb"].fn
        output_parent_detached = self.project / "outputs-detached"
        original_directory_publish = toolkit._publish_staged_directory_at

        def swap_output_parent(source, parent_fd, target_name):
            output_parent.rename(output_parent_detached)
            output_parent.symlink_to(outside, target_is_directory=True)
            return original_directory_publish(source, parent_fd, target_name)

        with patch.object(toolkit, "_publish_staged_directory_at", side_effect=swap_output_parent):
            result = autoroute(_original=fake_autoroute, pcb_path=str(self.board), output_dir=str(output))
        self.assertEqual(calls, 1)
        self.assertEqual((output_parent_detached / "autoroute/routed.kicad_pcb").read_text(), "(kicad_pcb)")
        self.assertFalse((outside / "autoroute").exists())
        self.assertEqual(result["path"], str(output / "routed.kicad_pcb"))
        output_parent.unlink()
        output_parent_detached.rename(output_parent)
        with self.assertRaisesRegex(ToolError, "already exists"):
            autoroute(_original=fake_autoroute, pcb_path=str(self.board), output_dir=str(output))
        self.assertEqual(calls, 1)

    def test_upstream_output_schemas_are_explicit_and_set_net_class_cas_guards_project(self):
        schemas = {item.name: item.input_schema for item in asyncio.run(toolkit.mcp.list_tools())}
        for name, parameter in {**toolkit._UPSTREAM_OUTPUT_DIRECTORIES, **toolkit._UPSTREAM_OUTPUT_FILES}.items():
            with self.subTest(tool=name):
                self.assertIn(parameter, schemas[name]["required"])
        self.assertIn("expected_sheet_sha256", schemas["add_hierarchical_sheet"]["required"])

        project_file = self.board.with_suffix(".kicad_pro")
        project_file.write_text('{"board": {}}\n')
        project_digest = hashlib.sha256(project_file.read_bytes()).hexdigest()
        board_digest = hashlib.sha256(self.board.read_bytes()).hexdigest()
        set_net_class = toolkit.mcp._tool_manager._tools["set_net_class"].fn
        with self.assertRaisesRegex(ToolError, "changed"):
            set_net_class(
                _original=lambda **_: None, name="Power", nets=["VCC"],
                pcb_path=str(self.board), expected_sha256=board_digest,
            )

        def fake_set_net_class(*, pcb_path, **_):
            Path(pcb_path).with_suffix(".kicad_pro").write_text('{"board": {"rules": {}}}\n')
            return "updated"

        self.assertEqual(set_net_class(
            _original=fake_set_net_class, name="Power", nets=["VCC"],
            pcb_path=str(self.board), expected_sha256=project_digest,
        ), "updated")
        self.assertIn("rules", project_file.read_text())

    def test_hierarchical_sheet_stages_and_cas_guards_every_modified_schematic(self):
        create_project = toolkit.mcp._tool_manager._tools["create_project"].fn
        create_schematic = toolkit.mcp._tool_manager._tools["create_schematic"].fn
        add_sheet = toolkit.mcp._tool_manager._tools["add_hierarchical_sheet"].fn
        create_project(directory=str(self.project), name="hierarchy")
        child = self.project / "child.kicad_sch"
        create_schematic(schematic_path=str(child))
        parent = self.project / "hierarchy.kicad_sch"
        parent_before = hashlib.sha256(parent.read_bytes()).hexdigest()
        child_before = hashlib.sha256(child.read_bytes()).hexdigest()

        with self.assertRaisesRegex(ToolError, "changed"):
            add_sheet(
                parent_schematic_path=str(parent), sheet_name="Child", sheet_file=child.name,
                pins=[{"name": "DATA", "direction": "input"}], project_path=str(self.project / "hierarchy.kicad_pro"),
                expected_sha256=parent_before, expected_sheet_sha256="0" * 64,
            )
        self.assertEqual(hashlib.sha256(parent.read_bytes()).hexdigest(), parent_before)
        self.assertEqual(hashlib.sha256(child.read_bytes()).hexdigest(), child_before)

        add_sheet(
            parent_schematic_path=str(parent), sheet_name="Child", sheet_file=child.name,
            pins=[{"name": "DATA", "direction": "input"}], project_path=str(self.project / "hierarchy.kicad_pro"),
            expected_sha256=parent_before, expected_sheet_sha256=child_before,
        )
        self.assertNotEqual(hashlib.sha256(parent.read_bytes()).hexdigest(), parent_before)
        self.assertNotEqual(hashlib.sha256(child.read_bytes()).hexdigest(), child_before)

    def test_schematic_advanced_items_are_uuid_and_cas_guarded(self):
        asyncio.run(toolkit.mcp.call_tool(
            "create_project", {"directory": str(self.project), "name": "items"}
        ))
        schematic = self.project / "items.kicad_sch"
        digest = hashlib.sha256(schematic.read_bytes()).hexdigest()
        add = toolkit.SchematicItemEdit(
            action="add", item_type="bus",
            sexpr="(bus (pts (xy 10 10) (xy 20 10)) (stroke (width 0) (type default)) "
                  "(uuid 00000000-0000-0000-0000-000000000000))",
        )
        with patch.object(toolkit.shutil, "which", return_value="/usr/bin/kicad-cli"), \
             patch.object(toolkit, "_execute") as execute:
            added = toolkit.edit_schematic_items(str(schematic), digest, [add])
        self.assertEqual(Path(execute.call_args.args[0][-1]).suffix, ".kicad_sch")
        self.assertEqual(added["count"], 1)
        added_items = added["items"]
        assert isinstance(added_items, list)
        item = added_items[0]
        assert isinstance(item, dict)
        item_uuid = item["uuid"]
        assert isinstance(item_uuid, str)
        self.assertNotEqual(item_uuid, "00000000-0000-0000-0000-000000000000")

        replace = toolkit.SchematicItemEdit(
            action="replace", item_type="bus", uuid=item_uuid,
            sexpr="(bus (pts (xy 10 10) (xy 30 10)) (stroke (width 0) (type default)) "
                  "(uuid 11111111-1111-1111-1111-111111111111))",
        )
        with patch.object(toolkit.shutil, "which", return_value=None):
            replaced = toolkit.edit_schematic_items(str(schematic), str(added["sha256"]), [replace])
        replaced_items = replaced["items"]
        assert isinstance(replaced_items, list)
        self.assertIn("(xy 30 10)", str(replaced_items[0]))
        self.assertIn(item_uuid, str(replaced_items[0]))

        delete = toolkit.SchematicItemEdit(action="delete", item_type="bus", uuid=item_uuid)
        with patch.object(toolkit.shutil, "which", return_value=None):
            deleted = toolkit.edit_schematic_items(str(schematic), str(replaced["sha256"]), [delete])
        self.assertEqual(deleted["count"], 0)

    def test_board_and_footprint_full_fidelity_editors_are_cas_guarded(self):
        self.board.write_text(
            '(kicad_pcb (version 20240108) (generator pcbnew) (general (thickness 1.6)) '
            '(paper "A4") (layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
            '(37 "F.SilkS" user "f.silkscreen") (44 "Edge.Cuts" user)) '
            '(setup (pad_to_mask_clearance 0)))'
        )
        digest = hashlib.sha256(self.board.read_bytes()).hexdigest()
        add = toolkit.BoardItemEdit(
            action="add", item_type="gr_line",
            sexpr='(gr_line (start 10 10) (end 20 10) (stroke (width 0.2) (type default)) '
                  '(layer "F.SilkS") (uuid 00000000-0000-0000-0000-000000000000))',
        )
        with patch.object(toolkit, "_validate_pcbnew_file"):
            added = toolkit.edit_board_items(str(self.board), digest, [add])
        self.assertEqual(added["count"], 1)
        board_items = added["items"]
        assert isinstance(board_items, list) and isinstance(board_items[0], dict)
        item_uuid = board_items[0]["uuid"]
        assert isinstance(item_uuid, str)

        section = toolkit.get_board_section(str(self.board), "setup")
        with patch.object(toolkit, "_validate_pcbnew_file"):
            changed_section = toolkit.replace_board_section(
                str(self.board), "setup", str(section["sha256"]),
                '(setup (pad_to_mask_clearance 0.05))',
            )
        self.assertIn("0.05", str(changed_section["sexpr"]))

        delete = toolkit.BoardItemEdit(action="delete", item_type="gr_line", uuid=item_uuid)
        with patch.object(toolkit, "_validate_pcbnew_file"):
            deleted = toolkit.edit_board_items(str(self.board), str(changed_section["sha256"]), [delete])
        self.assertEqual(deleted["count"], 0)

        footprint = self.project / "full.kicad_mod"
        footprint.write_text(
            '(footprint "full" (version 20240108) (generator pcbnew) (layer "F.Cu") '
            '(uuid 11111111-1111-1111-1111-111111111111))'
        )
        definition = toolkit.get_footprint_definition(str(footprint))
        replacement = str(definition["sexpr"]).replace('(layer "F.Cu")', '(layer "F.Cu") (descr "full fidelity")')
        with patch.object(toolkit, "_validate_pcbnew_file") as validate_footprint:
            replaced = toolkit.replace_footprint_definition(
                str(footprint), str(definition["sha256"]), replacement
            )
        self.assertEqual(Path(validate_footprint.call_args.args[0]).suffix, ".kicad_mod")
        self.assertIn("full fidelity", str(replaced["sexpr"]))

    def test_project_settings_tables_and_rules_are_full_fidelity_and_cas_guarded(self):
        project_file = self.project / "settings.kicad_pro"
        project_file.write_text('{"text_variables":{"REV":"A"},"unknown":{"kept":true}}')
        settings = toolkit.get_project_settings(str(project_file))
        payload = settings["settings"]
        assert isinstance(payload, dict) and isinstance(payload["text_variables"], dict)
        payload["text_variables"]["REV"] = "B"
        replaced = toolkit.replace_project_settings(str(project_file), str(settings["sha256"]), payload)
        self.assertEqual(toolkit.get_project_settings(str(project_file))["settings"], payload)
        self.assertNotEqual(replaced["sha256"], settings["sha256"])

        table = toolkit.replace_project_library_table(
            str(self.project), "fp",
            '(fp_lib_table (version 7) (lib (name "Local") (type "KiCad") '
            '(uri "${KIPRJMOD}/Local.pretty") (options "") (descr "local")))',
        )
        read_table = toolkit.get_project_library_table(str(self.project), "fp")
        self.assertEqual(read_table["sha256"], table["sha256"])
        with self.assertRaisesRegex(ToolError, "Library URIs"):
            toolkit.replace_project_library_table(
                str(self.project), "sym",
                '(sym_lib_table (version 7) (lib (name "Bad") (type "KiCad") '
                '(uri "../../etc") (options "") (descr "bad")))',
            )

        rules = self.project / "settings.kicad_dru"
        rules.write_text('(version 1)\n(rule "clearance" (constraint clearance (min 0.2mm)))\n')
        read_rules = toolkit.get_design_rules(str(rules))
        changed_rules = toolkit.replace_design_rules(
            str(rules), str(read_rules["sha256"]),
            '(version 1)\n(rule "clearance" (constraint clearance (min 0.25mm)))\n',
        )
        self.assertIn("0.25mm", str(changed_rules["sexpr"]))

    def test_jobset_paths_cannot_escape_project(self):
        jobset = self.project / "safe.kicad_jobset"
        jobset.write_text('{"jobs":[{"settings":{"output_filename":"board.step"}}],'
                          '"outputs":[{"settings":{"output_path":".\\\\STEP_Files"}}]}')
        toolkit._validate_jobset_paths(str(jobset))
        jobset.write_text('{"jobs":[{"settings":{"output_filename":"/etc/passwd"}}],"outputs":[]}')
        with self.assertRaisesRegex(ToolError, "relative"):
            toolkit._validate_jobset_paths(str(jobset))
        jobset.write_text('{"jobs":[{"settings":{"script":"evil.py"}}],"outputs":[]}')
        with self.assertRaisesRegex(ToolError, "not allowed"):
            toolkit._validate_jobset_paths(str(jobset))

    def test_jobset_stages_outputs_and_never_replaces_existing_artifacts(self):
        (self.project / "fixture.kicad_pro").write_text("{}")
        jobset = self.project / "safe.kicad_jobset"
        jobset.write_text('{"jobs":[{"settings":{"output_filename":"board.step"}}],'
                          '"outputs":[{"settings":{"output_path":"outputs"}}]}')
        run_jobset = toolkit.mcp._tool_manager._tools["run_jobset"].fn

        def generate(*, jobset_path):
            output = Path(jobset_path).parent / "outputs"
            output.mkdir(exist_ok=True)
            (output / "board.step").write_bytes(b"new staged artifact")
            return "Jobset completed successfully"

        result = run_jobset(
            _original=generate,
            jobset_path=str(jobset),
            expected_sha256=toolkit._project_fingerprint(self.project),
        )
        artifact = self.project / "outputs/board.step"
        self.assertEqual(result, "Jobset completed successfully")
        self.assertEqual(artifact.read_bytes(), b"new staged artifact")

        before = artifact.read_bytes()

        def overwrite(*, jobset_path):
            (Path(jobset_path).parent / "outputs/board.step").write_bytes(b"replacement")
            return "must not publish"

        with self.assertRaisesRegex(ToolError, "changed or removed existing"):
            run_jobset(
                _original=overwrite,
                jobset_path=str(jobset),
                expected_sha256=toolkit._project_fingerprint(self.project),
            )
        self.assertEqual(artifact.read_bytes(), before)

    def test_update_pcb_has_explicit_create_and_update_publication_contracts(self):
        schematic = self.project / "source.kicad_sch"
        schematic.write_text("(kicad_sch)")
        board = self.project / "created.kicad_pcb"
        update = toolkit.mcp._tool_manager._tools["update_pcb_from_schematic"].fn
        calls = 0

        def backend(*, pcb_path, **_):
            nonlocal calls
            calls += 1
            Path(pcb_path).write_text(f"(kicad_pcb (version {calls}))")
            return {"path": pcb_path}

        created = update(
            _original=backend,
            schematic_path=str(schematic),
            pcb_path=str(board),
            publication={"mode": "create"},
        )
        self.assertEqual(created["path"], str(board))
        self.assertTrue(board.is_file())
        with self.assertRaisesRegex(ToolError, "mode=create"):
            update(
                _original=backend,
                schematic_path=str(schematic),
                pcb_path=str(board),
                publication={"mode": "create"},
            )
        self.assertEqual(calls, 1)

        with self.assertRaisesRegex(ToolError, "changed"):
            update(
                _original=backend,
                schematic_path=str(schematic),
                pcb_path=str(board),
                publication={"mode": "update", "expected_sha256": "0" * 64},
            )
        before = hashlib.sha256(board.read_bytes()).hexdigest()
        updated = update(
            _original=backend,
            schematic_path=str(schematic),
            pcb_path=str(board),
            publication={"mode": "update", "expected_sha256": before},
        )
        self.assertEqual(updated["path"], str(board))
        self.assertIn("version 2", board.read_text())
        self.assertEqual(calls, 2)

        with self.assertRaisesRegex(ToolError, "mode=update"):
            update(
                _original=backend,
                schematic_path=str(schematic),
                pcb_path=str(self.project / "missing.kicad_pcb"),
                publication={"mode": "update", "expected_sha256": "0" * 64},
            )

    def test_failed_backend_preserves_input_and_removes_own_output(self):
        before = hashlib.sha256(self.board.read_bytes()).digest()
        output = self.root / "failed-output"
        with self.assertRaisesRegex(ToolError, "exit 9"):
            with toolkit._job(str(self.board), str(output)) as (_, destination, work, _):
                (destination / "partial").write_text("incomplete")
                toolkit._execute([sys.executable, "-c", "raise SystemExit(9)"], work)
        self.assertFalse(output.exists())
        self.assertEqual(hashlib.sha256(self.board.read_bytes()).digest(), before)

    def test_concurrent_source_change_is_not_overwritten(self):
        output = self.root / "concurrent-output"
        with self.assertRaisesRegex(ToolError, "changed concurrently"):
            with toolkit._job(str(self.board), str(output)) as (_, destination, __, ___):
                self.board.write_text("external writer's newer content")
                (destination / "artifact").write_text("not accepted")
        self.assertEqual(self.board.read_text(), "external writer's newer content")
        self.assertFalse(output.exists())

    def test_export_output_is_private_and_parent_descriptor_is_pinned(self):
        parent = self.project / "export-parent"
        parent.mkdir()
        output = parent / "artifacts"
        renamed_parent = self.project / "pinned-export-parent"
        outside = self.root / "outside-export-parent"
        outside.mkdir()
        with toolkit._job(str(self.board), str(output)) as (_, staged_output, _, display_output):
            (staged_output / "result.txt").write_text("complete")
            self.assertFalse(output.exists())
            parent.rename(renamed_parent)
            parent.symlink_to(outside, target_is_directory=True)
            self.assertEqual(display_output, output)
        self.assertEqual((renamed_parent / "artifacts" / "result.txt").read_text(), "complete")
        self.assertFalse((outside / "artifacts").exists())

    def test_panel_and_manufacturing_validation_makes_no_output(self):
        output = self.root / "invalid"
        for parameters in [{"spacing_mm": float("nan")}, {"rows": 0}, {"rows": 20, "columns": 20}, {"tab_width_mm": -1}]:
            with self.subTest(parameters=parameters), self.assertRaises(ToolError):
                toolkit.panelize_pcb(str(self.board), str(output), **parameters)
            self.assertFalse(output.exists())
        with self.assertRaises(ToolError):
            toolkit.manufacture_pcb(str(self.board), str(output), outputs=[])
        self.assertFalse(output.exists())

    def test_schematic_hierarchy_is_bounded_and_excludes_old_outputs(self):
        schematic = self.board.with_suffix(".kicad_sch")
        schematic.write_text('(kicad_sch (sheet (property "Sheetfile" "nested/child.kicad_sch")))')
        child = self.project / "nested/child.kicad_sch"
        child.parent.mkdir()
        child.write_text('(kicad_sch)')
        old = self.project / "old-output/ignored.kicad_sch"
        old.parent.mkdir()
        old.write_text('unrelated old output')
        self.assertEqual(set(toolkit._schematics(self.board)), {schematic, child})
        outside = self.root / "outside.kicad_sch"
        outside.write_text('(kicad_sch)')
        schematic.write_text('(kicad_sch (sheet (property "Sheetfile" "../outside.kicad_sch")))')
        with self.assertRaisesRegex(ToolError, "source project|without traversal"):
            toolkit._schematics(self.board)

        duplicate = toolkit.mcp._tool_manager._tools["duplicate_sheet"].fn
        calls = 0

        def backend(**_):
            nonlocal calls
            calls += 1

        with self.assertRaisesRegex(ToolError, "without traversal"):
            duplicate(
                _original=backend,
                sheet_uuid="00000000-0000-0000-0000-000000000001",
                new_sheet_name="Copy",
                schematic_path=str(schematic),
                expected_sha256=hashlib.sha256(schematic.read_bytes()).hexdigest(),
            )
        self.assertEqual(calls, 0)

        outside_symlink_target = self.root.parent / f"sheet-outside-{self.root.name}.kicad_sch"
        outside_symlink_target.write_text("(kicad_sch)")
        self.addCleanup(outside_symlink_target.unlink, missing_ok=True)
        child_link = self.project / "linked-child.kicad_sch"
        child_link.symlink_to(outside_symlink_target)
        schematic.write_text('(kicad_sch (sheet (property "Sheetfile" "linked-child.kicad_sch")))')
        with self.assertRaisesRegex(ToolError, "symlink"):
            duplicate(
                _original=backend,
                sheet_uuid="00000000-0000-0000-0000-000000000001",
                new_sheet_name="Copy",
                schematic_path=str(schematic),
                expected_sha256=hashlib.sha256(schematic.read_bytes()).hexdigest(),
            )
        self.assertEqual(calls, 0)

        child_link.unlink()
        schematic.write_text('(kicad_sch (sheet (property "Sheetfile" "child.kicad_sch")))')
        with self.assertRaisesRegex(ToolError, "one .kicad_sch filename"):
            duplicate(
                _original=backend,
                sheet_uuid="00000000-0000-0000-0000-000000000001",
                new_sheet_name="Copy",
                new_file_name="out/escape.kicad_sch",
                schematic_path=str(schematic),
                expected_sha256=hashlib.sha256(schematic.read_bytes()).hexdigest(),
            )
        self.assertEqual(calls, 0)

        nested = self.project / "nested-project"
        nested.mkdir(exist_ok=True)
        nested_child = nested / "child.kicad_sch"
        nested_child.write_text("(kicad_sch)")
        project_file = self.project / "root.kicad_pro"
        project_file.write_text(
            '{"schematic":{"top_level_sheets":[{"filename":"../../outside.kicad_sch"}]}}')
        root_schematic = project_file.with_suffix(".kicad_sch")
        root_schematic.write_text("(kicad_sch)")
        is_root = toolkit.mcp._tool_manager._tools["is_root_schematic"].fn
        with self.assertRaisesRegex(ToolError, "top-level schematic path.*without traversal"):
            is_root(_original=backend, schematic_path=str(nested_child))
        self.assertEqual(calls, 0)

    def test_native_shapes_and_localization_boundary(self):
        self.assertEqual(toolkit.ReplicationSettings().model_dump(), toolkit.REPLICATION_DEFAULTS)
        for model, parameters in [
            (toolkit.LinearPlacement, {"kind": "linear", "step_x_mm": float("inf"), "step_y_mm": 0}),
            (toolkit.PartPlacement, {"ref": "R1", "x_mm": 0, "y_mm": 0, "side": "unknown"}),
            (toolkit.ReplicationSettings, {"rep_tracks": "false"}),
        ]:
            with self.subTest(model=model), self.assertRaises(ValueError):
                model(**parameters)
        with patch.object(toolkit, "_native") as backend:
            for output in [str(self.project / "nested-output"), str(self.root / "project")]:
                with self.assertRaises(ToolError):
                    toolkit.localize_project_dependencies(str(self.project), output)
            backend.assert_not_called()
        output = self.root / "native-failed"
        with patch.object(toolkit, "_native", side_effect=ToolError("native failure")):
            with self.assertRaisesRegex(ToolError, "native failure"):
                toolkit.place_parts(str(self.board), str(output), [toolkit.PartPlacement(ref="R1", x_mm=0, y_mm=0)])
        self.assertFalse(output.exists())

    def test_native_board_preserves_project_context(self):
        library = self.project / "Sirokko.pretty"
        library.mkdir()
        (library / "Sirokko_Test.kicad_mod").write_text("(footprint \"Sirokko_Test\")\n")
        (self.project / "Root_Test.kicad_mod").write_text("(footprint \"Root_Test\")\n")
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (version 7) '
            '(lib (name "Sirokko") (type "KiCad") (uri "${KIPRJMOD}/Sirokko.pretty") '
            '(options "") (descr "local")) '
            '(lib (name "Root") (type "KiCad") (uri "${KIPRJMOD}/") '
            '(options "") (descr "project root")))\n'
        )
        (self.project / "sym-lib-table").write_text("(sym_lib_table)")
        self.board.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "keep")\n')
        output = self.root / "variant"

        def native(_, __, **parameters):
            Path(parameters["output_pcb"]).write_text("(kicad_pcb)")
            return {"output_pcb": parameters["output_pcb"]}

        with patch.object(toolkit, "_native", side_effect=native):
            toolkit._native_board("variant_apply", str(self.board), str(output), choices={"Power": "high"})
        self.assertEqual((output / "fp-lib-table").read_text(), (self.project / "fp-lib-table").read_text())
        self.assertEqual((output / "sym-lib-table").read_text(), "(sym_lib_table)")
        self.assertEqual((output / "fixture.kicad_dru").read_text(), '(version 1)\n(rule "keep")\n')
        self.assertEqual(
            (output / "Sirokko.pretty" / "Sirokko_Test.kicad_mod").read_text(),
            "(footprint \"Sirokko_Test\")\n",
        )
        self.assertEqual(
            (output / "Root_Test.kicad_mod").read_text(),
            "(footprint \"Root_Test\")\n",
        )

    def test_prefab_gate_stages_project_local_libraries(self):
        library = self.project / "Sirokko.pretty"
        library.mkdir()
        (library / "Sirokko_Test.kicad_mod").write_text("(footprint \"Sirokko_Test\")\n")
        (self.project / "Root_Test.kicad_mod").write_text("(footprint \"Root_Test\")\n")
        (self.project / "fp-lib-table").write_text(
            '(fp_lib_table (version 7) '
            '(lib (name "Sirokko") (type "KiCad") (uri "${KIPRJMOD}/Sirokko.pretty") '
            '(options "") (descr "local")) '
            '(lib (name "Root") (type "KiCad") (uri "${KIPRJMOD}") '
            '(options "") (descr "project root")))\n'
        )
        symbol = self.project / "nested" / "Sirokko.kicad_sym"
        symbol.parent.mkdir()
        symbol.write_text("(kicad_symbol_lib (version 20231120))\n")
        (self.project / "sym-lib-table").write_text(
            '(sym_lib_table (version 7) (lib (name "Sirokko") (type "KiCad") '
            '(uri "${KIPRJMOD}/nested/Sirokko.kicad_sym") (options "") (descr "local")))\n'
        )
        output = self.root / "prefab"

        def native(operation, _, **parameters):
            self.assertEqual(operation, "prefab_gate")
            staged_board = Path(parameters["input_pcb"])
            self.assertTrue((staged_board.parent / "Sirokko.pretty" / "Sirokko_Test.kicad_mod").is_file())
            self.assertEqual(
                (staged_board.parent / "Root_Test.kicad_mod").read_text(),
                "(footprint \"Root_Test\")\n",
            )
            self.assertEqual(
                (staged_board.parent / "nested" / "Sirokko.kicad_sym").read_text(),
                "(kicad_symbol_lib (version 20231120))\n",
            )
            return {"passed": True, "report": {"summary": {"errors": 0, "warnings": 0}, "checks": []}}

        with patch.object(toolkit, "_native", side_effect=native):
            toolkit.run_prefab_gate(str(self.board), str(output))
        self.assertIn('"warnings": 0', (output / "prefab-gate.json").read_text())

    def test_timeout_stops_the_process_group(self):
        pidfile = self.root / "child.pid"
        script = ("import subprocess,sys,time,pathlib;"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                  f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(60)")
        with self.assertRaisesRegex(ToolError, "process group was stopped"):
            toolkit._execute([sys.executable, "-c", script], self.root, timeout=1)
        child_stat = Path("/proc") / pidfile.read_text() / "stat"
        try:
            state = child_stat.read_text().split(") ", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            return  # The killed grandchild has already been reaped.
        self.assertEqual(state, "Z")

    def test_registered_schema_retains_all_original_tools(self):
        actual = {t.name: t for t in asyncio.run(toolkit.mcp.list_tools())}
        original = set()
        for module in [toolkit.upstream.schematic, toolkit.upstream.pcb, toolkit.upstream.symbol,
                       toolkit.upstream.footprint, toolkit.upstream.project]:
            original.update(t.name for t in asyncio.run(module.mcp.list_tools()))
        self.assertTrue(original <= actual.keys())
        added = {"get_toolkit_status", "manufacture_pcb", "generate_interactive_bom", "panelize_pcb", "render_pcb",
                 "discover_replication", "replicate_layout", "place_footprints", "place_parts",
                 "scan_project_dependencies", "localize_project_dependencies", "export_testpoints",
                 "generate_pinout", "generate_pinout_image", "generate_kibuzzard_footprint",
                 "apply_variant", "solve_trace_impedance", "run_prefab_gate",
                 "get_board_edit_state", "edit_board_copper_batch", "stitch_vias",
                 "edit_silk_references", "fanout_footprint", "apply_impedance_netclasses",
                 "update_clearance_matrix", "place_kobee_label", "place_kobee_artwork",
                 "insert_kibuzzard_footprint", "manage_project_notes", "run_pcb_action_tool",
                 "apply_via_fence", "run_rf_tool", "separate_pcb", "create_stencil", "present_pcb",
                 "search_skidl_parts", "convert_netlist_to_skidl", "generate_offline_cost_spreadsheet",
                 "create_footprint", "edit_footprint", "download_project_file", "list_project_files",
                 "stat_project_path", "create_project_directory", "upload_project_file", "copy_project_path",
                 "move_project_path", "delete_project_path", "create_project_archive", "extract_project_archive",
                 "get_schematic_items", "edit_schematic_items", "get_board_items", "edit_board_items",
                 "get_board_section", "replace_board_section", "get_footprint_definition",
                 "replace_footprint_definition", "get_project_settings", "replace_project_settings",
                 "get_project_library_table", "replace_project_library_table", "get_design_rules",
                 "replace_design_rules"}
        self.assertEqual(set(actual) - original, added)
        kivar = toolkit.ADDON_COVERAGE["de.markh.kivar"]
        viafence = toolkit.ADDON_COVERAGE["com.github.ozzysv.viafence"]
        assert isinstance(kivar, dict) and isinstance(viafence, dict)
        self.assertEqual(kivar["status"], "full")
        self.assertEqual(viafence["status"], "full")
        self.assertEqual(toolkit.ENGINE_COVERAGE["PcbDraw"]["tools"], ["render_pcb"])
        for coverage in [toolkit.ADDON_COVERAGE, toolkit.ENGINE_COVERAGE]:
            for package, row in coverage.items():
                with self.subTest(package=package):
                    assert isinstance(row, dict)
                    tools = row["tools"]
                    assert isinstance(tools, list)
                    for tool in tools:
                        assert isinstance(tool, str)
                        self.assertIn(tool, actual)
        for name in added:
            self.assertIsNotNone(actual[name].annotations)
        self.assertIn("layout", actual["place_footprints"].input_schema["required"])
        self.assertEqual(actual["create_footprint"].input_schema["properties"]["name"]["pattern"],
                         r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,127}$")
        self.assertIn("pads", actual["create_footprint"].input_schema["required"])
        self.assertIn("operations", actual["edit_footprint"].input_schema["required"])
        self.assertEqual(actual["download_project_file"].input_schema["required"], ["path"])
        self.assertEqual(actual["delete_project_path"].input_schema["required"],
                         ["path", "expected_sha256", "confirm"])
        self.assertEqual(len(asyncio.run(toolkit.mcp.list_resources())), 1)
        self.assertEqual(len(asyncio.run(toolkit.mcp.list_resource_templates())), 1)
        for name, target in toolkit._MUTATION_TARGETS.items():
            with self.subTest(tool=name, target=target):
                required = actual[name].input_schema["required"]
                if name == "update_pcb_from_schematic":
                    self.assertIn("publication", required)
                    self.assertNotIn("expected_sha256", required)
                    publication = actual[name].input_schema["properties"]["publication"]
                    self.assertEqual(publication["discriminator"]["propertyName"], "mode")
                else:
                    self.assertIn("expected_sha256", required)
        download_annotations = actual["download_project_file"].annotations
        assert download_annotations is not None
        self.assertTrue(download_annotations.read_only_hint)
        self.assertFalse(download_annotations.destructive_hint)
        edit_items = actual["edit_footprint"].input_schema["properties"]["operations"]["items"]
        self.assertEqual(edit_items["discriminator"]["propertyName"], "kind")
        for name in ["manufacture_pcb", "generate_interactive_bom", "panelize_pcb"]:
            annotations = actual[name].annotations
            assert annotations is not None
            self.assertFalse(annotations.read_only_hint)
            self.assertFalse(annotations.destructive_hint)
            schema = actual[name].input_schema
            self.assertIn("pcb_path", schema["required"])
            self.assertIn("output_dir", schema["required"])
        self.assertEqual(actual["panelize_pcb"].input_schema["properties"]["rows"]["minimum"], 1)
        panel_properties = actual["panelize_pcb"].input_schema["properties"]
        self.assertEqual(panel_properties["alternation"]["enum"], ["none", "rows", "cols", "rowsCols"])
        self.assertEqual(panel_properties["framing"]["enum"], ["none", "railstb", "railslr", "frame", "tightframe"])
        output_items = actual["manufacture_pcb"].input_schema["properties"]["outputs"]["anyOf"][0]["items"]["enum"]
        self.assertTrue({"bom", "netlist_ipc", "ipc2581", "step", "svg"} <= set(output_items))
        impedance_annotations = actual["solve_trace_impedance"].annotations
        assert impedance_annotations is not None
        self.assertTrue(impedance_annotations.read_only_hint)
        self.assertIn("choices", actual["apply_variant"].input_schema["required"])


if __name__ == "__main__":
    unittest.main()
