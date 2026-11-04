"""Source-level checks for the filesystem CAS used by the native batch tool."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mcp_board_batch as batch


class AtomicBoardWriteTests(unittest.TestCase):
    def test_footprint_move_layer_is_optional_strict_front_or_back(self):
        legacy = batch.FootprintMove(reference="J1", x_mm=1, y_mm=2)
        self.assertIsNone(legacy.layer)
        self.assertEqual(batch.FootprintMove(reference="J1", x_mm=1, y_mm=2, layer="F.Cu").layer, "F.Cu")
        with self.assertRaisesRegex(ValueError, "Input should be 'F.Cu' or 'B.Cu'"):
            batch.FootprintMove.model_validate({"reference": "J1", "x_mm": 1, "y_mm": 2, "layer": "front"})
        with self.assertRaisesRegex(ValueError, "Extra inputs are not permitted"):
            batch.FootprintMove.model_validate({"reference": "J1", "x_mm": 1, "y_mm": 2, "side": "F.Cu"})

    def test_stage_project_preserves_local_libraries_without_linking_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            board = project / "board.kicad_pcb"
            board.write_bytes(b"canonical board")
            (project / "board.kicad_pro").write_text("project rules")
            (project / "fp-lib-table").write_text("project footprint table")
            pretty = project / "Local.pretty"
            pretty.mkdir()
            (pretty / "Local.kicad_mod").write_text("footprint")
            models = project / "3dmodels"
            models.mkdir()
            (models / "Local.step").write_text("model")
            destination = project / ".kicad-batch-test"
            destination.mkdir()

            stage = batch._stage_project(board, destination)

            self.assertEqual(stage.read_bytes(), board.read_bytes())
            self.assertEqual((destination / "fp-lib-table").read_text(), "project footprint table")
            self.assertEqual((destination / "Local.pretty" / "Local.kicad_mod").read_text(), "footprint")
            self.assertEqual((destination / "3dmodels" / "Local.step").read_text(), "model")
            stage.write_bytes(b"candidate")
            self.assertEqual(board.read_bytes(), b"canonical board")

    def test_exchange_replaces_expected_board_and_retains_old_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "board.kicad_pcb"
            candidate = Path(temporary) / "candidate.kicad_pcb"
            source.write_bytes(b"expected board")
            candidate.write_bytes(b"accepted candidate")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            result = batch._atomic_replace(source, candidate, expected)
            self.assertEqual(source.read_bytes(), b"accepted candidate")
            self.assertEqual(candidate.read_bytes(), b"accepted candidate")
            self.assertEqual(result, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_change_between_precheck_and_exchange_is_restored_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "board.kicad_pcb"
            candidate = Path(temporary) / "candidate.kicad_pcb"
            source.write_bytes(b"expected board")
            candidate.write_bytes(b"accepted candidate")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            exchange = batch._exchange_at
            calls = 0

            def race(parent_fd, left, right):
                nonlocal calls
                calls += 1
                if calls == 1:
                    source.write_bytes(b"concurrent board")
                exchange(parent_fd, left, right)

            with patch.object(batch, "_exchange_at", side_effect=race):
                with self.assertRaisesRegex(batch.ToolError, "CAS mismatch at atomic write"):
                    batch._atomic_replace(source, candidate, expected)
            self.assertEqual(source.read_bytes(), b"concurrent board")
            self.assertEqual(candidate.read_bytes(), b"accepted candidate")

    def test_pinned_parent_prevents_symlink_swap_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "projects"
            project = root / "project"
            outside = Path(temporary) / "outside"
            project.mkdir(parents=True)
            outside.mkdir()
            source = project / "board.kicad_pcb"
            outside_board = outside / source.name
            source.write_bytes(b"expected board")
            outside_board.write_bytes(b"expected board")
            candidate = Path(temporary) / "candidate.kicad_pcb"
            candidate.write_bytes(b"accepted candidate")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()

            with patch.dict(os.environ, {"KICAD_PROJECT_ROOT": str(root)}):
                with batch._open_board(str(source)) as (_, stable):
                    pinned = root / "project.pinned"
                    project.rename(pinned)
                    project.symlink_to(outside, target_is_directory=True)
                    batch._atomic_replace(stable, candidate, expected)

            self.assertEqual((pinned / source.name).read_bytes(), b"accepted candidate")
            self.assertEqual(outside_board.read_bytes(), b"expected board")


if __name__ == "__main__":
    unittest.main()