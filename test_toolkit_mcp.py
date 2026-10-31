"""Runnable source-level boundaries; real KiCad engines are exercised by image smoke."""
import asyncio
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import toolkit_mcp as toolkit
from mcp.server.mcpserver.exceptions import ToolError


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
        added = {"get_toolkit_status", "manufacture_pcb", "generate_interactive_bom", "panelize_pcb",
                 "discover_replication", "replicate_layout", "place_footprints", "place_parts",
                 "scan_project_dependencies", "localize_project_dependencies"}
        self.assertEqual(set(actual) - original, added)
        for name in added:
            self.assertIsNotNone(actual[name].annotations)
        self.assertIn("layout", actual["place_footprints"].input_schema["required"])
        for name in ["manufacture_pcb", "generate_interactive_bom", "panelize_pcb"]:
            annotations = actual[name].annotations
            assert annotations is not None
            self.assertFalse(annotations.read_only_hint)
            self.assertFalse(annotations.destructive_hint)
            schema = actual[name].input_schema
            self.assertIn("pcb_path", schema["required"])
            self.assertIn("output_dir", schema["required"])
        self.assertEqual(actual["panelize_pcb"].input_schema["properties"]["rows"]["minimum"], 1)


if __name__ == "__main__":
    unittest.main()
