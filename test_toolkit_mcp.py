"""Runnable source-level boundaries; real KiCad engines are exercised by image smoke."""
import asyncio
import base64
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_failed_backend_preserves_input_and_removes_own_output(self):
        before = hashlib.sha256(self.board.read_bytes()).digest()
        output = self.root / "failed-output"
        with self.assertRaisesRegex(ToolError, "exit 9"):
            with toolkit._job(str(self.board), str(output)) as (_, destination, work):
                (destination / "partial").write_text("incomplete")
                toolkit._execute([sys.executable, "-c", "raise SystemExit(9)"], work)
        self.assertFalse(output.exists())
        self.assertEqual(hashlib.sha256(self.board.read_bytes()).digest(), before)

    def test_concurrent_source_change_is_not_overwritten(self):
        output = self.root / "concurrent-output"
        with self.assertRaisesRegex(ToolError, "changed concurrently"):
            with toolkit._job(str(self.board), str(output)) as (_, destination, __):
                self.board.write_text("external writer's newer content")
                (destination / "artifact").write_text("not accepted")
        self.assertEqual(self.board.read_text(), "external writer's newer content")
        self.assertFalse(output.exists())

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
        with self.assertRaisesRegex(ToolError, "source project"):
            toolkit._schematics(self.board)

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

    def test_native_board_preserves_project_library_tables(self):
        (self.project / "fp-lib-table").write_text("(fp_lib_table)")
        (self.project / "sym-lib-table").write_text("(sym_lib_table)")
        self.board.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "keep")\n')
        output = self.root / "variant"

        def native(_, __, **parameters):
            Path(parameters["output_pcb"]).write_text("(kicad_pcb)")
            return {"output_pcb": parameters["output_pcb"]}

        with patch.object(toolkit, "_native", side_effect=native):
            toolkit._native_board("variant_apply", str(self.board), str(output), choices={"Power": "high"})
        self.assertEqual((output / "fp-lib-table").read_text(), "(fp_lib_table)")
        self.assertEqual((output / "sym-lib-table").read_text(), "(sym_lib_table)")
        self.assertEqual((output / "fixture.kicad_dru").read_text(), '(version 1)\n(rule "keep")\n')

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
                 "create_footprint", "edit_footprint", "download_project_file"}
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
