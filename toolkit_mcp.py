"""Typed local-toolkit operations registered beside the pinned upstream MCP tools."""
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from typing import Annotated, Iterator, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from toolkit_native import REPLICATION_DEFAULTS

from mcp_server_kicad import _cst, server as upstream

PROJECT_ROOT = Path("/projects")
TOOLS_PYTHON = "/opt/kicad-tools/bin/python"
NATIVE_RUNNER = Path(__file__).with_name("toolkit_native.py")
LAYERS = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
_EXPORT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

ADDON_COVERAGE: dict[str, JsonValue] = {
    "com.github.yaqwsx.kikit": {"status": "partial", "tools": ["panelize_pcb", "manufacture_pcb", "run_drc"], "remaining": "KiKit present/separate/stencil and general preset surface are not published"},
    "com.github.yaqwsx.kikit-library": {"status": "full", "tools": ["list_lib_footprints", "get_footprint_info"], "operations": "native footprint-library discovery/read"},
    "org.openscopeproject.InteractiveHtmlBom": {"status": "full", "tools": ["generate_interactive_bom"], "operations": "self-contained assembly HTML"},
    "com.github.gregdavill.KiBuzzard": {"status": "partial", "tools": ["generate_kibuzzard_footprint"], "remaining": "native footprint generation is published; direct board insertion is intentionally not advertised as staged addon behavior"},
    "com.github.MitjaNemec.ReplicateLayout": {"status": "full", "tools": ["discover_replication", "replicate_layout"], "operations": "hierarchy discovery and layout replication"},
    "com.github.MitjaNemec.PlaceFootprints": {"status": "full", "tools": ["place_footprints"], "operations": "linear, matrix and circular placement"},
    "com.github.TheJigsApp.kicadtestpoints-pcm": {"status": "full", "tools": ["export_testpoints"], "operations": "property or explicit-pad CSV export"},
    "com.github.TheJigsApp.kicad-parts-placer-pcm": {"status": "full", "tools": ["place_parts"], "operations": "Cartesian placement, side, rotation and grouping"},
    "com.github.jOaSbA.localize-dependencies": {"status": "full", "tools": ["scan_project_dependencies", "localize_project_dependencies"], "operations": "scan, copy, localize, manifest and ZIP"},
    "com.github.jOaSbA.via-stitching": {"status": "blocked", "tools": [], "remaining": "polygon/zone stitching requires an active KiCad IPC editor session; generic via insertion is not represented as addon coverage"},
    "com.github.ozzysv.viafence": {"status": "blocked", "tools": [], "remaining": "placement orchestration is embedded in a modal SWIG GUI action with editor selection and no callable headless entrypoint"},
    "com.github.ozzysv.holefinder": {"status": "full", "tools": ["list_pcb_footprints", "get_footprint_pads", "list_pcb_traces"], "operations": "native pad/via hole inventory with dimensions and coordinates"},
    "de.markh.kivar": {"status": "full", "tools": ["apply_variant"], "operations": "rule validation, aspect/choice application and saved board"},
    "com.github.easyw.kicad-rf-tools": {"status": "blocked", "tools": [], "remaining": "taper, rounding, mask expansion and path-derived fence orchestration are modal selection actions; generic board mutations are not addon coverage"},
    "com.github.easyw.kicad-action-tools": {"status": "partial", "tools": ["export_positions"], "remaining": "fabrication position export is covered; snap-to-grid, drawing-layer moves and missing-3D report are modal selection actions"},
    "com.github.g200kg.kicad-gerberzipper2": {"status": "full", "tools": ["manufacture_pcb", "export_gerbers", "export_positions", "export_bom"], "operations": "Gerber, drill, placement and BOM package artifacts"},
    "com.github.sijah.center-silk-ref": {"status": "blocked", "tools": [], "remaining": "batch reference placement is exposed only through a live KiCad IPC GUI plugin"},
    "com.github.Mage-Control-Systems-Ltd.NCCM": {"status": "blocked", "tools": [], "remaining": "pairwise clearance-matrix rule generation requires the plugin's live IPC project context; generic netclass mutation is not addon coverage"},
    "com.github.jacmie.PosOrient": {"status": "full", "tools": ["export_positions", "place_parts"], "operations": "position/orientation export and deterministic reapplication"},
    "com.pcbtools.kinotes": {"status": "blocked", "tools": [], "remaining": "editor-note UI has no board-file operation or headless API"},
    "com.github.lou270.pinout-image-generator": {"status": "full", "tools": ["generate_pinout_image"], "operations": "pad extraction, board render, annotated SVG and PNG"},
    "com.github.cgrassin.kicad-pinout-generator": {"status": "full", "tools": ["generate_pinout"], "operations": "list, CSV, HTML, Markdown, C, Python, WireViz, XDC and PDC"},
    "vn.thanhduongvs.component-position": {"status": "full", "tools": ["export_positions", "get_footprint_info"], "operations": "component coordinates, side and rotation export"},
    "vn.thanhduongvs.fanout-tool": {"status": "blocked", "tools": [], "remaining": "fanout creation is implemented only against a live KiCad IPC editor session"},
    "com.github.danielboston38.prefab-gate": {"status": "full", "tools": ["run_prefab_gate"], "operations": "DRC/parity/ampacity check and guarded fabrication package"},
    "com.gitlab.egloff-studio.z0-field-solver": {"status": "partial", "tools": ["solve_trace_impedance"], "remaining": "Z0/Zdiff and width/gap solving are published; live-project netclass application is not a staged addon operation"},
    "com.github.mrcpuddington.kobeestudio": {"status": "blocked", "tools": [], "remaining": "Kobee Studio's graphics editor is live-IPC-only; KiBuzzard and generic text tools are separate capabilities"},
    "com.github.sparkfun.SparkFun-KiCad-Libraries": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "com.github.alarm-siren.arduino-kicad-library": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "org.fabcloud.gitlab.electronics.kicad": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "com.github.espressif.kicad-libraries": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
}

