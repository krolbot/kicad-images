import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BOARD = Path(os.environ.get("BATCH_FIXTURE", "/projects/board-batch-smoke/fixture.kicad_pcb"))
PARITY_BOARD = BOARD.with_name("footprint-parity.kicad_pcb")
_DIRECT_NATIVE_MOVE = r"""
import json
import sys
from pathlib import Path
import pcbnew

path, move = Path(sys.argv[1]), json.loads(sys.argv[2])
board = pcbnew.LoadBoard(str(path))
fp = next(item for item in board.GetFootprints() if item.GetReference() == move["reference"])
fp.SetPosition(pcbnew.VECTOR2I_MM(move["x_mm"], move["y_mm"]))
target = move.get("layer")
if target is not None and fp.GetLayerName() != target:
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
if move.get("rotation_deg") is not None:
    fp.SetOrientationDegrees(move["rotation_deg"])
if board.GetAreaCount():
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
pcbnew.SaveBoard(str(path), board)
assert pcbnew.LoadBoard(str(path)) is not None
"""


def sha(path=BOARD):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode(result):
    text = "\n".join(item.text for item in result.content if item.type == "text")
    return text, (json.loads(text) if text.startswith("{") else None)


def direct_native_bytes(move):
    with tempfile.TemporaryDirectory(prefix="native-footprint-parity-") as temporary:
        expected = Path(temporary) / PARITY_BOARD.name
        shutil.copy2(PARITY_BOARD, expected)
        subprocess.run(
            ["/usr/bin/python3", "-c", _DIRECT_NATIVE_MOVE, str(expected), json.dumps(move)],
            text=True,
            capture_output=True,
            check=True,
        )
        snapshot = subprocess.run(
            ["/usr/bin/python3", "/usr/local/share/kicad/mcp_board_batch_worker.py"],
            input=json.dumps({"action": "snapshot", "path": str(expected)}),
            text=True,
            capture_output=True,
            check=True,
        )
        return expected.read_bytes(), json.loads(snapshot.stdout)


async def call(session, name, arguments, error=False) -> Any:
    result = await session.call_tool(name, arguments)
    text, data = decode(result)
    assert result.is_error is error, (name, result.is_error, text)
    return data if data is not None else text


