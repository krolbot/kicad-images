#!/opt/kicad-tools/bin/python
"""Typed JSON bridge to the pinned KiCad placement and portability plugins.

Read one JSON request from stdin and write exactly one JSON response to stdout.
All upstream plugin diagnostics are redirected to stderr.
"""
from __future__ import annotations

import contextlib
import importlib
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any, NoReturn


class NativeError(ValueError):
    """A request error suitable for a typed MCP response."""


REPLICATION_SETTINGS = (
    "rep_tracks", "rep_zones", "rep_text", "rep_drawings",
    "group_layouts", "group_footprints", "group_tracks", "group_zones",
    "group_text", "group_drawings", "rep_locked_tracks", "rep_locked_zones",
    "rep_locked_text", "rep_locked_drawings", "intersecting", "group_items",
    "group_only", "locked_fps", "remove",
)
REPLICATION_DEFAULTS = dict(zip(REPLICATION_SETTINGS, (
    True, True, True, True,
    False, False, False, False, False, False,
    True, True, True, True,
    False, False, False, False, False,
)))
PLUGIN_DIRS = {
    "replicate": "com_github_MitjaNemec_ReplicateLayout",
    "place_footprints": "com_github_MitjaNemec_PlaceFootprints",
    "parts_placer": "com_github_TheJigsApp_kicad-parts-placer-pcm",
    "localize_dependencies": "com_github_jOaSbA_localize-dependencies",
}


def _fail(message: str) -> NoReturn:
    raise NativeError(message)


def _is_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        _fail(f"{name} must be a boolean")
    return value


