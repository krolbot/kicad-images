"""Source-level checks for the filesystem CAS used by the native batch tool."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mcp_board_batch as batch


class AtomicBoardWriteTests(unittest.TestCase):
    def test_exchange_replaces_expected_board_and_retains_old_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "board.kicad_pcb"
            candidate = Path(temporary) / "candidate.kicad_pcb"
            source.write_bytes(b"expected board")
            candidate.write_bytes(b"accepted candidate")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            result = batch._atomic_replace(source, candidate, expected)
            self.assertEqual(source.read_bytes(), b"accepted candidate")
            self.assertEqual(candidate.read_bytes(), b"expected board")
            self.assertEqual(result, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_change_between_precheck_and_exchange_is_restored_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "board.kicad_pcb"
            candidate = Path(temporary) / "candidate.kicad_pcb"
            source.write_bytes(b"expected board")
            candidate.write_bytes(b"accepted candidate")
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            exchange = batch._exchange
            calls = 0

            def race(left, right):
                nonlocal calls
                calls += 1
                if calls == 1:
                    source.write_bytes(b"concurrent board")
                exchange(left, right)

            with patch.object(batch, "_exchange", side_effect=race):
                with self.assertRaisesRegex(batch.ToolError, "CAS mismatch at atomic write"):
                    batch._atomic_replace(source, candidate, expected)
            self.assertEqual(source.read_bytes(), b"concurrent board")
            self.assertEqual(candidate.read_bytes(), b"accepted candidate")


if __name__ == "__main__":
    unittest.main()