ENGINE_COVERAGE: dict[str, JsonValue] = {
    "KiBot": {"status": "partial", "tools": ["manufacture_pcb"], "remaining": "fixed safe production outputs only; uploaded KiBot scripts/config are intentionally forbidden"},
    "KiKit": {"status": "partial", "tools": ["panelize_pcb"], "remaining": "fixed grid panel; present/separate/stencil are not published"},
    "PcbDraw": {"status": "full", "tools": ["render_pcb"], "operations": "stylized SVG/PNG and 3D PNG"},
    "KiAuto": {"status": "internal", "tools": ["manufacture_pcb"], "operations": "KiBot internal automation, no separate installed command"},
    "KiDiff": {"status": "internal", "tools": ["manufacture_pcb"], "operations": "KiBot internal diff support, no separate installed command"},
    "KiBoM": {"status": "full", "tools": ["export_bom", "manufacture_pcb"], "operations": "BOM export"},
    "SKiDL": {"status": "not_applicable", "tools": [], "remaining": "circuit authoring/conversion is not an operation on a saved KiCad board"},
    "WireViz": {"status": "full", "tools": ["generate_pinout"], "operations": "WireViz connector YAML export"},
    "KiCost": {"status": "blocked", "tools": [], "remaining": "pricing requires external supplier/network configuration and is outside offline CAD mutation scope"},
}

mcp = MCPServer(
    "kicad",
    version=importlib.metadata.version("mcp-server-kicad"),
    instructions=(upstream.mcp.instructions or "").replace("There are 109 tools;", "The upstream core has 109 tools;")
    + "\n\nTOOLKIT: get_toolkit_status discovers bundled engines. Use manufacture_pcb for KiBot production output, "
      "panelize_pcb for KiKit panels and generate_interactive_bom for assembly HTML. "
      "render_pcb uses the pinned PcbDraw engine for stylized or 3D board images. "
      "discover_replication precedes replicate_layout; place_footprints and place_parts create new boards. "
      "scan_project_dependencies precedes localize_project_dependencies, which works only on a project copy. "
      "export_testpoints, generate_pinout, generate_pinout_image, generate_kibuzzard_footprint, apply_variant, solve_trace_impedance and run_prefab_gate expose their pinned addon engines. "
      "Toolkit operations accept absolute paths within /projects, preserve their inputs and require a NEW output directory. "
      "An existing output is an error, not permission to replace it. Generated artifacts are not an electrical approval; use run_erc/run_drc.",
)


def _path(value: str, suffix: str | None = None, *, new: bool = False, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ToolError("Use an absolute path within /projects")
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT_ROOT.resolve()) or resolved == PROJECT_ROOT.resolve():
        raise ToolError("The path must be inside /projects, not its root or a symlink outside it")
    if new:
        if path.exists() or path.is_symlink():
            raise ToolError(f"Output already exists: {path}; choose a new output directory")
    elif not (resolved.is_dir() if directory else resolved.is_file()):
        raise ToolError(f"Input {'directory' if directory else 'file'} does not exist: {path}")
    if suffix and resolved.suffix != suffix:
        raise ToolError(f"Expected a {suffix} file: {path}")
    return resolved


