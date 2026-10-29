"""Regression checks for selected-net import and the Freerouting 2.5.0 adapter."""
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from mcp_server_kicad import _freerouting as fr
from mcp_server_kicad import pcb
from mcp_server_kicad._cst import parse, serialize
from mcp_server_kicad._shared import ToolError

source = b'''(kicad_pcb (version 20260101)
(footprint "keep" (pad "1" smd rect (net "/A")) (pad "2" smd rect (net "/B")))
(segment (start 0 0) (end 1 0) (width .25) (layer "F.Cu") (net "/A") (uuid "source-segment"))
(via (at 1 0) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net "/A") (uuid "source-via"))
(segment (start 4 4) (end 5 4) (width .25) (layer "F.Cu") (net "/B") (uuid "other-segment"))
(zone (net "/B") (name "untouched")))'''

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    routed = tmp / "imported.kicad_pcb"
    routed.write_bytes(b'''(kicad_pcb (version 20260101)
(footprint "keep" (pad "1" smd rect (net "/A")) (pad "2" smd rect (net "/B")))
(segment (start 0 0) (end 1 0) (width .25) (layer "F.Cu") (net "/A") (uuid "source-segment"))
(via (at 1 0) (size .6) (drill .3) (layers "F.Cu" "B.Cu") (net "/A") (uuid "renumbered-source-via"))
(segment (start 1 0) (end 3 0) (width .25) (layer "F.Cu") (net "/A") (uuid "new-segment"))
(segment (start 5 4) (end 7 4) (width .25) (layer "F.Cu") (net "/B") (uuid "unselected-new")))''')
    result = pcb._selected_routes(source, str(routed), ["/A"])
    original = parse(source).lists[0]
    merged = parse(result).lists[0]
    original_nodes = [serialize(node) for node in original.lists]
    assert [serialize(node) for node in merged.lists[: len(original_nodes)]] == original_nodes
    selected = [
        node for node in merged.lists
        if node.head in {"segment", "arc", "via"} and pcb._net_name_of(merged, node) == "/A"
    ]
    assert len(selected) == 3
    assert {pcb._uuid_text(node) for node in selected} == {
        "source-segment", "source-via", "new-segment"
    }
    assert b"unselected-new" not in result

    board = tmp / "input.kicad_pcb"
    board.write_bytes(source)
    output = tmp / "out"
    output.mkdir()
    stale_board = output / "input_routed.kicad_pcb"
    stale_drc = output / "input_routed-drc.json"

    def export_dsn(_source, dsn_path):
        Path(dsn_path).touch()
        return None

    for route_result, expected in (("Freerouting timed out", "timed out"), (None, "session file")):
        stale_board.write_text("stale")
        stale_drc.write_text("stale")
        with (
            patch.object(pcb, "_find_on_path", return_value="/usr/bin/java"),
            patch.object(pcb, "_check_java", return_value=None),
            patch.object(
                pcb, "_ensure_jar", return_value=("/opt/freerouting/freerouting.jar", None)
            ),
            patch.object(pcb, "_export_dsn", side_effect=export_dsn),
            patch.object(pcb, "_pcbnew_major", return_value=10),
            patch.object(pcb, "_run_freerouting", return_value=route_result),
        ):
            try:
                pcb.autoroute_pcb(pcb_path=str(board), output_dir=str(output), timeout=1)
            except ToolError as exc:
                assert expected in str(exc)
            else:
                raise AssertionError("failed autoroute was accepted")
        assert not stale_board.exists() and not stale_drc.exists()

    dsn = tmp / "in.dsn"
    dsn.write_bytes(b"(wiring (wire (type route)) (via (type protect)))")
    with patch.object(
        fr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
    ) as run:
        assert fr.run_freerouting(
            "jar", str(dsn), "out.ses", max_passes=3, java="/usr/bin/java", protect_existing=True
        ) is None
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-mp") + 1] == "3"
        assert "--router.optimizer.enabled=false" in cmd
        assert "--router.fanout.max_passes=3" in cmd
        assert "--router.automatic_neckdown=false" in cmd
        assert "--router.fanout.enabled=true" in cmd
        assert b"(type fix)" in dsn.read_bytes()
        assert b"(type route)" not in dsn.read_bytes()
    assert fr.run_freerouting("jar", str(dsn), "out.ses", max_passes=0, java="/usr/bin/java")

print("PASS: source copper preserved, new selected paths appended, stale/partial outputs rejected")
