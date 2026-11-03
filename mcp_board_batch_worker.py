#!/usr/bin/env python3
"""Native pcbnew worker for bounded MCP board edits; JSON in/out over stdio."""

import json
import math
import sys
from pathlib import Path

import pcbnew


def _mm(value):
    return round(float(pcbnew.ToMM(value)), 6)


def _point(value):
    return [_mm(value.x), _mm(value.y)]


def _uuid(item):
    return str(item.m_Uuid.AsString())


def _layers(board, layer_set):
    return [str(board.GetLayerName(layer)) for layer in layer_set.Seq()]


def _pad(board, pad):
    return {
        "uuid": _uuid(pad),
        "number": str(pad.GetNumber()),
        "net": str(pad.GetNetname()),
        "position_mm": _point(pad.GetPosition()),
        "size_mm": _point(pad.GetSize()),
        "drill_mm": _point(pad.GetDrillSize()),
        "layers": _layers(board, pad.GetLayerSet()),
    }


def _copper(board, item):
    common = {"uuid": _uuid(item), "net": str(item.GetNetname())}
    if isinstance(item, pcbnew.PCB_VIA):
        top, bottom = item.TopLayer(), item.BottomLayer()
        return common | {
            "type": "via",
            "position_mm": _point(item.GetPosition()),
            "diameter_mm": _mm(item.GetWidth(top)),
            "drill_mm": _mm(item.GetDrillValue()),
            "layers": [str(board.GetLayerName(top)), str(board.GetLayerName(bottom))],
        }
    if isinstance(item, pcbnew.PCB_ARC):
        return common | {
            "type": "arc",
            "start_mm": _point(item.GetStart()),
            "mid_mm": _point(item.GetMid()),
            "end_mm": _point(item.GetEnd()),
            "width_mm": _mm(item.GetWidth()),
            "layer": str(item.GetLayerName()),
        }
    return common | {
        "type": "segment",
        "start_mm": _point(item.GetStart()),
        "end_mm": _point(item.GetEnd()),
        "width_mm": _mm(item.GetWidth()),
        "layer": str(item.GetLayerName()),
    }


def _snapshot(board):
    footprints = {}
    pads = {}
    for fp in board.GetFootprints():
        reference = str(fp.GetReference())
        fp_pads = sorted((_pad(board, pad) for pad in fp.Pads()), key=lambda item: (item["number"], item["uuid"]))
        footprints[reference] = {
            "uuid": _uuid(fp),
            "reference": reference,
            "position_mm": _point(fp.GetPosition()),
            "rotation_deg": round(float(fp.GetOrientationDegrees()), 6),
            "layer": str(fp.GetLayerName()),
            "pads": fp_pads,
        }
        for pad in fp_pads:
            pads[pad["uuid"]] = {"reference": reference} | pad
    copper = {_uuid(item): _copper(board, item) for item in board.GetTracks()}
    zones = {}
    for zone in board.Zones():
        zones[_uuid(zone)] = {
            "uuid": _uuid(zone),
            "net": str(zone.GetNetname()),
            "layers": _layers(board, zone.GetLayerSet()),
        }
    return {
        "footprints": footprints,
        "pads": pads,
        "copper": copper,
        "zones": zones,
        "nets": sorted(str(name) for name in board.GetNetInfo().NetsByName().keys()),
        "counts": {
            "footprints": len(footprints),
            "pads": len(pads),
            "segments": sum(item["type"] in {"segment", "arc"} for item in copper.values()),
            "vias": sum(item["type"] == "via" for item in copper.values()),
            "zones": len(zones),
        },
    }


def _net(board, name):
    item = board.GetNetInfo().GetNetItem(name)
    if item is None:
        raise ValueError(f"net not found: {name}")
    return item


def _layer(board, name):
    layer = board.GetLayerID(name)
    if layer < 0 or not board.IsLayerEnabled(layer):
        raise ValueError(f"enabled board layer not found: {name}")
    return layer