def _execute(command: list[str], cwd: Path, timeout: int = 180, request: dict[str, object] | None = None) -> str:
    """No shell and no arbitrary user argv. Kill the entire child group on timeout."""
    with subprocess.Popen(command, cwd=cwd, text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
        try:
            stdout, stderr = child.communicate(json.dumps(request) if request is not None else None, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
            raise ToolError(f"Toolkit operation exceeded {timeout}s; its process group was stopped") from exc
        if child.returncode:
            raise ToolError(f"{Path(command[0]).name} failed (exit {child.returncode}): {(stderr or stdout)[-6000:]}")
        return stdout


def _project_tables(source: Path) -> list[Path]:
    tables = []
    for name in ("fp-lib-table", "sym-lib-table"):
        path = source.parent / name
        if path.is_symlink():
            raise ToolError(f"Project library table must not be a symlink: {path}")
        if path.is_file():
            tables.append(path)
    return tables


@contextmanager
def _job(pcb_path: str, output_dir: str) -> Iterator[tuple[Path, Path, Path]]:
    source = _path(pcb_path, ".kicad_pcb")
    destination = _path(output_dir, new=True)
    original = hashlib.sha256(source.read_bytes()).digest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting another request's output.
    destination.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix=".toolkit-", dir=destination) as temporary:
            workspace = Path(temporary)
            board = workspace / source.name
            shutil.copyfile(source, board)
            project = source.with_suffix(".kicad_pro")
            if project.is_file():
                shutil.copyfile(project, board.with_suffix(".kicad_pro"))
            for table in _project_tables(source):
                shutil.copyfile(table, workspace / table.name)
            yield board, destination, workspace
            if hashlib.sha256(source.read_bytes()).digest() != original:
                raise ToolError("The source board changed concurrently; the result was not accepted")
    except BaseException:
        shutil.rmtree(destination)
        raise


def _result(destination: Path) -> dict[str, JsonValue]:
    files = []
    for path in sorted(destination.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(destination):
            raise ToolError("Backend produced a symlink or path outside its output directory")
        if path.is_file():
            files.append({"path": str(path), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not files or not any(row["bytes"] for row in files):
        raise ToolError("The backend produced no nonempty artifacts")
    return {"output_dir": str(destination), "files": files, "source_modified": False}


@mcp.tool(annotations=_READ)
def get_toolkit_status() -> dict[str, JsonValue]:
    """Discover pinned plugin versions and the installed command-line engines; no project writes."""
    bundle = Path("/opt/kicad-addons/installed_packages.json")
    packages = json.loads(bundle.read_text())["packages"]
    versions = _execute([TOOLS_PYTHON, "-c", "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['kibot','kikit','pcbdraw']}))"], PROJECT_ROOT)
    return {"project_root": str(PROJECT_ROOT), "kicad": _execute(["kicad-cli", "version"], PROJECT_ROOT).strip(),
            "mcp": importlib.metadata.version("mcp-server-kicad"), "engines": json.loads(versions),
            "plugins": {p["package"]["identifier"]: p["current_version"] for p in packages},
            "addon_coverage": ADDON_COVERAGE, "engine_coverage": ENGINE_COVERAGE}


@mcp.tool(annotations=_EXPORT)
def manufacture_pcb(pcb_path: str, output_dir: str,
                    outputs: list[Literal["gerber", "drill", "positions", "pdf"]] | None = None) -> dict[str, JsonValue]:
    """Generate production files with KiBot, without changing the source board/project.

    outputs selects Gerber, Excellon drill, CSV placement and per-layer PDF; defaults to all four.
    Uses fixed local output definitions, no uploaded configuration, hooks or supplier network requests.
    output_dir must not exist. Run native ERC/DRC separately before manufacturing a real board.
    """
    selected = ["gerber", "drill", "positions", "pdf"] if outputs is None else outputs
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= {"gerber", "drill", "positions", "pdf"}:
        raise ToolError("Select one or more distinct supported output types")
    definitions = {
        "gerber": {"name": "gerbers", "type": "gerber", "dir": "gerber", "layers": LAYERS},
        "drill": {"name": "drills", "type": "excellon", "dir": "drill"},
        "positions": {"name": "assembly_positions", "type": "position", "dir": "assembly", "options": {"format": "CSV", "units": "millimeters"}},
        "pdf": {"name": "layer_pdf", "type": "pdf", "dir": "pdf", "layers": ["F.Cu", "B.Cu", "F.Silkscreen", "Edge.Cuts"]},
    }
    with _job(pcb_path, output_dir) as (board, destination, work):
        config = work / "fabrication.kibot.yaml"
        config.write_text(json.dumps({"kibot": {"version": 1}, "outputs": [definitions[key] for key in selected]}))
        _execute(["kibot", "-A", "-b", str(board), "-c", str(config), "-d", str(destination)], work, timeout=600)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def generate_interactive_bom(pcb_path: str, output_dir: str, include_tracks: bool = True,
                             include_nets: bool = True, dark_mode: bool = False,
                             layer_view: Literal["F", "FB", "B"] = "FB") -> dict[str, JsonValue]:
    """Generate a self-contained InteractiveHtmlBom assembly HTML using the pinned plugin engine.

    Runs without a GUI/browser, preserves the source and requires a new output directory.
    """
    if layer_view not in {"F", "FB", "B"}:
        raise ToolError("layer_view must be F, FB or B")
    with _job(pcb_path, output_dir) as (board, destination, work):
        command = ["xvfb-run", "-a", "ibom", "--no-browser", "--no-compression", "--dest-dir", str(destination),
                   "--name-format", "ibom", "--layer-view", layer_view]
        if include_tracks:
            command.append("--include-tracks")
        if include_nets:
            command.append("--include-nets")
        if dark_mode:
            command.append("--dark-mode")
        _execute(command + [str(board)], work)
        if "pcbdata" not in (destination / "ibom.html").read_text():
            raise ToolError("InteractiveHtmlBom returned no embedded PCB data")
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def panelize_pcb(pcb_path: str, output_dir: str,
                 rows: Annotated[int, Field(ge=1, le=20)] = 1,
                 columns: Annotated[int, Field(ge=1, le=20)] = 2,
                 spacing_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 3,
                 tab_width_mm: Annotated[float, Field(gt=0, le=50, allow_inf_nan=False)] = 3,
                 cuts: Literal["none", "vcuts", "mousebites"] = "none") -> dict[str, JsonValue]:
    """Create a KiKit grid panel with fixed connecting tabs in a NEW output directory.

    rows*columns is bounded to 100 boards. The original PCB is never rewritten.
    Inspect native DRC and fabrication constraints on the resulting panel before release.
    """
    if isinstance(rows, bool) or isinstance(columns, bool) or not 1 <= rows <= 20 or not 1 <= columns <= 20 or rows * columns > 100:
        raise ToolError("Use 1..20 rows/columns and at most 100 board instances")
    if not math.isfinite(spacing_mm) or not 0 <= spacing_mm <= 100 or not math.isfinite(tab_width_mm) or not 0 < tab_width_mm <= 50:
        raise ToolError("Use finite spacing 0..100mm and tab width >0..50mm")
    if cuts not in {"none", "vcuts", "mousebites"}:
        raise ToolError("Unsupported cut type")
    with _job(pcb_path, output_dir) as (board, destination, work):
        _execute(["kikit", "panelize", "--layout", f"grid; rows: {rows}; cols: {columns}; space: {spacing_mm}mm",
                  "--tabs", f"fixed; width: {tab_width_mm}mm; vcount: 1; hcount: 1", "--cuts", cuts,
                  str(board), str(destination / "panel.kicad_pcb")], work, timeout=600)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def render_pcb(pcb_path: str, output_dir: str,
               mode: Literal["stylized", "3d"] = "stylized",
               side: Literal["front", "back"] = "front",
               format: Literal["svg", "png"] = "svg",
               components: bool = True, copper: bool = True,
               dpi: Annotated[int, Field(ge=36, le=2400)] = 300) -> dict[str, JsonValue]:
    """Render a board with the pinned PcbDraw engine into a NEW output directory.

    stylized supports SVG/PNG and optional components/copper. 3d uses kicad-cli's
    renderer through PcbDraw and requires PNG. The source board is never rewritten.
    """
    if mode == "3d" and format != "png":
        raise ToolError("3d rendering requires format=png")
    with _job(pcb_path, output_dir) as (board, destination, work):
        output = destination / ("board." + format)
        if mode == "stylized":
            command = ["pcbdraw", "plot", "--side", side, "--dpi", str(dpi),
                       "--components" if components else "--no-components",
                       "--copper" if copper else "--no-copper", str(board), str(output)]
        else:
            command = ["pcbdraw", "render", "--side", side, "--renderer", "normal",
                       "--projection", "orthographic", "--transparent", str(board), str(output)]
        _execute(command, work, timeout=600)
    return _result(destination)


FiniteMM = Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)]
Angle = Annotated[float, Field(ge=-36000, le=36000, allow_inf_nan=False)]
References = Annotated[list[str], Field(min_length=1, max_length=10000)]


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReplicationSettings(Parameters):
    rep_tracks: bool = True
    rep_zones: bool = True
    rep_text: bool = True
    rep_drawings: bool = True
    group_layouts: bool = False
    group_footprints: bool = False
    group_tracks: bool = False
    group_zones: bool = False
    group_text: bool = False
    group_drawings: bool = False
    rep_locked_tracks: bool = True
    rep_locked_zones: bool = True
    rep_locked_text: bool = True
    rep_locked_drawings: bool = True
    intersecting: bool = False
    group_items: bool = False
    group_only: bool = False
    locked_fps: bool = False
    remove: bool = False


class LinearPlacement(Parameters):
    kind: Literal["linear"]
    step_x_mm: FiniteMM
    step_y_mm: FiniteMM
    rotation_step: Annotated[int, Field(ge=1, le=10000)] = 1
    rotation_deg: Angle = 0


class MatrixPlacement(Parameters):
    kind: Literal["matrix"]
    step_x_mm: FiniteMM
    step_y_mm: FiniteMM
    rotation_step: Annotated[int, Field(ge=1, le=10000)] = 1
    rotation_deg: Angle = 0
    columns: Annotated[int, Field(ge=1, le=10000)]


class CircularPlacement(Parameters):
    kind: Literal["circular"]
    radius_mm: Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)]
    delta_angle_deg: Angle
    delta_radius_mm: FiniteMM = 0
    rotation_step: Annotated[int, Field(ge=1, le=10000)] = 1
    rotation_deg: Angle = 0