def _is_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        _fail(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        _fail(f"{name} must be >= {minimum}")
    return value


def _finite(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        _fail(f"{name} must be a finite number")
    return float(value)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{name} must be a non-empty string")
    return value


def _absolute_path(value: Any, name: str) -> Path:
    path = Path(_string(value, name)).expanduser()
    if not path.is_absolute():
        _fail(f"{name} must be an absolute path")
    # Keep the lexical target: resolving here would hide a pre-existing dangling
    # output symlink, which must be rejected before the plugin can write through it.
    return Path(os.path.abspath(path))


def _input_pcb(value: Any) -> Path:
    path = _absolute_path(value, "input_pcb")
    if path.suffix != ".kicad_pcb" or not path.is_file():
        _fail("input_pcb must name an existing .kicad_pcb file")
    return path


def _output_pcb(value: Any, input_pcb: Path) -> Path:
    raw = _absolute_path(value, "output_pcb")
    if raw.suffix != ".kicad_pcb":
        _fail("output_pcb must end in .kicad_pcb")
    if raw.exists() or raw.is_symlink():
        _fail("output_pcb already exists")
    if not raw.parent.is_dir():
        _fail("output_pcb parent directory does not exist")
    if raw == input_pcb:
        _fail("output_pcb must differ from input_pcb")
    return raw


def _plugin_root() -> Path:
    configured = os.environ.get("KICAD_NATIVE_PLUGIN_ROOT")
    candidates = [Path(configured)] if configured else [
        Path("/opt/kicad-addons/3rdparty/plugins"),
    ]
    for root in candidates:
        if root.is_dir():
            return root.resolve()
    _fail("KiCad PCM plugin root is unavailable; seed the pinned addons first")


def _load_plugin(kind: str, module_name: str):
    directory = _plugin_root() / PLUGIN_DIRS[kind]
    if not directory.is_dir():
        _fail(f"Pinned {kind} plugin is not installed at {directory}")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    sys.modules.pop(module_name, None)
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - depends on KiCad's runtime ABI
        _fail(f"Cannot import pinned {kind} engine: {exc}")


def _load_pcbnew():
    try:
        return importlib.import_module("pcbnew")
    except Exception as exc:  # pragma: no cover - exercised in the image
        _fail("pcbnew is unavailable; invoke with /opt/kicad-tools/bin/python: " + str(exc))


def _load_board(pcbnew, path: Path):
    board = pcbnew.LoadBoard(str(path))
    if board is None:
        _fail(f"KiCad could not load {path}")
    return board


def _board_refs(board) -> set[str]:
    refs = [footprint.GetReference() for footprint in board.GetFootprints()]
    if len(refs) != len(set(refs)):
        _fail("input_pcb contains duplicate footprint references")
    return set(refs)


def _refs(value: Any, available: set[str], name: str = "references") -> list[str]:
    if not isinstance(value, list) or not value:
        _fail(f"{name} must be a non-empty array")
    refs = [_string(ref, f"{name}[]") for ref in value]
    if len(refs) != len(set(refs)):
        _fail(f"{name} must not contain duplicates")
    missing = sorted(set(refs) - available)
    if missing:
        _fail(f"{name} are not on input_pcb: {', '.join(missing)}")
    return refs


def _save_board(pcbnew, board, output: Path) -> None:
    pcbnew.SaveBoard(str(output), board)
    if not output.is_file() or output.stat().st_size == 0:
        _fail(f"KiCad did not create output_pcb: {output}")


def _assert_file_unchanged(path: Path, before: bytes) -> None:
    if path.read_bytes() != before:
        _fail(f"Refusing success: source file changed unexpectedly: {path}")


def _settings(value: Any) -> dict[str, bool]:
    if value is None:
        return dict(REPLICATION_DEFAULTS)
    if not isinstance(value, dict):
        _fail("settings must be an object")
    unknown = sorted(set(value) - set(REPLICATION_SETTINGS))
    if unknown:
        _fail("Unknown replication settings: " + ", ".join(unknown))
    return {key: _is_bool(value.get(key, default), f"settings.{key}")
            for key, default in REPLICATION_DEFAULTS.items()}


def _sheet_path(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _fail(f"{name} must be a non-empty array of sheet UUIDs")
    return tuple(_string(part, f"{name}[]") for part in value)


def _replicator_for(board, anchor_ref: str):
    engine = _load_plugin("replicate", "replicate_layout")
    try:
        replicator = engine.Replicator(board, anchor_ref, update_func=_stderr_progress)
    except Exception as exc:
        _fail(f"Replicate Layout could not inspect anchor {anchor_ref}: {exc}")
    anchor = replicator.get_fp_by_ref(anchor_ref)
    if anchor is None:
        _fail(f"anchor_ref is not on input_pcb: {anchor_ref}")
    if not anchor.sheet_id or not anchor.filename:
        _fail("anchor_ref must belong to a hierarchical sheet")
    return engine, replicator, anchor


def _stderr_progress(stage, percentage, message=None) -> None:
    text = message if message is not None else f"stage={stage} progress={percentage}"
    print(f"Replicate Layout: {text}", file=sys.stderr, flush=True)


def replicate_discover(request: dict[str, Any]) -> dict[str, Any]:
    input_pcb = _input_pcb(request.get("input_pcb"))
    anchor_ref = _string(request.get("anchor_ref"), "anchor_ref")
    pcbnew = _load_pcbnew()
    board = _load_board(pcbnew, input_pcb)
    available = _board_refs(board)
    if anchor_ref not in available:
        _fail(f"anchor_ref is not on input_pcb: {anchor_ref}")
    _, replicator, anchor = _replicator_for(board, anchor_ref)
    levels = []
    for index, (sheet_id, filename) in enumerate(zip(anchor.sheet_id, anchor.filename)):
        destinations = replicator.get_sheets_to_replicate(anchor, sheet_id)
        levels.append({
            "level_index": index,
            "sheet_id": sheet_id,
            "sheet_file": filename,
            "source_sheet": list(anchor.sheet_id[:index + 1]),
            "destination_sheets": [list(sheet) for sheet in destinations],
        })
    matches = replicator.get_list_of_footprints_with_same_id(anchor.fp_id)
    return {
        "input_pcb": str(input_pcb),
        "anchor_ref": anchor_ref,
        "anchor_footprint_id": anchor.fp_id,
        "matching_anchor_refs": [item.ref for item in matches],
        "levels": levels,
        "settings_defaults": dict(REPLICATION_DEFAULTS),
        "remove_duplicates_default": False,
    }


def replicate_layout(request: dict[str, Any]) -> dict[str, Any]:
    input_pcb = _input_pcb(request.get("input_pcb"))
    output_pcb = _output_pcb(request.get("output_pcb"), input_pcb)
    anchor_ref = _string(request.get("anchor_ref"), "anchor_ref")
    level_index = _is_int(request.get("level_index"), "level_index", minimum=0)
    destinations_value = request.get("destination_sheets")
    if not isinstance(destinations_value, list) or not destinations_value:
        _fail("destination_sheets must be a non-empty array")
    destinations = [_sheet_path(sheet, "destination_sheets[]") for sheet in destinations_value]
    if len(destinations) != len(set(destinations)):
        _fail("destination_sheets must not contain duplicates")
    settings = _settings(request.get("settings"))
    remove_duplicates = _is_bool(request.get("remove_duplicates", False), "remove_duplicates")

    pcbnew = _load_pcbnew()
    board = _load_board(pcbnew, input_pcb)
    available = _board_refs(board)
    if anchor_ref not in available:
        _fail(f"anchor_ref is not on input_pcb: {anchor_ref}")
    engine, replicator, anchor = _replicator_for(board, anchor_ref)
    if level_index >= len(anchor.sheet_id):
        _fail("level_index is outside anchor_ref hierarchy")
    source_sheet = tuple(anchor.sheet_id[:level_index + 1])
    offered = {tuple(sheet) for sheet in replicator.get_sheets_to_replicate(anchor, anchor.sheet_id[level_index])}
    invalid = sorted(set(destinations) - offered)
    if invalid:
        _fail("destination_sheets contains a hierarchy not offered for this anchor/level")

    source_bytes = input_pcb.read_bytes()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            replicator.replicate_layout(
                anchor, list(source_sheet), [list(sheet) for sheet in destinations],
                engine.Settings(**settings), remove_duplicates,
            )
        _save_board(pcbnew, board, output_pcb)
        _assert_file_unchanged(input_pcb, source_bytes)
    except Exception:
        output_pcb.unlink(missing_ok=True)
        raise
    return {
        "input_pcb": str(input_pcb), "output_pcb": str(output_pcb),
        "anchor_ref": anchor_ref, "source_sheet": list(source_sheet),
        "destination_sheets": [list(sheet) for sheet in destinations],
        "settings": settings, "remove_duplicates": remove_duplicates,
        "connectivity_issues": [list(issue) for issue in sorted(replicator.connectivity_issues)],
    }


def _layout(request: dict[str, Any]) -> tuple[str, dict[str, float | int]]:
    layout = request.get("layout")
    if not isinstance(layout, dict):
        _fail("layout must be an object")
    kind = _string(layout.get("kind"), "layout.kind")
    if kind not in {"linear", "circular", "matrix"}:
        _fail("layout.kind must be linear, circular, or matrix")
    common = {
        "rotation_step": _is_int(layout.get("rotation_step", 1), "layout.rotation_step", minimum=1),
        "rotation_deg": _finite(layout.get("rotation_deg", 0), "layout.rotation_deg"),
    }
    if kind == "circular":
        common |= {
            "radius_mm": _finite(layout.get("radius_mm"), "layout.radius_mm"),
            "delta_angle_deg": _finite(layout.get("delta_angle_deg"), "layout.delta_angle_deg"),
            "delta_radius_mm": _finite(layout.get("delta_radius_mm", 0), "layout.delta_radius_mm"),
        }
    else:
        common |= {
            "step_x_mm": _finite(layout.get("step_x_mm"), "layout.step_x_mm"),
            "step_y_mm": _finite(layout.get("step_y_mm"), "layout.step_y_mm"),
        }
        if kind == "matrix":
            common["columns"] = _is_int(layout.get("columns"), "layout.columns", minimum=1)
    return kind, common


def place_footprints(request: dict[str, Any]) -> dict[str, Any]:
    input_pcb = _input_pcb(request.get("input_pcb"))
    output_pcb = _output_pcb(request.get("output_pcb"), input_pcb)
    anchor_ref = _string(request.get("anchor_ref"), "anchor_ref")
    kind, layout = _layout(request)
    copy_text_items = _is_bool(request.get("copy_text_items", True), "copy_text_items")

    pcbnew = _load_pcbnew()
    board = _load_board(pcbnew, input_pcb)
    refs = _refs(request.get("references"), _board_refs(board))
    if anchor_ref not in refs:
        _fail("anchor_ref must be included in references")
    engine = _load_plugin("place_footprints", "place_footprints")
    try:
        placer = engine.Placer(board)
    except Exception as exc:
        _fail(f"Place Footprints could not inspect input_pcb: {exc}")
    if placer.get_fp_by_ref(anchor_ref) is None:
        _fail(f"anchor_ref is not on input_pcb: {anchor_ref}")

    source_bytes = input_pcb.read_bytes()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if kind == "linear":
                placer.place_linear(refs, anchor_ref, layout["step_x_mm"], layout["step_y_mm"],
                                    layout["rotation_step"], layout["rotation_deg"], copy_text_items)
            elif kind == "circular":
                placer.place_circular(refs, anchor_ref, layout["radius_mm"], layout["delta_angle_deg"],
                                      layout["delta_radius_mm"], layout["rotation_step"],
                                      layout["rotation_deg"], copy_text_items)
            else:
                placer.place_matrix(refs, anchor_ref, layout["step_x_mm"], layout["step_y_mm"],
                                    layout["columns"], layout["rotation_step"],
                                    layout["rotation_deg"], copy_text_items)
        _save_board(pcbnew, board, output_pcb)
        _assert_file_unchanged(input_pcb, source_bytes)
    except Exception:
        output_pcb.unlink(missing_ok=True)
        raise
    return {"input_pcb": str(input_pcb), "output_pcb": str(output_pcb), "anchor_ref": anchor_ref,
            "references": refs, "layout": {"kind": kind, **layout}, "copy_text_items": copy_text_items}


def _placement_entries(value: Any, available: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail("placements must be a non-empty array")
    allowed = {"ref", "x_mm", "y_mm", "rotation_deg", "side"}
    entries = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _fail(f"placements[{index}] must be an object")
        unknown = sorted(set(item) - allowed)
        if unknown:
            _fail(f"placements[{index}] has unknown fields: {', '.join(unknown)}")
        ref = _string(item.get("ref"), f"placements[{index}].ref")
        side = item.get("side", "current")
        if side not in {"front", "back", "current"}:
            _fail(f"placements[{index}].side must be front, back, or current")
        entries.append({
            "refdes": ref,
            "x": _finite(item.get("x_mm"), f"placements[{index}].x_mm"),
            "y": _finite(item.get("y_mm"), f"placements[{index}].y_mm"),
            "rotation": _finite(item.get("rotation_deg", 0), f"placements[{index}].rotation_deg"),
            "side": side,
        })
    refs = [entry["refdes"] for entry in entries]
    if len(refs) != len(set(refs)):
        _fail("placements must not contain duplicate refs")
    missing = sorted(set(refs) - available)
    if missing:
        _fail("placements refs are not on input_pcb: " + ", ".join(missing))
    return entries


def _point(value: Any, name: str) -> tuple[float, float]:
    if value is None:
        return 0.0, 0.0
    if not isinstance(value, list) or len(value) != 2:
        _fail(f"{name} must be [x_mm, y_mm]")
    return _finite(value[0], f"{name}[0]"), _finite(value[1], f"{name}[1]")


def parts_place(request: dict[str, Any]) -> dict[str, Any]:
    input_pcb = _input_pcb(request.get("input_pcb"))
    output_pcb = _output_pcb(request.get("output_pcb"), input_pcb)
    origin = _point(request.get("origin_mm"), "origin_mm")
    group_name = request.get("group_name")
    if group_name is not None:
        group_name = _string(group_name, "group_name")

    pcbnew = _load_pcbnew()
    board = _load_board(pcbnew, input_pcb)
    entries = _placement_entries(request.get("placements"), _board_refs(board))
    engine = _load_plugin("parts_placer", "kicad_parts_placer_")
    dataframe = _load_plugin("parts_placer", "dataframe_lite_")
    components = dataframe.DataFrame(entries)
    components = engine.setup_dataframe(components)
    valid, errors = engine.check_input_valid(components)
    if not valid:
        _fail("Parts Placer rejected placements: " + "; ".join(errors))
    missing = engine.get_missing_references(board, components)
    if missing:
        _fail("Parts Placer could not find refs: " + ", ".join(missing))

    source_bytes = input_pcb.read_bytes()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine.place_parts(board, components, origin=origin)
            if group_name is not None:
                engine.group_parts(board, components, group_name=group_name)
        _save_board(pcbnew, board, output_pcb)
        _assert_file_unchanged(input_pcb, source_bytes)
    except Exception:
        output_pcb.unlink(missing_ok=True)
        raise
    return {"input_pcb": str(input_pcb), "output_pcb": str(output_pcb), "origin_mm": list(origin),
            "placements": [{"ref": item["refdes"], "x_mm": item["x"], "y_mm": item["y"],
                            "rotation_deg": item["rotation"], "side": item["side"]} for item in entries],
            "group_name": group_name}


def _input_project(value: Any) -> Path:
    root = _absolute_path(value, "input_project_dir")
    if not root.is_dir():
        _fail("input_project_dir must be an existing directory")
    if not any(root.glob("*.kicad_pcb")) and not any(root.glob("*.kicad_sch")):
        _fail("input_project_dir must contain a .kicad_pcb or .kicad_sch file")
    for path in root.rglob("*"):
        if path.is_symlink():
            _fail(f"input_project_dir may not contain symlinks: {path}")
    return root


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _output_project(value: Any, input_project: Path) -> Path:
    output = _absolute_path(value, "output_project_dir")
    if output.exists() or output.is_symlink():
        _fail("output_project_dir already exists")
    if not output.parent.is_dir():
        _fail("output_project_dir parent directory does not exist")
    if _inside(output, input_project) or _inside(input_project, output):
        _fail("input_project_dir and output_project_dir must not overlap")
    return output


def _snapshot_tree(root: Path) -> dict[Path, bytes]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(root)] = hashlib.file_digest(stream, "sha256").digest()
    return result


def _assert_tree_unchanged(root: Path, before: dict[Path, bytes]) -> None:
    after = _snapshot_tree(root)
    if after != before:
        _fail("Refusing success: input_project_dir changed unexpectedly")


def _scan_rows(engine, project: Path, include_standard: bool) -> list[dict[str, Any]]:
    return engine.scan_items(str(project), include_standard)


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["key"] = list(row["key"])
    return out


def dependency_scan(request: dict[str, Any]) -> dict[str, Any]:
    project = _input_project(request.get("input_project_dir"))
    include_standard = _is_bool(request.get("include_standard", False), "include_standard")
    engine = _load_plugin("localize_dependencies", "portability")
    with contextlib.redirect_stdout(sys.stderr):
        rows = _scan_rows(engine, project, include_standard)
    return {"input_project_dir": str(project), "include_standard": include_standard,
            "items": [_json_row(row) for row in rows]}


def _selection_keys(value: Any, rows: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
    known = {tuple(row["key"]): row for row in rows}
    if value is None:
        selected = {key for key, row in known.items() if row.get("wanted")}
    else:
        if not isinstance(value, list):
            _fail("selection_keys must be an array")
        selected = set()
        for index, key in enumerate(value):
            if not isinstance(key, list) or not key:
                _fail(f"selection_keys[{index}] must be a non-empty key array returned by dependency_scan")
            normalized = tuple(key)
            if normalized not in known:
                _fail("selection_keys contains a key not returned by dependency_scan")
            selected.add(normalized)
    if not selected:
        _fail("No localizable dependencies selected")
    invalid = [known[key] for key in selected if known[key].get("status") not in {"external", "standard"}]
    if invalid:
        _fail("selection_keys may contain only external or standard dependencies")
    return selected


def _archive_path(value: Any, input_project: Path, output_project: Path) -> Path | None:
    if value is None:
        return None
    path = _absolute_path(value, "archive_path")
    if path.suffix != ".zip":
        _fail("archive_path must end in .zip")
    if path.exists() or path.is_symlink():
        _fail("archive_path already exists")
    if not path.parent.is_dir():
        _fail("archive_path parent directory does not exist")
    if _inside(path, input_project) or _inside(path, output_project):
        _fail("archive_path must be outside input_project_dir and output_project_dir")
    return path


def _validate_dependencies(engine, project: Path, rows: list[dict[str, Any]], include_standard: bool) -> None:
    """Bound both upstream-generated names and indirect reads before any output.

    The engine resolves project library tables and environment variables. Those
    are data, not permission to copy arbitrary container files into an archive.
    """
    roots = [Path("/projects"), Path("/usr/share/kicad"), Path("/opt/kicad-addons")]
    resolve = engine.make_resolver(str(project))
    for row in rows:
        if row["status"] == "standard" and not include_standard:
            _fail("Standard dependencies require include_standard=true")
        source = Path(resolve(row["source"]))
        if row["type"] in {"symbol", "footprint"}:
            parts = row["name"].split(":", 1)
            for part in parts:
                if not part or part in {".", ".."} or any(c in part for c in '/\\"\n\r'):
                    _fail("Unsafe library nickname or item name in dependency")
            if row["type"] == "footprint":
                if len(parts) != 2:
                    _fail("Footprint dependency must have library:item name")
                source = source / (parts[1] + ".kicad_mod")
        candidates = [source]
        if row["type"] == "model":
            candidates += [source.with_suffix(ext) for ext in (*engine.TWIN_EXTS, *(e.upper() for e in engine.TWIN_EXTS))]
        for candidate in candidates:
            resolved = candidate.resolve()
            if not candidate.is_absolute() or not any(_inside(resolved, root) for root in roots):
                _fail("Dependency source must stay within /projects or bundled KiCad libraries")
        if not source.is_file():
            _fail("Selected dependency source file does not exist")


def dependency_localize(request: dict[str, Any]) -> dict[str, Any]:
    input_project = _input_project(request.get("input_project_dir"))
    output_project = _output_project(request.get("output_project_dir"), input_project)
    include_standard = _is_bool(request.get("include_standard", False), "include_standard")
    archive_path = _archive_path(request.get("archive_path"), input_project, output_project)
    engine = _load_plugin("localize_dependencies", "portability")
    with contextlib.redirect_stdout(sys.stderr):
        source_rows = _scan_rows(engine, input_project, include_standard)
    selected_keys = _selection_keys(request.get("selection_keys"), source_rows)
    selected = [row for row in source_rows if tuple(row["key"]) in selected_keys]
    _validate_dependencies(engine, input_project, selected, include_standard)
    source_before = _snapshot_tree(input_project)

    try:
        shutil.copytree(input_project, output_project)
        with contextlib.redirect_stdout(sys.stderr):
            output_rows = _scan_rows(engine, output_project, include_standard)
            selected_rows = [row for row in output_rows if tuple(row["key"]) in selected_keys]
            if len(selected_rows) != len(selected_keys):
                _fail("Copied project no longer matches dependency_scan selection")
            _validate_dependencies(engine, output_project, selected_rows, include_standard)
            session = engine.Session(str(output_project))
            result = engine.apply_selection(session, selected_rows)
            session.save_manifest(result)
        _assert_tree_unchanged(input_project, source_before)
        if archive_path is not None:
            created = Path(shutil.make_archive(str(archive_path.with_suffix("")), "zip",
                                                root_dir=output_project.parent,
                                                base_dir=output_project.name))
            if created != archive_path or not archive_path.is_file():
                _fail("Could not create archive_path")
    except Exception:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        shutil.rmtree(output_project, ignore_errors=True)
        raise
    return {"input_project_dir": str(input_project), "output_project_dir": str(output_project),
            "selected_keys": [list(key) for key in sorted(selected_keys)], "result": result,
            "archive_path": str(archive_path) if archive_path else None}


OPERATIONS = {
    "replicate_discover": replicate_discover,
    "replicate_layout": replicate_layout,
    "place_footprints": place_footprints,
    "parts_place": parts_place,
    "dependency_scan": dependency_scan,
    "dependency_localize": dependency_localize,
}


def run_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        _fail("Request must be a JSON object")
    operation = _string(request.get("operation"), "operation")
    handler = OPERATIONS.get(operation)
    if handler is None:
        _fail("Unknown operation: " + operation)
    return {"ok": True, "operation": operation, "result": handler(request)}


def main() -> int:
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            response = run_request(request)
        status = 0
    except (NativeError, json.JSONDecodeError, OSError) as exc:
        print(f"toolkit_native: {exc}", file=sys.stderr)
        response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        status = 2
    except Exception as exc:  # upstream engines do not expose typed exceptions
        print(f"toolkit_native upstream failure: {exc}", file=sys.stderr)
        response = {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}
        status = 1
    print(json.dumps(response, allow_nan=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