def _finite(value, name):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _apply(board, request):
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    for move in request.get("moves", []):
        reference = move["reference"]
        if reference not in footprints:
            raise ValueError(f"footprint not found: {reference}")
        fp = footprints[reference]
        fp.SetPosition(pcbnew.VECTOR2I_MM(_finite(move["x_mm"], "x_mm"), _finite(move["y_mm"], "y_mm")))
        target_layer = move.get("layer")
        if target_layer is not None:
            if target_layer not in {"F.Cu", "B.Cu"}:
                raise ValueError("footprint layer must be F.Cu or B.Cu")
            if fp.GetLayerName() not in {"F.Cu", "B.Cu"}:
                raise ValueError(f"footprint {reference} is not on F.Cu or B.Cu")
            if fp.GetLayerName() != target_layer:
                fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
                if fp.GetLayerName() != target_layer:
                    raise RuntimeError(f"native flip did not move {reference} to {target_layer}")
        if move.get("rotation_deg") is not None:
            fp.SetOrientationDegrees(_finite(move["rotation_deg"], "rotation_deg"))

    copper = {_uuid(item): item for item in board.GetTracks()}
    for uuid in request.get("remove_uuids", []):
        if uuid not in copper:
            raise ValueError(f"track/via UUID not found: {uuid}")
        board.Delete(copper[uuid])

    for spec in request.get("traces", []):
        trace = pcbnew.PCB_TRACK(board)
        trace.SetStart(pcbnew.VECTOR2I_MM(_finite(spec["x1_mm"], "x1_mm"), _finite(spec["y1_mm"], "y1_mm")))
        trace.SetEnd(pcbnew.VECTOR2I_MM(_finite(spec["x2_mm"], "x2_mm"), _finite(spec["y2_mm"], "y2_mm")))
        trace.SetWidth(pcbnew.FromMM(_finite(spec["width_mm"], "width_mm")))
        trace.SetLayer(_layer(board, spec["layer"]))
        trace.SetNet(_net(board, spec["net_name"]))
        board.Add(trace)

    for spec in request.get("vias", []):
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I_MM(_finite(spec["x_mm"], "x_mm"), _finite(spec["y_mm"], "y_mm")))
        via.SetWidth(pcbnew.FromMM(_finite(spec["diameter_mm"], "diameter_mm")))
        via.SetDrill(pcbnew.FromMM(_finite(spec["drill_mm"], "drill_mm")))
        layers = spec.get("layers") or ["F.Cu", "B.Cu"]
        if len(layers) != 2:
            raise ValueError("via layers must contain exactly two layer names")
        via.SetLayerPair(_layer(board, layers[0]), _layer(board, layers[1]))
        via.SetNet(_net(board, spec["net_name"]))
        board.Add(via)


def _refill(board):
    if board.GetAreaCount():
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())


def main():
    request = json.load(sys.stdin)
    path = Path(request["path"])
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        raise RuntimeError(f"pcbnew could not open {path}")

    action = request["action"]
    if action == "snapshot":
        result = _snapshot(board)
    elif action == "edit":
        _apply(board, request)
        _refill(board)
        pcbnew.SaveBoard(str(path), board)
        reopened = pcbnew.LoadBoard(str(path))
        if reopened is None:
            raise RuntimeError("pcbnew could not reopen edited board")
        result = _snapshot(reopened)
    elif action == "finalize":
        baseline = set(request.get("baseline_uuids", []))
        widths = request.get("new_trace_widths_mm", {})
        for item in board.GetTracks():
            if _uuid(item) in baseline or isinstance(item, pcbnew.PCB_VIA):
                continue
            width = widths.get(str(item.GetNetname()))
            if width is not None:
                item.SetWidth(pcbnew.FromMM(_finite(width, "new trace width")))
        _refill(board)
        pcbnew.SaveBoard(str(path), board)
        reopened = pcbnew.LoadBoard(str(path))
        if reopened is None:
            raise RuntimeError("pcbnew could not reopen finalized board")
        result = _snapshot(reopened)
    else:
        raise ValueError(f"unknown action: {action}")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