class PartPlacement(Parameters):
    ref: str
    x_mm: FiniteMM
    y_mm: FiniteMM
    rotation_deg: Angle = 0
    side: Literal["front", "back", "current"] = "current"


class TestPointPad(Parameters):
    reference: str
    pad: str


def _native(operation: str, cwd: Path, **parameters: object) -> dict[str, JsonValue]:
    text = _execute([TOOLS_PYTHON, str(NATIVE_RUNNER)], cwd, timeout=600,
                    request={"operation": operation, **parameters})
    try:
        response = json.loads(text)
        if response["ok"] is not True or not isinstance(response["result"], dict):
            raise ValueError("not a successful object result")
        return response["result"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError("Native plugin returned an invalid JSON response") from exc


def _schematics(board: Path) -> list[Path]:
    """Copy only the referenced hierarchy, not unrelated or previous output trees."""
    root = board.with_suffix(".kicad_sch")
    pending = [root] if root.is_file() else []
    found = set()
    while pending:
        path = pending.pop()
        if path.is_symlink():
            raise ToolError("Schematic dependencies must not be symlinks")
        path = _path(str(path), ".kicad_sch")
        if not path.is_relative_to(board.parent):
            raise ToolError("Schematic hierarchy must stay within the source project directory")
        if path in found:
            continue
        found.add(path)
        tree = _cst.parse(path.read_bytes())
        root_node = tree.find("kicad_sch")
        if root_node is None:
            raise ToolError("Expected a native KiCad schematic")
        for sheet in root_node.find_all("sheet"):
            for prop in sheet.find_all("property"):
                atoms = prop.atoms
                if len(atoms) >= 3 and atoms[1].text in {"Sheetfile", "Sheet file"}:
                    pending.append(path.parent / atoms[2].text)
    return sorted(found)


def _native_board(operation: str, pcb_path: str, output_dir: str, **parameters: object) -> dict[str, JsonValue]:
    source = _path(pcb_path, ".kicad_pcb")
    # Enumerate before creating the staging tree; never discover our own copies.
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work):
        for path in schematics:
            if path.is_symlink():
                raise ToolError("Schematic dependencies must not be symlinks")
            _path(str(path), ".kicad_sch")
            target = work / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        result = _native(operation, work, input_pcb=str(board),
                         output_pcb=str(destination / source.name), **parameters)
        # Retain the hierarchy and project rules beside the new board so native
        # reopening and a subsequent replication do not lose their context.
        for path in schematics:
            relative = path.relative_to(source.parent)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / relative, target)
        project = board.with_suffix(".kicad_pro")
        if project.is_file():
            shutil.copyfile(project, destination / project.name)
        for table in _project_tables(source):
            shutil.copyfile(work / table.name, destination / table.name)
    # Do not advertise the removed staging input as an artifact.
    result["input_pcb"] = str(source)
    return {**_result(destination), "operation": result}