async def main():
    params = StdioServerParameters(command="/usr/local/bin/kicad-mcp", env=os.environ.copy())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert {"get_board_edit_state", "edit_board_copper_batch"} <= set(tools)
            schema = tools["edit_board_copper_batch"].input_schema
            assert schema["properties"]["dry_run"]["default"] is True
            assert "required_pads" in schema["properties"] and "autoroute_nets" in schema["properties"]
            move_schema = schema["$defs"]["FootprintMove"]
            layer_schema = move_schema["properties"]["layer"]
            assert move_schema["additionalProperties"] is False
            assert next(item["enum"] for item in layer_schema["anyOf"] if "enum" in item) == ["F.Cu", "B.Cu"]

            parity_initial = await call(
                session,
                "get_board_edit_state",
                {"pcb_path": str(PARITY_BOARD), "include_drc": True},
            )
            assert parity_initial["sha256"] == sha(PARITY_BOARD)
            assert parity_initial["counts"] == {"footprints": 1, "pads": 2, "segments": 0, "vias": 0, "zones": 0}
            assert parity_initial["drc"]["violation_count"] == parity_initial["drc"]["unconnected_count"] == 0
            original_sha = parity_initial["sha256"]
            original_u1 = parity_initial["footprints"]["U1"]
            original_parity_bytes = PARITY_BOARD.read_bytes()

            for bad_move in (
                {"reference": "U1", "x_mm": 15, "y_mm": 25, "layer": "front"},
                {"reference": "U1", "x_mm": 15, "y_mm": 25, "side": "F.Cu"},
            ):
                await call(
                    session,
                    "edit_board_copper_batch",
                    {"pcb_path": str(PARITY_BOARD), "expected_sha256": original_sha, "moves": [bad_move]},
                    error=True,
                )
                assert sha(PARITY_BOARD) == original_sha

            async def native_parity(move):
                source_sha = sha(PARITY_BOARD)
                expected_bytes, expected_state = direct_native_bytes(move)
                package = {"pcb_path": str(PARITY_BOARD), "expected_sha256": source_sha, "moves": [move]}
                dry = await call(session, "edit_board_copper_batch", package)
                assert dry["status"] == "dry_run_passed", dry
                assert dry["source_unchanged"] and sha(PARITY_BOARD) == source_sha
                await call(
                    session,
                    "edit_board_copper_batch",
                    package | {"dry_run": False, "confirmation_token": "0" * 64},
                    error=True,
                )
                assert sha(PARITY_BOARD) == source_sha
                applied = await call(
                    session,
                    "edit_board_copper_batch",
                    package | {"dry_run": False, "confirmation_token": dry["confirmation_token"]},
                )
                assert applied["status"] == "applied", applied
                assert PARITY_BOARD.read_bytes() == expected_bytes
                actual = await call(
                    session,
                    "get_board_edit_state",
                    {"pcb_path": str(PARITY_BOARD), "references": ["U1"], "include_drc": True},
                )
                assert actual["footprints"]["U1"] == expected_state["footprints"]["U1"]
                assert actual["drc"]["violation_count"] == actual["drc"]["unconnected_count"] == 0
                return actual["footprints"]["U1"]

            back_u1 = await native_parity({"reference": "U1", "x_mm": 15, "y_mm": 25, "layer": "B.Cu"})
            assert back_u1["layer"] == "B.Cu"
            assert [pad["position_mm"] for pad in back_u1["pads"]] == [[14.0, 26.0], [17.0, 26.0]]
            assert all("B.Cu" in pad["layers"] and "F.Cu" not in pad["layers"] for pad in back_u1["pads"])
            for old, new in zip(original_u1["pads"], back_u1["pads"], strict=True):
                assert {key: old[key] for key in ("uuid", "number", "net", "size_mm", "drill_mm")} == {
                    key: new[key] for key in ("uuid", "number", "net", "size_mm", "drill_mm")
                }
            front_u1 = await native_parity({"reference": "U1", "x_mm": 15, "y_mm": 25, "layer": "F.Cu"})
            assert front_u1 == original_u1
            assert PARITY_BOARD.read_bytes() == original_parity_bytes
            combined_u1 = await native_parity(
                {"reference": "U1", "x_mm": 18, "y_mm": 27, "rotation_deg": 37.5, "layer": "B.Cu"}
            )
            assert combined_u1["layer"] == "B.Cu"
            assert combined_u1["position_mm"] == [18.0, 27.0]
            assert combined_u1["rotation_deg"] == 37.5
            restored_u1 = await native_parity(
                {"reference": "U1", "x_mm": 15, "y_mm": 25, "rotation_deg": 0, "layer": "F.Cu"}
            )
            assert restored_u1 == original_u1

            initial = await call(
                session,
                "get_board_edit_state",
                {"pcb_path": str(BOARD), "include_drc": True},
            )
            assert initial["sha256"] == sha()
            assert initial["counts"] == {"footprints": 4, "pads": 4, "segments": 2, "vias": 0, "zones": 0}
            assert initial["drc"]["violation_count"] == initial["drc"]["unconnected_count"] == 0
            n1 = next((uuid for uuid, item in initial["copper"].items() if item["net"] == "/N1"))
            n2 = next((uuid for uuid, item in initial["copper"].items() if item["net"] == "/N2"))
            initial_sha = initial["sha256"]

            await call(
                session,
                "edit_board_copper_batch",
                {
                    "pcb_path": str(BOARD),
                    "expected_sha256": initial_sha,
                    "remove_uuids": ["00000000-0000-0000-0000-000000000000"],
                },
                error=True,
            )
            assert sha() == initial_sha

            await call(
                session,
                "edit_board_copper_batch",
                {
                    "pcb_path": str(BOARD),
                    "expected_sha256": "0" * 64,
                    "moves": [{"reference": "J1", "x_mm": 21, "y_mm": 20}],
                },
                error=True,
            )
            assert sha() == initial_sha

            opened = await call(
                session,
                "edit_board_copper_batch",
                {
                    "pcb_path": str(BOARD),
                    "expected_sha256": initial_sha,
                    "remove_uuids": [n2],
                },
            )
            assert opened["status"] == "rejected" and opened["drc"]["unconnected_count"] > 0
            assert opened["source_unchanged"] and sha() == initial_sha

            shorted = await call(
                session,
                "edit_board_copper_batch",
                {
                    "pcb_path": str(BOARD),
                    "expected_sha256": initial_sha,
                    "traces": [
                        {
                            "net_name": "/N1",
                            "x1_mm": 25,
                            "y1_mm": 20,
                            "x2_mm": 25,
                            "y2_mm": 30,
                            "width_mm": 0.5,
                            "layer": "F.Cu",
                        }
                    ],
                },
            )
            assert shorted["status"] == "rejected" and shorted["drc"]["violation_count"] > 0
            assert shorted["source_unchanged"] and sha() == initial_sha

            required = [
                {"reference": reference, "pad_number": "1", "net_name": net}
                for reference, net in (("J1", "/N1"), ("J2", "/N1"), ("J3", "/N2"), ("J4", "/N2"))
            ]
            protected_groups = [
                {"reference": "J3", "pad_numbers": ["1"]},
                {"reference": "J4", "pad_numbers": ["1"]},
            ]
            routed = await call(
                session,
                "edit_board_copper_batch",
                {
                    "pcb_path": str(BOARD),
                    "expected_sha256": initial_sha,
                    "moves": [{"reference": "J1", "x_mm": 21, "y_mm": 20}],
                    "remove_uuids": [n1],
                    "autoroute_nets": ["/N1"],
                    "new_trace_widths": [{"net_name": "/N1", "width_mm": 0.4}],
                    "required_pads": required,
                    "protected_uuids": [n2],
                    "protected_nets": ["/N2"],
                    "protected_pad_groups": protected_groups,
                    "max_passes": 20,
                    "autoroute_timeout": 180,
                },
            )
            # This deliberately simple fixture is not routed by the installed Freerouting
            # pipeline. The batch wrapper must reject its optimistic "routed" result because
            # native DRC still sees an open, and must remove every routed scratch artifact.
            assert routed["status"] == "rejected", routed
            assert "drc" in routed, routed
            assert routed["drc"]["unconnected_count"] > 0
            assert routed["source_unchanged"] and sha() == initial_sha

            package = {
                "pcb_path": str(BOARD),
                "expected_sha256": initial_sha,
                "moves": [{"reference": "J1", "x_mm": 21, "y_mm": 20}],
                "remove_uuids": [n1],
                "traces": [
                    {
                        "net_name": "/N1",
                        "x1_mm": 21,
                        "y1_mm": 20,
                        "x2_mm": 30,
                        "y2_mm": 20,
                        "width_mm": 0.5,
                        "layer": "F.Cu",
                    },
                    {
                        "net_name": "/N1",
                        "x1_mm": 24,
                        "y1_mm": 20,
                        "x2_mm": 26,
                        "y2_mm": 20,
                        "width_mm": 0.5,
                        "layer": "B.Cu",
                    },
                ],
                "vias": [
                    {
                        "net_name": "/N1",
                        "x_mm": 24,
                        "y_mm": 20,
                        "diameter_mm": 1.0,
                        "drill_mm": 0.4,
                        "layers": ["F.Cu", "B.Cu"],
                    },
                    {
                        "net_name": "/N1",
                        "x_mm": 26,
                        "y_mm": 20,
                        "diameter_mm": 1.0,
                        "drill_mm": 0.4,
                        "layers": ["F.Cu", "B.Cu"],
                    },
                ],
                "required_pads": required,
                "protected_uuids": [n2],
                "protected_nets": ["/N2"],
                "protected_pad_groups": protected_groups,
            }
            dry = await call(session, "edit_board_copper_batch", package)
            assert dry["status"] == "dry_run_passed", dry
            assert dry["source_unchanged"] and sha() == initial_sha
            assert dry["drc"]["violation_count"] == dry["drc"]["unconnected_count"] == 0

            applied = await call(
                session,
                "edit_board_copper_batch",
                package | {"dry_run": False, "confirmation_token": dry["confirmation_token"]},
            )
            assert applied["status"] == "applied", applied
            assert applied["new_sha256"] == sha() != initial_sha

            final = await call(
                session,
                "get_board_edit_state",
                {"pcb_path": str(BOARD), "include_drc": True},
            )
            assert final["sha256"] == applied["new_sha256"]
            assert final["drc"]["violation_count"] == final["drc"]["unconnected_count"] == 0
            assert final["footprints"]["J1"]["position_mm"] == [21.0, 20.0]
            assert n1 not in final["copper"] and final["copper"][n2] == initial["copper"][n2]
            n1_items = [item for item in final["copper"].values() if item["net"] == "/N1"]
            assert any(item["type"] == "segment" and item["width_mm"] == 0.5 for item in n1_items)
            assert any(item["type"] == "via" and item["diameter_mm"] == 1.0 for item in n1_items)
            assert not list(BOARD.parent.glob(".kicad-batch-*"))

            await call(
                session,
                "edit_board_copper_batch",
                package,
                error=True,
            )
            assert sha() == final["sha256"]

            print(
                json.dumps(
                    {
                        "tools": sorted(name for name in tools if name in {"get_board_edit_state", "edit_board_copper_batch"}),
                        "initial_sha256": initial_sha,
                        "final_sha256": final["sha256"],
                        "positive": {"drc": final["drc"], "counts": final["counts"]},
                        "native_footprint_flip": {
                            "schema_layer": ["F.Cu", "B.Cu"],
                            "invalid_layer": "rejected",
                            "extra_side_field": "rejected",
                            "f_to_b": {"layer": back_u1["layer"], "pads": [pad["position_mm"] for pad in back_u1["pads"]]},
                            "b_to_f_roundtrip": "native-byte-parity-passed",
                            "combined": {
                                "layer": combined_u1["layer"],
                                "position_mm": combined_u1["position_mm"],
                                "rotation_deg": combined_u1["rotation_deg"],
                            },
                            "full_board_native_byte_parity": "passed",
                        },
                        "negative": {
                            "bad_uuid": "rejected",
                            "bad_confirmation_token": "rejected",
                            "sha_mismatch": "rejected",
                            "intentional_open": {"violations": opened["drc"]["violation_count"], "opens": opened["drc"]["unconnected_count"]},
                            "intentional_short": {"violations": shorted["drc"]["violation_count"], "opens": shorted["drc"]["unconnected_count"]},
                        },
                        "scoped_autoroute": {"status": routed["status"], "changes": routed["changes"], "drc": routed["drc"]},
                    },
                    sort_keys=True,
                )
            )


asyncio.run(main())
