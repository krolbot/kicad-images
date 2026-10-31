"""Validation-only checks for the typed native plugin bridge; no KiCad runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import toolkit_native


class ToolkitNativeValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input_pcb = self.root / "input.kicad_pcb"
        self.input_pcb.write_bytes(b"source bytes")

    def test_existing_output_is_rejected_before_any_engine_load(self):
        output = self.root / "output.kicad_pcb"
        output.write_bytes(b"keep")
        with patch.object(toolkit_native, "_load_pcbnew") as load_pcbnew:
            with self.assertRaisesRegex(toolkit_native.NativeError, "already exists"):
                toolkit_native.run_request({
                    "operation": "place_footprints", "input_pcb": str(self.input_pcb),
                    "output_pcb": str(output), "anchor_ref": "U1", "references": ["U1"],
                    "layout": {"kind": "linear", "step_x_mm": 1, "step_y_mm": 1},
                })
            load_pcbnew.assert_not_called()
        dangling = self.root / "dangling.kicad_pcb"
        dangling.symlink_to(self.root / "missing.kicad_pcb")
        with self.assertRaisesRegex(toolkit_native.NativeError, "already exists"):
            toolkit_native._output_pcb(str(dangling), self.input_pcb)
        self.assertEqual(output.read_bytes(), b"keep")

    def test_nonfinite_layout_and_unknown_reference_are_rejected(self):
        with self.assertRaisesRegex(toolkit_native.NativeError, "finite"):
            toolkit_native._layout({"layout": {"kind": "linear", "step_x_mm": float("nan"), "step_y_mm": 1}})
        with self.assertRaisesRegex(toolkit_native.NativeError, "not on input_pcb"):
            toolkit_native._placement_entries(
                [{"ref": "U404", "x_mm": 1, "y_mm": 2}], {"U1"}
            )

    def test_dependency_indirect_read_and_library_traversal_are_rejected(self):
        class Resolver:
            TWIN_EXTS = (".step", ".stp", ".wrl")

            @staticmethod
            def make_resolver(project):
                return lambda value: value

        for row in [
            {"type": "model", "source": "/etc/passwd", "status": "external", "name": "passwd"},
            {"type": "footprint", "source": "/usr/share/kicad/footprints", "status": "external", "name": "../../escape:item"},
            {"type": "symbol", "source": "/usr/share/kicad/symbols/Device.kicad_sym", "status": "standard", "name": "Device:R"},
        ]:
            with self.subTest(row=row), self.assertRaises(toolkit_native.NativeError):
                toolkit_native._validate_dependencies(Resolver, self.root, [row], False)

    def test_cli_emits_one_json_error_response(self):
        completed = subprocess.run(
            [sys.executable, str(Path(toolkit_native.__file__))],
            input=json.dumps({"operation": "not_an_operation"}), text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 2)
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertFalse(response["ok"])
        self.assertIn("Unknown operation", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