@mcp.tool(annotations=_READ)
def discover_replication(pcb_path: str, anchor_ref: str) -> dict[str, JsonValue]:
    """Inspect Replicate Layout anchors, hierarchy destinations and settings before copying layout.

    destination_sheets are the exact paths returned by the installed engine; do not invent them.
    """
    board = _path(pcb_path, ".kicad_pcb")
    return _native("replicate_discover", board.parent, input_pcb=str(board), anchor_ref=anchor_ref)


@mcp.tool(annotations=_EXPORT)
def replicate_layout(pcb_path: str, output_dir: str, anchor_ref: str,
                     level_index: Annotated[int, Field(ge=0)],
                     destination_sheets: list[list[str]],
                     settings: ReplicationSettings | None = None,
                     remove_duplicates: bool = False) -> dict[str, JsonValue]:
    """Run the pinned Replicate Layout engine and save a NEW board; original stays unchanged.

    First call discover_replication and use its level_index/destination_sheets. Tracks,
    zones, text, drawings and locking/group options are the upstream engine's settings.
    """
    return _native_board("replicate_layout", pcb_path, output_dir, anchor_ref=anchor_ref,
                         level_index=level_index, destination_sheets=destination_sheets,
                         settings=settings.model_dump() if settings else REPLICATION_DEFAULTS,
                         remove_duplicates=remove_duplicates)


