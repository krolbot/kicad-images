"""Run typed installed-addon operations in an isolated KiCad IPC editor session."""
from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any


ADDONS = Path("/opt/kicad-addons")
PLUGINS = ADDONS / "3rdparty/plugins"

PLUGIN_PATHS = {
    "via_stitching": PLUGINS / "com_github_jOaSbA_via-stitching",
    "silk_reference": PLUGINS / "com_github_sijah_center-silk-ref",
    "fanout": PLUGINS / "vn_thanhduongvs_fanout-tool",
    "nccm": PLUGINS / "com_github_Mage-Control-Systems-Ltd_NCCM",
    "z0_netclasses": PLUGINS / "com_gitlab_egloff-studio_z0-field-solver",
    "kobee_label": PLUGINS / "com_github_mrcpuddington_kobeestudio/com.github.mrcpuddington.kobeestudio",
}


class IpcOperationError(RuntimeError):
    pass


def _require_number(request: dict[str, Any], name: str, *, minimum: float | None = None) -> float:
    value = request.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise IpcOperationError(f"{name} must be a finite number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise IpcOperationError(f"{name} must be at least {minimum}")
    return result


def _plugin(operation: str) -> Path:
    path = PLUGIN_PATHS[operation]
    if not path.is_dir():
        raise IpcOperationError(f"Installed addon source is missing: {path}")
    sys.path.insert(0, str(path))
    return path


def _via_stitching(board: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("via_stitching")
    module = importlib.import_module("via_stitching_action")
    layer_names = {str(name): getattr(module.BL, "BL_" + str(name).replace(".", "_"))
                   for name in request["layers"]}
    via_type = {
        "through": module.ViaType.VT_THROUGH,
        "blind_buried": module.ViaType.VT_BLIND_BURIED,
        "micro": module.ViaType.VT_MICRO,
    }[request["via_type"]]
    arguments = (
        board, via_type, layer_names[request["layers"][0]], layer_names[request["layers"][1]],
        request["net_name"], request["via_diameter_mm"], request["drill_mm"],
        request["spacing_mm"], request["pattern"], request["x_offset_mm"],
        request["y_offset_mm"], request["avoid_other_zones"], request["avoid_footprints"],
        request["avoid_same_net_pads"],
    )
    if request["action"] == "plan":
        plan = module.plan(*arguments)
        return {
            "planned_vias": len(plan.points),
            "candidate_positions": len(plan.candidates),
            "net": plan.net.name,
            "layers": [board.get_layer_name(layer) for layer, _ in plan.layers],
        }
    placed, grouped = module.stitch(*arguments)
    return {"placed_vias": placed, "grouped": grouped, "net": request["net_name"]}


def _silk_reference(board: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("silk_reference")
    module = importlib.import_module("center_silk_ref")
    opts = {key: value for key, value in request.items()
            if key not in {"operation", "input_pcb", "session_dir"}}
    return module.run_silk_ref_tools(board, opts)


def _fanout(board: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("fanout")
    module = importlib.import_module("fanout")
    utils = importlib.import_module("utils")
    from kipy.proto.board.board_types_pb2 import BoardLayer
    from kipy.util import from_mm

    footprint = next(
        (item for item in board.get_footprints()
         if item.reference_field is not None and item.reference_field.text.value == request["reference"]),
        None,
    )
    if footprint is None:
        raise IpcOperationError(f"Footprint not found: {request['reference']}")
    layer = getattr(BoardLayer, "BL_" + request["track_layer"].replace(".", "_"))
    engine = module.Fanout(
        footprint=footprint,
        board=board,
        via=utils.ViaData(
            via_type=request["via_type"], via_diameter=from_mm(request["via_diameter_mm"]),
            via_hole=from_mm(request["drill_mm"]),
            start_layer=getattr(BoardLayer, "BL_" + request["start_layer"].replace(".", "_")),
            end_layer=getattr(BoardLayer, "BL_" + request["end_layer"].replace(".", "_")),
            net=None, position=None,
        ),
        track=utils.TrackData(width=from_mm(request["track_width_mm"]), layer=layer,
                              net=None, start=None, end=None),
        package=request["package"], alignment=request["alignment"], direction=request["direction"],
        in_pad=request["in_pad"], unused_pad=request["connected_only"],
        fanout_length=from_mm(request["fanout_length_mm"]),
        stagger_gap=from_mm(request["stagger_gap_mm"]),
        via_pitch=from_mm(request["via_pitch_mm"]),
    )
    engine.fanout()
    if not engine.items:
        raise IpcOperationError("Fanout produced no tracks or vias")
    tracks = sum(type(item).__name__ == "Track" for item in engine.items)
    vias = sum(type(item).__name__ == "Via" for item in engine.items)
    return {"reference": request["reference"], "created_items": len(engine.items),
            "tracks": tracks, "vias": vias}


def _z0_netclasses(kicad: Any, board: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("z0_netclasses")
    module = importlib.import_module("z0solver.netclasses")
    version = module.kicad_version(kicad)
    blocked = module.write_blocked_reason(version)
    if blocked:
        raise IpcOperationError(blocked)
    project = board.get_project()
    current = module.read_classes(project)
    proposals = {
        name: {field: module.mm_to_nm(value) for field, value in fields.items()}
        for name, fields in request["proposals"].items()
    }
    preview = module.make_preview(current, proposals)
    if request["action"] == "preview":
        return {"changes": [change.__dict__ for change in preview.changes],
                "notes": preview.notes, "kicad_version": list(version)}
    success, message = module.apply(project, preview, current)
    if not success:
        raise IpcOperationError(message)
    return {"changes": [change.__dict__ for change in preview.changes],
            "notes": preview.notes, "message": message, "kicad_version": list(version)}


def _nccm(board: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("nccm")
    module = importlib.import_module("nccm_action")
    known = {netclass.name for netclass in board.get_project().get_net_classes()}
    rules = []
    for row in request["clearances"]:
        first, second = row["first"], row["second"]
        missing = {first, second} - known
        if missing:
            raise IpcOperationError("Unknown netclass: " + ", ".join(sorted(missing)))
        rules.append(
            f"\n(rule \"CLR_{first}_to_{second}\"\n"
            "  (severity error)\n"
            f"  (condition \"A.NetClass == '{first}' && B.NetClass == '{second}'\")\n"
            f"  (constraint clearance (min {row['clearance_mm']}mm))\n)\n"
        )
    if request["action"] == "preview":
        return {"netclasses": sorted(known), "rules": rules, "rule_count": len(rules)}
    dru = Path(request["input_pcb"]).with_suffix(".kicad_dru")
    existing = dru.read_text().splitlines(keepends=True) if dru.is_file() else []
    retained = module.get_or_remove_section(existing, "remove")
    if not any("(version 1)" in line.strip() for line in retained):
        retained.insert(0, "(version 1)\n")
    temporary = dru.with_suffix(dru.suffix + ".tmp")
    temporary.write_text("".join(retained) + module.SECTION_START_STR + "".join(rules)
                         + module.SECTION_END_STR)
    temporary.replace(dru)
    return {"netclasses": sorted(known), "rule_count": len(rules), "rules_file": str(dru)}


def _place_kobee_artwork(board: Any, kicad: Any, request: dict[str, Any], artwork: Any) -> dict[str, Any]:
    from kipy.geometry import Vector2
    from kobeestudio.integration.ipc_artwork import IpcArtworkPlacement
    from kobeestudio.integration.ipc_session import IpcSession
    session = IpcSession(kicad=kicad, board=board)
    placement = IpcArtworkPlacement(session)
    payload = {"format": "kobee-studio-composition", "document": artwork.document.to_dict()}
    footprint = placement.build_footprint(artwork, payload, request["layer"], request["layers"])
    footprint.position = Vector2.from_xy_mm(request["x_mm"], request["y_mm"])
    transaction = session.begin_commit()
    try:
        created = board.create_items(footprint)
        session.commit(transaction, "Place Kobee Studio artwork")
    except Exception:
        session.discard(transaction)
        raise
    if not created:
        raise IpcOperationError("Kobee Studio did not create a footprint")
    return {"reference": created[0].reference_field.text.value,
            "primitive_count": len(artwork.filled_polygons) + len(artwork.strokes),
            "layers": request["layers"], "x_mm": request["x_mm"], "y_mm": request["y_mm"]}


def _kobee_label(board: Any, kicad: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("kobee_label")
    from kobeestudio.core.composition import DocumentStyle, Padding, ShapeStyle, TypographyStyle
    from kobeestudio.core.studio_artwork import TextVectorizer, render_label_artwork
    from kobeestudio.core.text_geometry import TextGeometry

    typography = TypographyStyle(
        font_name=request["font"], height_mm=request["height_mm"], width_mm=request["width_mm"],
        line_spacing=request["line_spacing"], alignment=request["alignment"],
    )
    shape = ShapeStyle(
        padding=Padding.symmetric(request["padding_x_mm"], request["padding_y_mm"]),
        border_thickness_mm=request["border_thickness_mm"],
        corner_radius_mm=request["corner_radius_mm"], filled=request["filled"],
        inverted=request["inverted"], direction=request["direction"],
        start_cap=request["start_cap"], end_cap=request["end_cap"],
    )
    artwork = render_label_artwork(
        TextVectorizer(TextGeometry().buzzard), request["text"],
        DocumentStyle(typography=typography, shape=shape), request["layer"],
        shape=request["shape"], minimum_width_mm=request["minimum_width_mm"],
        inline_format=request["inline_format"], subtitle_text=request["subtitle"],
        underline=request["underline"],
    )
    return _place_kobee_artwork(board, kicad, request, artwork)


def _kobee_feature(board: Any, kicad: Any, request: dict[str, Any]) -> dict[str, Any]:
    _plugin("kobee_label")
    from kobeestudio.core.composition import DocumentStyle, TypographyStyle
    from kobeestudio.core.component_callout import ComponentCalloutSpec
    from kobeestudio.core.pin_header import PinHeaderSpec
    from kobeestudio.core.studio_artwork import (
        TextVectorizer, render_component_callout_artwork, render_header_artwork,
        render_machine_code_artwork,
    )
    from kobeestudio.core.text_geometry import TextGeometry

    vectorizer = TextVectorizer(TextGeometry().buzzard)
    kind = request["kind"]
    if kind == "machine_code":
        artwork = render_machine_code_artwork(
            request["payload"], request["code_type"], request["module_size_mm"],
            request["bar_height_mm"], request["layer"], vectorizer=vectorizer,
            presentation=request["presentation"], caption_text=request["caption"],
        )
    else:
        style = DocumentStyle(typography=TypographyStyle(
            font_name=request["font"], height_mm=request["height_mm"], alignment="center"))
        if kind == "pin_header":
            spec = PinHeaderSpec(
                pin_count=len(request["pin_labels"]), pin_labels=tuple(request["pin_labels"]),
                pitch_mm=request["pitch_mm"], orientation=request["orientation"],
                pin1_end=request["pin1_end"], label_side=request["label_side"],
                opening_mode=request["opening_mode"], output_layer=request["layer"], style=style,
            )
            artwork = render_header_artwork(vectorizer, spec)
        elif kind == "component_callout":
            spec = ComponentCalloutSpec(
                title=request["title"], subtitle=request["subtitle"],
                preset_id=request["preset_id"], component_width_mm=request["component_width_mm"],
                component_height_mm=request["component_height_mm"],
                component_clearance_mm=request["component_clearance_mm"],
                component_position=request["component_position"],
                output_layer=request["layer"], style=style,
            )
            artwork = render_component_callout_artwork(vectorizer, spec)
        else:
            raise IpcOperationError("Unknown Kobee Studio feature")
    return _place_kobee_artwork(board, kicad, request, artwork)


def _run_operation(kicad: Any, board: Any, request: dict[str, Any]) -> dict[str, Any]:
    operation = request["operation"]
    if operation == "via_stitching":
        return _via_stitching(board, request)
    if operation == "silk_reference":
        return _silk_reference(board, request)
    if operation == "fanout":
        return _fanout(board, request)
    if operation == "z0_netclasses":
        return _z0_netclasses(kicad, board, request)
    if operation == "nccm":
        return _nccm(board, request)
    if operation == "kobee_label":
        return _kobee_label(board, kicad, request)
    if operation == "kobee_feature":
        return _kobee_feature(board, kicad, request)
    raise IpcOperationError(f"Unknown IPC operation: {operation}")


def _configuration(root: Path) -> dict[str, str]:
    config = root / "home/.config/kicad/10.0"
    config.mkdir(parents=True)
    common = {"api": {"enable_server": True, "interpreter_path": "/usr/bin/python3"},
              "do_not_show_again": {"data_collection_prompt": True, "update_check_prompt": True}}
    (config / "kicad_common.json").write_text(json.dumps(common))
    (config / "kicad.json").write_text(json.dumps({"update_check": False, "pcm": {"update_check": False}}))
    for name in ("fp-lib-table", "sym-lib-table"):
        shutil.copyfile(Path("/usr/share/kicad/template") / name, config / name)
    (config / "design-block-lib-table").write_text("(design_block_lib_table (version 7))\n")
    return {
        **os.environ,
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home/.config"),
        "XDG_CACHE_HOME": str(root / "home/.cache"),
        "KICAD_CONFIG_HOME": str(config.parent),
        "KICAD_DOCUMENTS_HOME": str(root / "data"),
        "PYTHONPATH": "",
    }


def main() -> int:
    request = json.load(sys.stdin)
    board_path = Path(request["input_pcb"])
    root = Path(request["session_dir"])
    if not board_path.is_file() or not root.is_dir() or not board_path.resolve().is_relative_to(root.resolve()):
        raise IpcOperationError("IPC board and session must be an existing isolated staging tree")
    env = _configuration(root / ".ipc-session")
    os.environ.clear()
    os.environ.update(env)
    from kipy import KiCad

    with (root / "pcbnew.log").open("w") as log:
        child = subprocess.Popen(["pcbnew", str(board_path)], env=env, stdout=log, stderr=subprocess.STDOUT)
        kicad = None
        try:
            deadline = time.monotonic() + 90
            last_error = ""
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise IpcOperationError("PCB Editor exited before IPC became ready")
                try:
                    kicad = KiCad(timeout_ms=30000, client_name="Toolkit isolated addon session")
                    board = kicad.get_board()
                    break
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(1)
            else:
                raise IpcOperationError("Native IPC was not ready: " + last_error)
            result = _run_operation(kicad, board, request)
            board.save()
            time.sleep(0.5)
            print(json.dumps({"ok": True, "operation": request["operation"], "result": result},
                             allow_nan=False, sort_keys=True))
            return 0
        except Exception as exc:
            print(f"toolkit_ipc: {type(exc).__name__}: {exc}", file=sys.stderr)
            if (root / "pcbnew.log").is_file():
                print((root / "pcbnew.log").read_text(errors="replace")[-3000:], file=sys.stderr)
            print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}))
            return 1
        finally:
            if kicad is not None:
                with contextlib.suppress(Exception):
                    kicad.close()
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