@mcp.tool(annotations=_EXPORT)
def place_footprints(pcb_path: str, output_dir: str, anchor_ref: str, references: References,
                     layout: Annotated[LinearPlacement | MatrixPlacement | CircularPlacement, Field(discriminator="kind")],
                     copy_text_items: bool = True) -> dict[str, JsonValue]:
    """Run Place Footprints linear/circular/matrix placement on a NEW board.

    References are ordered; anchor_ref must be included. Distances are millimetres,
    angles are degrees. No GUI selection or unsaved editor state is used.
    """
    return _native_board("place_footprints", pcb_path, output_dir, anchor_ref=anchor_ref,
                         references=references, layout=layout.model_dump(), copy_text_items=copy_text_items)


@mcp.tool(annotations=_EXPORT)
def place_parts(pcb_path: str, output_dir: str,
                placements: Annotated[list[PartPlacement], Field(min_length=1, max_length=10000)],
                origin_mm: tuple[FiniteMM, FiniteMM] = (0, 0),
                group_name: str | None = None) -> dict[str, JsonValue]:
    """Run Parts Placer on a NEW board using its CARTESIAN input (Y increases upward).

    x/y are millimetres relative to origin_mm in KiCad coordinates; the engine maps
    (x,y) to (origin_x+x, origin_y-y). Rotation/side follow Parts Placer semantics.
    Optionally group the placed parts. Unlisted components are retained.
    """
    return _native_board("parts_place", pcb_path, output_dir,
                         placements=[item.model_dump() for item in placements],
                         origin_mm=list(origin_mm), group_name=group_name)


@mcp.tool(annotations=_READ)
def scan_project_dependencies(project_dir: str, include_standard: bool = False) -> dict[str, JsonValue]:
    """Read Localize Dependencies selection keys for saved project files; no mutation.

    Upstream scans top-level schematic/board files only. Missing/unresolved entries
    are reported, not silently treated as localized. Standard libraries are opt-in.
    """
    project = _path(project_dir, directory=True)
    return _native("dependency_scan", project, input_project_dir=str(project), include_standard=include_standard)


@mcp.tool(annotations=_EXPORT)
def localize_project_dependencies(project_dir: str, output_dir: str,
                                  include_standard: bool = False,
                                  selection_keys: list[list[str | int]] | None = None,
                                  create_archive: bool = False) -> dict[str, JsonValue]:
    """Run Localize Dependencies on a COPY in a NEW output directory, never in place.

    Select exact keys returned by scan_project_dependencies, or omit for upstream
    wanted entries. Standard dependencies require include_standard=true. The plugin
    scans top-level CAD files; symbols are localized per library, footprints per item.
    Optional ZIP contains the resulting copy. Source project must contain no symlinks.
    """
    project = _path(project_dir, directory=True)
    destination = _path(output_dir, new=True)
    if destination.is_relative_to(project) or project.is_relative_to(destination):
        raise ToolError("Input project and output directory must not overlap")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        result = _native("dependency_localize", project,
                         input_project_dir=str(project), output_project_dir=str(destination / project.name),
                         include_standard=include_standard, selection_keys=selection_keys,
                         archive_path=str(destination / (project.name + ".zip")) if create_archive else None)
        return {**_result(destination), "operation": result}
    except BaseException:
        shutil.rmtree(destination)
        raise


@mcp.tool(annotations=_EXPORT)
def export_testpoints(pcb_path: str, output_dir: str,
                      pads: Annotated[list[TestPointPad], Field(min_length=1, max_length=10000)] | None = None,
                      use_aux_origin: bool = False) -> dict[str, JsonValue]:
    """Export the pinned KiCad Test Points CSV from explicit ref/pad pairs or test-point pad properties.

    Omitting pads selects pads with KiCad's test-point property. Coordinates match Gerber
    Cartesian orientation; use_aux_origin applies the board auxiliary origin. Source is preserved.
    """
    with _job(pcb_path, output_dir) as (board, destination, work):
        result = _native("testpoints_export", work, input_pcb=str(board),
                         output_file=str(destination / "testpoints.csv"),
                         pads=None if pads is None else [item.model_dump() for item in pads],
                         use_aux_origin=use_aux_origin)
    return {**_result(destination), "operation": result}


@mcp.tool(annotations=_EXPORT)
def generate_pinout(pcb_path: str, output_dir: str, references: References,
                    format: Literal["list", "csv", "html", "markdown", "c_define", "c_enum",
                                    "python_dict", "wireviz", "fpga_xdc", "fpga_pdc"] = "markdown",
                    pin_name_filter: str | None = None) -> dict[str, JsonValue]:
    """Generate a pinout with the pinned Pinout Generator engine for exact footprint references.

    The optional regex prefix filter extracts numeric pin identifiers for C/Python formats.
    Multi-footprint c_define/fpga_xdc/fpga_pdc output is rejected like the addon GUI.
    """
    suffix = {"list": ".txt", "csv": ".csv", "html": ".html", "markdown": ".md",
              "c_define": ".h", "c_enum": ".h", "python_dict": ".py",
              "wireviz": ".yml", "fpga_xdc": ".xdc", "fpga_pdc": ".pdc"}[format]
    with _job(pcb_path, output_dir) as (board, destination, work):
        result = _native("pinout_export", work, input_pcb=str(board),
                         output_file=str(destination / ("pinout" + suffix)),
                         references=references, format=format, pin_name_filter=pin_name_filter)
    return {**_result(destination), "operation": result}


@mcp.tool(annotations=_EXPORT)
def apply_variant(pcb_path: str, output_dir: str, choices: dict[str, str]) -> dict[str, JsonValue]:
    """Apply exact KiVar aspect/choice rules to a NEW board and preserve the source board.

    Rules come from the footprints' installed KiVar fields. Unknown aspects or choices and
    invalid rule sets are rejected; the response lists every change reported by KiVar.
    """
    return _native_board("variant_apply", pcb_path, output_dir, choices=choices)


@mcp.tool(annotations=_READ)
def solve_trace_impedance(
    kind: Literal["microstrip", "stripline", "cpw"],
    width_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)],
    copper_thickness_um: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)],
    dielectric_height_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)],
    dielectric_er: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)],
    gap_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None,
    upper_height_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0,
    upper_er: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0,
    soldermask_thickness_um: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0,
    soldermask_er: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 3.3,
    quality: Literal["schnell", "standard", "fein"] = "standard",
    solve_for: Literal["none", "width", "gap", "width_for_diff"] = "none",
    target_ohms: Annotated[float, Field(gt=0, le=10000, allow_inf_nan=False)] | None = None,
) -> dict[str, JsonValue]:
    """Run the pinned Z0 Field Solver for Z0/Zdiff or solve width/gap for a target impedance.

    Dimensions use mm except copper/soldermask thickness in um. gap is required for CPW,
    solve_for=gap and solve_for=width_for_diff. target_ohms is required when solving geometry.
    """
    return _native("impedance_solve", PROJECT_ROOT, kind=kind, width_mm=width_mm,
                   copper_thickness_um=copper_thickness_um,
                   dielectric_height_mm=dielectric_height_mm, dielectric_er=dielectric_er,
                   gap_mm=gap_mm, upper_height_mm=upper_height_mm, upper_er=upper_er,
                   soldermask_thickness_um=soldermask_thickness_um, soldermask_er=soldermask_er,
                   quality=quality, solve_for=solve_for, target_ohms=target_ohms)


@mcp.tool(annotations=_EXPORT)
def run_prefab_gate(pcb_path: str, output_dir: str,
                    command: Literal["check", "package"] = "check",
                    strict: bool = False, no_ampacity: bool = False,
                    no_parity: bool = False) -> dict[str, JsonValue]:
    """Run the pinned Pre-fab Gate on a staged copy and save its JSON report.

    package additionally emits the gate-approved fabrication package. A blocked board is a
    successful tool call with passed=false and exact findings; engine/environment failure is an error.
    Source board/project are preserved and output_dir must be new.
    """
    source = _path(pcb_path, ".kicad_pcb")
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work):
        for path in schematics:
            target = work / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        result = _native("prefab_gate", work, input_pcb=str(board), command=command,
                         strict=strict, no_ampacity=no_ampacity, no_parity=no_parity,
                         output_dir=str(destination / "fab") if command == "package" else None)
        (destination / "prefab-gate.json").write_text(json.dumps(result["report"], indent=2, sort_keys=True))
    return {**_result(destination), "operation": result}


@mcp.tool(annotations=_EXPORT)
def generate_pinout_image(
    pcb_path: str, output_dir: str, references: References | None = None,
    export_png: bool = True,
    png_dpi: Annotated[int, Field(ge=36, le=2400)] = 300,
    label_orientation: Literal["auto", "horizontal", "vertical"] = "auto",
) -> dict[str, JsonValue]:
    """Generate the pinned Pinout Image Generator's annotated SVG and optional PNG.

    Exact references are optional; when omitted the addon detects connector-like footprints,
    then falls back to all footprints with pads. Board rendering and pin extraction use a staged copy.
    """
    with _job(pcb_path, output_dir) as (board, destination, work):
        result = _native("pinout_image_export", work, input_pcb=str(board),
                         output_svg=str(destination / "pinout.svg"), references=references,
                         export_png=export_png, png_dpi=png_dpi,
                         label_orientation=label_orientation)
    return {**_result(destination), "operation": result}


@mcp.tool(annotations=_EXPORT)
def generate_kibuzzard_footprint(
    output_dir: str, text: Annotated[str, Field(min_length=1, max_length=128)],
    height_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 2,
    width_mm: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0,
    font: str = "UbuntuMono-B",
    layer: Literal["F.Cu", "F.Paste", "F.SilkS", "F.Mask", "F.Cu/F.Mask"] = "F.SilkS",
    alignment: Literal["Left", "Center", "Right"] = "Center",
    left_cap: Literal["none", "round", "square", "pointer", "forward_slash", "back_slash", "flag_tail"] = "none",
    right_cap: Literal["none", "round", "square", "pointer", "forward_slash", "back_slash", "flag_tail"] = "none",
    padding_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.25,
    line_spacing: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.5,
    inline_format: bool = False,
) -> dict[str, JsonValue]:
    """Generate a NEW native .kicad_mod label with the pinned KiBuzzard engine.

    Output is a reusable footprint artifact; this does not mutate a board or use the GUI clipboard.
    """
    destination = _path(output_dir, new=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        result = _native("kibuzzard_generate", destination,
                         output_file=str(destination / "kibuzzard.kicad_mod"), text=text,
                         height_mm=height_mm, width_mm=width_mm, font=font, layer=layer,
                         alignment=alignment, left_cap=left_cap, right_cap=right_cap,
                         padding_mm=padding_mm, line_spacing=line_spacing,
                         inline_format=inline_format)
        return {**_result(destination), "operation": result}
    except BaseException:
        shutil.rmtree(destination)
        raise


# Reuse the pinned upstream composition function, preserving every original tool.
for _module in [upstream.schematic, upstream.pcb, upstream.symbol, upstream.footprint, upstream.project]:
    upstream._copy_tools(_module.mcp, mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
