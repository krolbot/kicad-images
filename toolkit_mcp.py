"""Typed local-toolkit operations registered beside the pinned upstream MCP tools."""
import base64
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import math
import mimetypes
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
from typing import Annotated, Iterator, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from toolkit_native import REPLICATION_DEFAULTS

import mcp_board_batch as board_batch
from mcp_server_kicad import _cst, server as upstream

PROJECT_ROOT = Path("/projects")
MAX_PROJECT_FILE_BYTES = 16 * 1024 * 1024
TOOLS_PYTHON = "/opt/kicad-tools/bin/python"
NATIVE_RUNNER = Path(__file__).with_name("toolkit_native.py")
IPC_RUNNER = Path(__file__).with_name("toolkit_ipc.py")
IPC_PYTHONS = {
    "via_stitching": "/opt/kicad-addons/python-environments/com.github.jOaSbA.via-stitching/bin/python",
    "silk_reference": "/opt/kicad-addons/python-environments/com.github.sijah.center-silk-ref/bin/python",
    "fanout": "/opt/kicad-addons/python-environments/vn.thanhduongvs.fanout-tool/bin/python",
    "nccm": "/opt/kicad-addons/python-environments/com.github.Mage-Control-Systems.NCCM/bin/python",
    "z0_netclasses": "/opt/kicad-addons/python-environments/com.gitlab.egloff-studio.z0-field-solver/bin/python",
    "kobee_label": "/opt/kicad-addons/python-environments/com.github.mrcpuddington.kobeestudio/bin/python",
    "kobee_feature": "/opt/kicad-addons/python-environments/com.github.mrcpuddington.kobeestudio/bin/python",
}
LAYERS = ["F.Cu", "B.Cu", "F.Paste", "B.Paste", "F.Mask", "B.Mask", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts"]
_EXPORT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

ADDON_COVERAGE: dict[str, JsonValue] = {
    "com.github.yaqwsx.kikit": {"status": "full", "tools": ["panelize_pcb", "separate_pcb", "create_stencil", "present_pcb", "manufacture_pcb", "run_drc"], "operations": "typed panelization, board separation, stencil generation, presentation pages, fabrication and DRC without uploaded presets or plugins"},
    "com.github.yaqwsx.kikit-library": {"status": "full", "tools": ["list_lib_footprints", "get_footprint_info"], "operations": "native footprint-library discovery/read"},
    "org.openscopeproject.InteractiveHtmlBom": {"status": "full", "tools": ["generate_interactive_bom"], "operations": "self-contained assembly HTML"},
    "com.github.gregdavill.KiBuzzard": {"status": "full", "tools": ["generate_kibuzzard_footprint", "insert_kibuzzard_footprint"], "operations": "native footprint generation and staged native board insertion"},
    "com.github.MitjaNemec.ReplicateLayout": {"status": "full", "tools": ["discover_replication", "replicate_layout"], "operations": "hierarchy discovery and layout replication"},
    "com.github.MitjaNemec.PlaceFootprints": {"status": "full", "tools": ["place_footprints"], "operations": "linear, matrix and circular placement"},
    "com.github.TheJigsApp.kicadtestpoints-pcm": {"status": "full", "tools": ["export_testpoints"], "operations": "property or explicit-pad CSV export"},
    "com.github.TheJigsApp.kicad-parts-placer-pcm": {"status": "full", "tools": ["place_parts"], "operations": "Cartesian placement, side, rotation and grouping"},
    "com.github.jOaSbA.localize-dependencies": {"status": "full", "tools": ["scan_project_dependencies", "localize_project_dependencies"], "operations": "scan, copy, localize, manifest and ZIP"},
    "com.github.jOaSbA.via-stitching": {"status": "full", "tools": ["stitch_vias"], "operations": "native IPC planning and zone-overlap stitching on an isolated board"},
    "com.github.ozzysv.viafence": {"status": "full", "tools": ["apply_via_fence"], "operations": "typed native track/arc/pad fence placement on an isolated board"},
    "com.github.ozzysv.holefinder": {"status": "full", "tools": ["list_pcb_footprints", "get_footprint_pads", "list_pcb_traces"], "operations": "native pad/via hole inventory with dimensions and coordinates"},
    "de.markh.kivar": {"status": "full", "tools": ["apply_variant"], "operations": "rule validation, aspect/choice application and saved board"},
    "com.github.easyw.kicad-rf-tools": {"status": "full", "tools": ["run_rf_tool"], "operations": "track length, rounding/reconnect/removal, tapers, solder-mask expansion, clearance and path-derived via fence"},
    "com.github.easyw.kicad-action-tools": {"status": "full", "tools": ["export_positions", "run_pcb_action_tool"], "operations": "fabrication position export, snap-to-grid, drawing layer moves, missing-3D report and annular check"},
    "com.github.g200kg.kicad-gerberzipper2": {"status": "full", "tools": ["manufacture_pcb", "export_gerbers", "export_positions", "export_bom"], "operations": "Gerber, drill, placement and BOM package artifacts"},
    "com.github.sijah.center-silk-ref": {"status": "full", "tools": ["edit_silk_references"], "operations": "native IPC placement, visibility and sizing on an isolated board"},
    "com.github.Mage-Control-Systems-Ltd.NCCM": {"status": "full", "tools": ["update_clearance_matrix"], "operations": "native IPC netclass validation plus bounded NCCM rules-section preview/write on an isolated project"},
    "com.github.jacmie.PosOrient": {"status": "full", "tools": ["export_positions", "place_parts"], "operations": "position/orientation export and deterministic reapplication"},
    "com.pcbtools.kinotes": {"status": "full", "tools": ["manage_project_notes"], "operations": "atomic notes, todos and version-log read/write plus changelog export on an isolated project copy"},
    "com.github.lou270.pinout-image-generator": {"status": "full", "tools": ["generate_pinout_image"], "operations": "pad extraction, board render, annotated SVG and PNG"},
    "com.github.cgrassin.kicad-pinout-generator": {"status": "full", "tools": ["generate_pinout"], "operations": "list, CSV, HTML, Markdown, C, Python, WireViz, XDC and PDC"},
    "vn.thanhduongvs.component-position": {"status": "full", "tools": ["export_positions", "get_footprint_info"], "operations": "component coordinates, side and rotation export"},
    "vn.thanhduongvs.fanout-tool": {"status": "full", "tools": ["fanout_footprint"], "operations": "native IPC BGA, SOIC/QFN, connector and via-in-pad fanout on an isolated board"},
    "com.github.danielboston38.prefab-gate": {"status": "full", "tools": ["run_prefab_gate"], "operations": "DRC/parity/ampacity check and guarded fabrication package"},
    "com.gitlab.egloff-studio.z0-field-solver": {"status": "full", "tools": ["solve_trace_impedance", "apply_impedance_netclasses"], "operations": "Z0/Zdiff solving plus guarded native IPC netclass preview/write/read-back on an isolated project"},
    "com.github.mrcpuddington.kobeestudio": {"status": "full", "tools": ["place_kobee_label", "place_kobee_artwork"], "operations": "native IPC labels, QR/Code 128 artwork, pin-header rails and component callouts with typed composition"},
    "com.github.sparkfun.SparkFun-KiCad-Libraries": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "com.github.alarm-siren.arduino-kicad-library": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "org.fabcloud.gitlab.electronics.kicad": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
    "com.github.espressif.kicad-libraries": {"status": "full", "tools": ["list_lib_symbols", "list_lib_footprints"], "operations": "native library discovery/read"},
}

ENGINE_COVERAGE: dict[str, JsonValue] = {
    "KiBot": {"status": "partial", "tools": ["manufacture_pcb"], "operations": "nine structured fabrication, documentation and exchange outputs", "remaining": "uploaded configs, hooks and specialized outputs are intentionally not published"},
    "KiKit": {"status": "full", "tools": ["panelize_pcb", "separate_pcb", "create_stencil", "present_pcb"], "operations": "typed panel geometry, rectangular separation, steel/printed stencils and self-contained presentation pages"},
    "PcbDraw": {"status": "full", "tools": ["render_pcb"], "operations": "stylized SVG/PNG and 3D PNG"},
    "KiAuto": {"status": "internal", "tools": ["manufacture_pcb"], "operations": "KiBot internal automation, no separate installed command"},
    "KiDiff": {"status": "internal", "tools": ["manufacture_pcb"], "operations": "KiBot internal diff support, no separate installed command"},
    "KiBoM": {"status": "full", "tools": ["export_bom", "manufacture_pcb"], "operations": "BOM export"},
    "SKiDL": {"status": "full", "tools": ["search_skidl_parts", "convert_netlist_to_skidl"], "operations": "read-only installed-library search and bounded KiCad netlist conversion without executing uploaded Python"},
    "WireViz": {"status": "full", "tools": ["generate_pinout"], "operations": "WireViz connector YAML export"},
    "KiCost": {"status": "partial", "tools": ["generate_offline_cost_spreadsheet"], "operations": "offline no-price BOM spreadsheet", "remaining": "supplier pricing remains disabled because it requires credentials and network access"},
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
      "create_footprint and edit_footprint author native .kicad_mod pads, circular drills and oval slots. "
      "download_project_file returns an exact base64 MCP resource for one regular file under /projects, up to 16 MiB. "
      "get_board_edit_state precedes CAS-guarded edit_board_copper_batch, whose dry run is required before apply. "
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


def _read_project_file(value: str) -> tuple[Path, bytes]:
    """Read one regular project file without following any symlink in its path."""
    path = Path(value)
    if not path.is_absolute():
        raise ToolError("Use an absolute path within /projects")
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ToolError("The path must be inside /projects") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ToolError("The path must name one file directly inside /projects without traversal")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(PROJECT_ROOT, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, file_flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ToolError("Only regular project files can be downloaded")
        if before.st_size > MAX_PROJECT_FILE_BYTES:
            raise ToolError(
                f"Project file is {before.st_size} bytes; the download limit is {MAX_PROJECT_FILE_BYTES} bytes"
            )
        with os.fdopen(file_fd, "rb", closefd=True) as source:
            file_fd = -1
            content = source.read(MAX_PROJECT_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
        if len(content) != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise ToolError("The project file changed while it was being downloaded; retry the request")
        return PROJECT_ROOT / relative, content
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("Project file does not exist, is inaccessible, or its path contains a symlink") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


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
            rules = source.with_suffix(".kicad_dru")
            if rules.is_file():
                shutil.copyfile(rules, board.with_suffix(".kicad_dru"))
            for table in _project_tables(source):
                shutil.copyfile(table, workspace / table.name)
            yield board, destination, workspace
            if hashlib.sha256(source.read_bytes()).digest() != original:
                raise ToolError("The source board changed concurrently; the result was not accepted")
    except BaseException:
        shutil.rmtree(destination)
        raise


@contextmanager
def _artifact_job(input_path: str, output_dir: str, suffix: str) -> Iterator[tuple[Path, Path, Path]]:
    """Stage one non-board input while retaining the output and source guards used by board jobs."""
    source = _path(input_path, suffix)
    destination = _path(output_dir, new=True)
    original = hashlib.sha256(source.read_bytes()).digest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix=".toolkit-", dir=destination) as temporary:
            workspace = Path(temporary)
            staged = workspace / source.name
            shutil.copyfile(source, staged)
            yield staged, destination, workspace
            if hashlib.sha256(source.read_bytes()).digest() != original:
                raise ToolError("The source file changed concurrently; the result was not accepted")
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


@mcp.tool(annotations=_READ)
def download_project_file(path: str) -> EmbeddedResource:
    """Download one regular file under /projects as an exact base64 MCP resource.

    The whole path must be symlink-free. Files larger than 16 MiB are rejected so one
    Streamable HTTP response cannot consume unbounded memory. The resource metadata includes
    the exact byte count and SHA-256 digest for client-side verification.
    """
    source, content = _read_project_file(path)
    relative = source.relative_to(PROJECT_ROOT)
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    metadata: dict[str, JsonValue] = {
        "path": str(source),
        "filename": source.name,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "encoding": "base64",
        "max_bytes": MAX_PROJECT_FILE_BYTES,
    }
    return EmbeddedResource(
        resource=BlobResourceContents(
            uri=f"kicad-project:///{quote(relative.as_posix(), safe='/')}",
            mime_type=mime_type,
            blob=base64.b64encode(content).decode("ascii"),
            _meta=metadata,
        ),
        _meta=metadata,
    )


@mcp.tool(annotations=_READ)
def search_skidl_parts(terms: Annotated[str, Field(min_length=1, max_length=200)],
                       limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict[str, JsonValue]:
    """Search installed KiCad 10 symbol libraries through SKiDL without executing circuit code."""
    output = _execute(["skidl-part-search", "--tool", "kicad10", "--limit", str(limit), terms], PROJECT_ROOT)
    rows: list[JsonValue] = [line for line in output.splitlines() if line.strip()]
    return {"terms": terms, "limit": limit, "count": len(rows), "results": rows}


@mcp.tool(annotations=_EXPORT)
def convert_netlist_to_skidl(netlist_path: str, output_dir: str) -> dict[str, JsonValue]:
    """Convert one saved KiCad .net file into SKiDL source without executing uploaded Python."""
    with _artifact_job(netlist_path, output_dir, ".net") as (netlist, destination, work):
        _execute(["netlist_to_skidl", "--input", str(netlist), "--output", str(destination),
                  "--overwrite", "--nobackup"], work)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def generate_offline_cost_spreadsheet(bom_xml_path: str, output_dir: str,
                                      board_quantity: Annotated[int, Field(ge=1, le=1000000)] = 1,
                                      currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "USD") -> dict[str, JsonValue]:
    """Create a KiCost workbook from a KiCad XML BOM with every supplier API disabled."""
    with _artifact_job(bom_xml_path, output_dir, ".xml") as (bom, destination, work):
        output = destination / "cost.xlsx"
        _execute(["kicost", "--input", str(bom), "--output", str(output), "--overwrite",
                  "--no_price", "--board_qty", str(board_quantity), "--currency", currency,
                  "--quiet"], work, timeout=600)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def manufacture_pcb(pcb_path: str, output_dir: str,
                    outputs: list[Literal["gerber", "drill", "positions", "pdf", "bom",
                                          "netlist_ipc", "ipc2581", "step", "svg"]] | None = None) -> dict[str, JsonValue]:
    """Generate production files with KiBot, without changing the source board/project.

    outputs selects Gerber, drill, placement, PDF, BOM, IPC-D-356, IPC-2581, STEP and SVG;
    the backward-compatible default remains Gerber, drill, placement and PDF.
    Uses fixed local output definitions, no uploaded configuration, hooks or supplier network requests.
    output_dir must not exist. Run native ERC/DRC separately before manufacturing a real board.
    """
    selected = ["gerber", "drill", "positions", "pdf"] if outputs is None else outputs
    supported = {"gerber", "drill", "positions", "pdf", "bom", "netlist_ipc", "ipc2581", "step", "svg"}
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= supported:
        raise ToolError("Select one or more distinct supported output types")
    definitions = {
        "gerber": {"name": "gerbers", "type": "gerber", "dir": "gerber", "layers": LAYERS},
        "drill": {"name": "drills", "type": "excellon", "dir": "drill"},
        "positions": {"name": "assembly_positions", "type": "position", "dir": "assembly", "options": {"format": "CSV", "units": "millimeters"}},
        "pdf": {"name": "layer_pdf", "type": "pdf", "dir": "pdf", "layers": ["F.Cu", "B.Cu", "F.Silkscreen", "Edge.Cuts"]},
        "bom": {"name": "bom", "type": "bom", "dir": "bom", "options": {"format": "CSV", "output": "%f-bom.%x"}},
        "netlist_ipc": {"name": "ipc_d356", "type": "netlist", "dir": "netlist", "options": {"format": "ipc", "output": "%f.%x"}},
        "ipc2581": {"name": "ipc2581", "type": "ipc2581", "dir": "ipc2581", "options": {"compress": True, "output": "%f-ipc2581.%x"}},
        "step": {"name": "step", "type": "step", "dir": "3d", "options": {"output": "%f.%x"}},
        "svg": {"name": "svg", "type": "svg", "dir": "svg", "layers": LAYERS, "options": {"output": "%f-%i.%x"}},
    }
    source = _path(pcb_path, ".kicad_pcb")
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work):
        for path in schematics:
            target = work / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
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
                 cuts: Literal["none", "vcuts", "mousebites"] = "none",
                 horizontal_spacing_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None,
                 vertical_spacing_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None,
                 rotation_deg: Annotated[float, Field(ge=-360, le=360, allow_inf_nan=False)] = 0,
                 alternation: Literal["none", "rows", "cols", "rowsCols"] = "none",
                 tab_style: Literal["none", "fixed", "full"] = "fixed",
                 horizontal_tab_count: Annotated[int, Field(ge=0, le=20)] = 1,
                 vertical_tab_count: Annotated[int, Field(ge=0, le=20)] = 1,
                 framing: Literal["none", "railstb", "railslr", "frame", "tightframe"] = "none",
                 frame_width_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 5,
                 frame_spacing_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 2,
                 mousebite_drill_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.5,
                 mousebite_spacing_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 1) -> dict[str, JsonValue]:
    """Create a structured KiKit grid panel in a NEW output directory.

    Supports independent spacing, rotation/alternation, fixed/full/no tabs, V-cuts/mousebites
    and rails/frames without accepting executable KiKit plugins or uploaded presets.
    rows*columns is bounded to 100 boards. The original PCB is never rewritten.
    Inspect native DRC and fabrication constraints on the resulting panel before release.
    """
    if isinstance(rows, bool) or isinstance(columns, bool) or not 1 <= rows <= 20 or not 1 <= columns <= 20 or rows * columns > 100:
        raise ToolError("Use 1..20 rows/columns and at most 100 board instances")
    hspace = spacing_mm if horizontal_spacing_mm is None else horizontal_spacing_mm
    vspace = spacing_mm if vertical_spacing_mm is None else vertical_spacing_mm
    finite = [spacing_mm, hspace, vspace, tab_width_mm, rotation_deg, frame_width_mm,
              frame_spacing_mm, mousebite_drill_mm, mousebite_spacing_mm]
    if not all(math.isfinite(value) for value in finite) or not 0 <= hspace <= 100 or not 0 <= vspace <= 100 or not 0 < tab_width_mm <= 50:
        raise ToolError("Use finite spacing 0..100mm and tab width >0..50mm")
    if cuts not in {"none", "vcuts", "mousebites"}:
        raise ToolError("Unsupported cut type")
    layout = (f"grid; rows: {rows}; cols: {columns}; hspace: {hspace}mm; vspace: {vspace}mm; "
              f"rotation: {rotation_deg}deg; alternation: {alternation}")
    tabs = tab_style
    if tab_style == "fixed":
        tabs += (f"; hwidth: {tab_width_mm}mm; vwidth: {tab_width_mm}mm; "
                 f"hcount: {horizontal_tab_count}; vcount: {vertical_tab_count}")
    cuts_section = cuts
    if cuts == "mousebites":
        cuts_section += f"; drill: {mousebite_drill_mm}mm; spacing: {mousebite_spacing_mm}mm"
    framing_section = framing
    if framing != "none":
        framing_section += f"; width: {frame_width_mm}mm; space: {frame_spacing_mm}mm"
    with _job(pcb_path, output_dir) as (board, destination, work):
        _execute(["kikit", "panelize", "--layout", layout, "--tabs", tabs,
                  "--cuts", cuts_section, "--framing", framing_section,
                  str(board), str(destination / "panel.kicad_pcb")], work, timeout=600)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def separate_pcb(pcb_path: str, output_dir: str,
                 top_left_x_mm: Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)],
                 top_left_y_mm: Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)],
                 bottom_right_x_mm: Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)],
                 bottom_right_y_mm: Annotated[float, Field(ge=-10000, le=10000, allow_inf_nan=False)]) -> dict[str, JsonValue]:
    """Extract one typed rectangular board region with KiKit into a NEW output directory."""
    if top_left_x_mm >= bottom_right_x_mm or top_left_y_mm >= bottom_right_y_mm:
        raise ToolError("The separation rectangle must have positive width and height")
    source = (f"rectangle; tlx: {top_left_x_mm}mm; tly: {top_left_y_mm}mm; "
              f"brx: {bottom_right_x_mm}mm; bry: {bottom_right_y_mm}mm")
    with _job(pcb_path, output_dir) as (board, destination, work):
        _execute(["kikit", "separate", "--source", source, str(board),
                  str(destination / "separated.kicad_pcb")], work, timeout=600)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def create_stencil(pcb_path: str, output_dir: str,
                   kind: Literal["steel", "printed"] = "steel") -> dict[str, JsonValue]:
    """Create typed KiKit steel or 3D-printable stencil artifacts without uploaded presets."""
    with _job(pcb_path, output_dir) as (board, destination, work):
        command = ["kikit", "stencil", "create" if kind == "steel" else "createprinted"]
        _execute(command + [str(board), str(destination)], work, timeout=900)
    return _result(destination)


@mcp.tool(annotations=_EXPORT)
def present_pcb(pcb_path: str, output_dir: str, name: Annotated[str, Field(min_length=1, max_length=120)],
                description: Annotated[str, Field(max_length=20000)] = "") -> dict[str, JsonValue]:
    """Create a self-contained KiKit board presentation page from typed text and a staged board."""
    with _job(pcb_path, output_dir) as (board, destination, work):
        description_path = work / "description.md"
        description_path.write_text(description)
        _execute(["kikit", "present", "boardpage", "--name", name,
                  "--description", str(description_path), "--board", name, "", str(board),
                  str(destination)], work, timeout=900)
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
CopperLayer = Annotated[str, Field(pattern=r"^(F|B|In([1-9]|[12][0-9]|30))\.Cu$")]


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


class NetClassValues(Parameters):
    track_width: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None
    diff_pair_width: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None
    diff_pair_gap: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None
    diff_pair_via_gap: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] | None = None


class ClearanceMatrixEntry(Parameters):
    first: Annotated[str, Field(min_length=1, max_length=256)]
    second: Annotated[str, Field(min_length=1, max_length=256)]
    clearance_mm: Annotated[float, Field(gt=0, le=999.999999, allow_inf_nan=False)]


class PadSelector(Parameters):
    reference: Annotated[str, Field(min_length=1, max_length=256)]
    pad: Annotated[str, Field(min_length=1, max_length=256)]


class FootprintPad(Parameters):
    number: Annotated[str, Field(max_length=64)]
    kind: Literal["smd", "thru_hole", "np_thru_hole"]
    shape: Literal["circle", "rect", "oval", "roundrect"]
    x_mm: FiniteMM
    y_mm: FiniteMM
    size_x_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    size_y_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    rotation_deg: Angle = 0
    side: Literal["front", "back"] = "front"
    drill_x_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] | None = None
    drill_y_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] | None = None
    roundrect_ratio: Annotated[float, Field(ge=0, le=0.5, allow_inf_nan=False)] = 0.25


class AddFootprintPad(Parameters):
    kind: Literal["add"]
    pad: FootprintPad


class ReplaceFootprintPad(Parameters):
    kind: Literal["replace"]
    number: Annotated[str, Field(max_length=64)]
    occurrence: Annotated[int, Field(ge=1, le=10000)] = 1
    pad: FootprintPad


class DeleteFootprintPad(Parameters):
    kind: Literal["delete"]
    number: Annotated[str, Field(max_length=64)]
    occurrence: Annotated[int, Field(ge=1, le=10000)] = 1


FootprintPadEdit = Annotated[AddFootprintPad | ReplaceFootprintPad | DeleteFootprintPad,
                             Field(discriminator="kind")]


class SnapToGrid(Parameters):
    kind: Literal["snap_to_grid"]
    references: References
    grid_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    origin: Literal["grid", "aux", "zero"] = "grid"


class MoveDrawings(Parameters):
    kind: Literal["move_drawings"]
    drawing_uuids: Annotated[list[str], Field(min_length=1, max_length=10000)]
    layer: Annotated[str, Field(min_length=1, max_length=256)]


class Missing3DReport(Parameters):
    kind: Literal["missing_3d"]


class AnnularCheck(Parameters):
    kind: Literal["annular_check"]
    pad_min_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.125
    via_min_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.125
    drill_extra_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.1


class RfTracks(Parameters):
    track_uuids: Annotated[list[str], Field(min_length=1, max_length=10000)]


class RfTrackLength(RfTracks):
    kind: Literal["track_length"]


class RfRoundTracks(RfTracks):
    kind: Literal["round_tracks"]
    distance_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    segments: Annotated[int, Field(ge=2, le=1000)] = 16


class RfSelectedTracks(RfTracks):
    kind: Literal["reconnect_tracks", "remove_rounding"]


class RfRemoveTapers(Parameters):
    kind: Literal["remove_tapers"]
    confirm_all: Literal[True]


class RfRemoveSolderMask(Parameters):
    kind: Literal["remove_solder_mask"]
    drawing_uuids: Annotated[list[str], Field(min_length=1, max_length=10000)]


class RfTaper(RfTracks):
    kind: Literal["add_taper"]
    pads: Annotated[list[PadSelector], Field(max_length=10000)] = Field(default_factory=list)


class RfClearance(RfTracks):
    kind: Literal["expand_solder_mask", "trace_clearance"]
    clearance_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]


class RfViaFence(RfTracks):
    kind: Literal["via_fence"]
    net_name: Annotated[str, Field(min_length=1, max_length=256)]
    via_diameter_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.6
    drill_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.3
    pitch_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 2
    offset_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 1


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


def _ipc(operation: str, cwd: Path, **parameters: object) -> dict[str, JsonValue]:
    interpreter = IPC_PYTHONS[operation]
    text = _execute(["xvfb-run", "-a", "-e", "/dev/stderr", interpreter, str(IPC_RUNNER)],
                    cwd, timeout=180, request={"operation": operation, **parameters})
    try:
        response = json.loads(next(line for line in reversed(text.splitlines()) if line.strip()))
        if response["ok"] is not True or not isinstance(response["result"], dict):
            raise ValueError("not a successful object result")
        return response["result"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ToolError("Native IPC addon returned an invalid JSON response") from exc


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
        if parameters.pop("_report_dir", False):
            parameters["report_dir"] = str(destination)
        result = _native(operation, work, input_pcb=str(board),
                         output_pcb=str(destination / source.name), **parameters)
        # Retain the hierarchy and project rules beside the new board so native
        # reopening and a subsequent replication do not lose their context.
        for path in schematics:
            relative = path.relative_to(source.parent)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / relative, target)
        for suffix in (".kicad_pro", ".kicad_dru"):
            sidecar = board.with_suffix(suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, destination / sidecar.name)
        for table in _project_tables(source):
            shutil.copyfile(work / table.name, destination / table.name)
    # Do not advertise the removed staging input as an artifact.
    result["input_pcb"] = str(source)
    return {**_result(destination), "operation": result}


def _ipc_board(operation: str, pcb_path: str, output_dir: str, **parameters: object) -> dict[str, JsonValue]:
    source = _path(pcb_path, ".kicad_pcb")
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work):
        for path in schematics:
            relative = path.relative_to(source.parent)
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        result = _ipc(operation, work, input_pcb=str(board), session_dir=str(work), **parameters)
        shutil.copyfile(board, destination / source.name)
        for path in schematics:
            relative = path.relative_to(source.parent)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(work / relative, target)
        for suffix in (".kicad_pro", ".kicad_dru"):
            sidecar = board.with_suffix(suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, destination / sidecar.name)
        for table in _project_tables(source):
            shutil.copyfile(work / table.name, destination / table.name)
    return {**_result(destination), "operation": result}


def _project_snapshot(root: Path) -> bytes:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ToolError(f"Project copy does not accept symlinks: {path}")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.digest()


def _project_copy(project_dir: str, output_dir: str) -> tuple[Path, Path, bytes]:
    source = _path(project_dir, directory=True)
    destination = _path(output_dir, new=True)
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ToolError("Source project and output directory must not contain each other")
    before = _project_snapshot(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, copy_function=shutil.copyfile)
    return source, destination, before


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
def stitch_vias(
    pcb_path: str, output_dir: str, net_name: Annotated[str, Field(min_length=1, max_length=256)],
    start_layer: CopperLayer = "F.Cu", end_layer: CopperLayer = "B.Cu",
    action: Literal["plan", "stitch"] = "stitch",
    via_type: Literal["through", "blind_buried", "micro"] = "through",
    via_diameter_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.6,
    drill_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.3,
    spacing_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 2,
    pattern: Literal["Hexagonal", "Square", "Staggered"] = "Square",
    x_offset_mm: FiniteMM = 0, y_offset_mm: FiniteMM = 0,
    avoid_other_zones: bool = False, avoid_footprints: bool = False,
    avoid_same_net_pads: bool = False,
) -> dict[str, JsonValue]:
    """Plan or place the installed Via Stitching addon's zone-overlap grid on a NEW board.

    The operation uses a disposable native KiCad IPC editor, saves the staged board and never
    attaches to the user's active GUI. The original board/project remain byte-for-byte unchanged.
    """
    if drill_mm >= via_diameter_mm:
        raise ToolError("drill_mm must be smaller than via_diameter_mm")
    return _ipc_board("via_stitching", pcb_path, output_dir, net_name=net_name,
                      layers=[start_layer, end_layer], action=action, via_type=via_type,
                      via_diameter_mm=via_diameter_mm, drill_mm=drill_mm,
                      spacing_mm=spacing_mm, pattern=pattern, x_offset_mm=x_offset_mm,
                      y_offset_mm=y_offset_mm, avoid_other_zones=avoid_other_zones,
                      avoid_footprints=avoid_footprints,
                      avoid_same_net_pads=avoid_same_net_pads)


@mcp.tool(annotations=_EXPORT)
def edit_silk_references(
    pcb_path: str, output_dir: str,
    scope: Literal["all", "ref", "value", "lib"] = "all",
    scope_pattern: Annotated[str, Field(max_length=256)] = "",
    do_center: bool = True, process_front: bool = True, process_back: bool = True,
    placement: Literal["center", "auto", "top", "bottom", "left", "right"] = "center",
    fit_text: bool = False,
    min_text_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 0.5,
    nudge_pads: bool = True,
    rotation_mode: Literal["zero", "footprint", "keep"] = "zero",
    do_visibility: bool = False, visible: bool = True,
    do_resize: bool = False, resize_mode: Literal["delta", "set"] = "delta",
    resize_delta_mm: Annotated[float, Field(ge=-100, le=100, allow_inf_nan=False)] = 0,
    set_height_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1,
) -> dict[str, JsonValue]:
    """Run Silk Reference Tools placement, visibility and sizing on a NEW staged board."""
    if scope != "all" and not scope_pattern:
        raise ToolError("scope_pattern is required for ref, value and lib scopes")
    if not (do_center or do_visibility or do_resize):
        raise ToolError("Enable at least one silk-reference operation")
    return _ipc_board("silk_reference", pcb_path, output_dir,
                      scope=scope, scope_pattern=scope_pattern, do_center=do_center,
                      process_front=process_front, process_back=process_back,
                      placement=placement, fit_text=fit_text, min_text_mm=min_text_mm,
                      nudge_pads=nudge_pads, rotation_mode=rotation_mode, export_csv=False,
                      do_visibility=do_visibility, visible=visible, do_resize=do_resize,
                      resize_mode=resize_mode, resize_delta_mm=resize_delta_mm,
                      set_height_mm=set_height_mm)


@mcp.tool(annotations=_EXPORT)
def fanout_footprint(
    pcb_path: str, output_dir: str,
    reference: Annotated[str, Field(min_length=1, max_length=256)],
    package: Literal["BGA", "SOIC/QFN", "Connector/FPC"] = "BGA",
    alignment: Literal["Quadrant", "Diagonal", "X-pattern", "Staggered", "Linear Escape",
                       "Fan Escape", "Staggered Linear", "Staggered Fan",
                       "Alternating Sides", "Staggered One Side"] = "Quadrant",
    direction: Literal["", "TopLeft", "TopRight", "BottomLeft", "BottomRight",
                       "Counterclock", "Counterclockwise", "Horizontal", "Vertical",
                       "Outside", "Inside", "Both sides", "Left/Right", "Top/Bottom",
                       "Left", "Right", "Top", "Bottom"] = "",
    in_pad: bool = False, connected_only: bool = True,
    via_type: Literal["Through", "Micro", "Blind/Buried"] = "Through",
    via_diameter_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.6,
    drill_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.3,
    start_layer: CopperLayer = "F.Cu", end_layer: CopperLayer = "B.Cu",
    track_layer: CopperLayer = "F.Cu",
    track_width_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.2,
    fanout_length_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 1,
    stagger_gap_mm: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 1,
    via_pitch_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 0.5,
) -> dict[str, JsonValue]:
    """Run the installed Fanout Tool for one exact footprint on a NEW staged board."""
    valid = {
        "BGA": {"Quadrant", "Diagonal", "X-pattern", "Staggered"},
        "SOIC/QFN": {"Linear Escape", "Fan Escape", "Staggered Linear", "Staggered Fan"},
        "Connector/FPC": {"Alternating Sides", "Staggered One Side"},
    }
    if alignment not in valid[package]:
        raise ToolError(f"{alignment} is not an alignment for {package}")
    if drill_mm >= via_diameter_mm:
        raise ToolError("drill_mm must be smaller than via_diameter_mm")
    return _ipc_board("fanout", pcb_path, output_dir, reference=reference, package=package,
                      alignment=alignment, direction=direction, in_pad=in_pad,
                      connected_only=connected_only, via_type=via_type,
                      via_diameter_mm=via_diameter_mm, drill_mm=drill_mm,
                      start_layer=start_layer, end_layer=end_layer, track_layer=track_layer,
                      track_width_mm=track_width_mm, fanout_length_mm=fanout_length_mm,
                      stagger_gap_mm=stagger_gap_mm, via_pitch_mm=via_pitch_mm)


@mcp.tool(annotations=_EXPORT)
def apply_impedance_netclasses(
    pcb_path: str, output_dir: str, proposals: dict[str, NetClassValues],
    action: Literal["preview", "apply"] = "apply",
) -> dict[str, JsonValue]:
    """Preview or apply Z0 Field Solver netclass widths/gaps on a NEW project copy.

    The installed addon's KiCad-version gate, whole-class merge and exact read-back are retained.
    Values are millimetres; only explicitly supplied fields are changed.
    """
    values = {name: item.model_dump(exclude_none=True) for name, item in proposals.items()}
    if not values or any(not name or not fields for name, fields in values.items()):
        raise ToolError("Each named netclass must contain at least one proposed value")
    return _ipc_board("z0_netclasses", pcb_path, output_dir, proposals=values, action=action)


@mcp.tool(annotations=_EXPORT)
def update_clearance_matrix(
    pcb_path: str, output_dir: str,
    clearances: Annotated[list[ClearanceMatrixEntry], Field(min_length=1, max_length=10000)],
    action: Literal["preview", "apply"] = "apply",
) -> dict[str, JsonValue]:
    """Preview or write the installed NCCM addon's bounded rules section on a NEW project copy."""
    pairs = [(row.first, row.second) for row in clearances]
    if len(pairs) != len(set(pairs)):
        raise ToolError("Each ordered netclass pair may appear only once")
    return _ipc_board("nccm", pcb_path, output_dir,
                      clearances=[row.model_dump() for row in clearances], action=action)


KobeeLayer = Literal["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask"]


class KobeeFeatureBase(Parameters):
    x_mm: FiniteMM
    y_mm: FiniteMM
    layer: KobeeLayer = "F.SilkS"
    layers: list[KobeeLayer] = Field(default_factory=lambda: ["F.SilkS"], min_length=1, max_length=6)


class KobeeMachineCode(KobeeFeatureBase):
    kind: Literal["machine_code"]
    code_type: Literal["qr", "code128"] = "qr"
    payload: Annotated[str, Field(min_length=1, max_length=512)]
    module_size_mm: Annotated[float, Field(ge=0.2, le=10, allow_inf_nan=False)] = 0.35
    bar_height_mm: Annotated[float, Field(ge=3, le=100, allow_inf_nan=False)] = 4
    presentation: Literal["plain", "rounded_frame", "rounded_caption"] = "plain"
    caption: Annotated[str, Field(max_length=128)] = "SCAN ME"


class KobeePinHeader(KobeeFeatureBase):
    kind: Literal["pin_header"]
    pin_labels: Annotated[list[str], Field(min_length=1, max_length=128)]
    pitch_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 2.54
    orientation: Literal["horizontal", "vertical"] = "horizontal"
    pin1_end: Literal["start", "end"] = "start"
    label_side: Literal["above", "below", "left", "right"] = "above"
    opening_mode: Literal["none", "continuous", "individual"] = "none"
    font: Annotated[str, Field(min_length=1, max_length=256)] = "UbuntuMono-B"
    height_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 1.2


class KobeeComponentCallout(KobeeFeatureBase):
    kind: Literal["component_callout"]
    title: Annotated[str, Field(min_length=1, max_length=512)]
    subtitle: Annotated[str, Field(max_length=512)] = ""
    preset_id: Annotated[str, Field(min_length=1, max_length=64)] = "custom"
    component_width_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 2.2
    component_height_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 1.1
    component_clearance_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.3
    component_position: Literal["left", "right", "above", "below"] = "left"
    font: Annotated[str, Field(min_length=1, max_length=256)] = "UbuntuMono-B"
    height_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.2


KobeeFeature = Annotated[KobeeMachineCode | KobeePinHeader | KobeeComponentCallout,
                         Field(discriminator="kind")]


@mcp.tool(annotations=_EXPORT)
def place_kobee_artwork(pcb_path: str, output_dir: str, feature: KobeeFeature) -> dict[str, JsonValue]:
    """Place a typed Kobee Studio QR/barcode, pin-header rail or component callout on a staged board."""
    values = feature.model_dump()
    if feature.layer not in feature.layers:
        raise ToolError("feature.layer must be included in feature.layers")
    if isinstance(feature, KobeeMachineCode):
        if feature.code_type == "qr" and feature.module_size_mm < 0.35:
            raise ToolError("QR module_size_mm must be at least 0.35")
        if feature.code_type == "code128" and feature.presentation != "plain":
            raise ToolError("Code 128 supports presentation=plain only")
    if isinstance(feature, KobeePinHeader):
        expected_sides = {"horizontal": {"above", "below"}, "vertical": {"left", "right"}}
        if feature.label_side not in expected_sides[feature.orientation]:
            raise ToolError("label_side is incompatible with the selected header orientation")
    values["layers"] = list(dict.fromkeys(feature.layers))
    return _ipc_board("kobee_feature", pcb_path, output_dir, **values)


@mcp.tool(annotations=_EXPORT)
def place_kobee_label(
    pcb_path: str, output_dir: str,
    text: Annotated[str, Field(min_length=1, max_length=512)],
    x_mm: FiniteMM, y_mm: FiniteMM,
    layer: Literal["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask"] = "F.SilkS",
    layers: Annotated[list[Literal["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask"]],
                      Field(min_length=1, max_length=6)] = ["F.SilkS"],
    height_mm: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.2,
    width_mm: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0,
    font: Annotated[str, Field(min_length=1, max_length=256)] = "UbuntuMono-B",
    line_spacing: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)] = 1.5,
    alignment: Literal["left", "center", "right"] = "center",
    shape: Literal["rectangle", "circle", "rounded_rectangle", "pill", "custom_ends",
                   "custom_long_edges", "pointer", "flag", "tab", "chamfer", "hexagon"] | None = None,
    padding_x_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 1.2,
    padding_y_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0.5,
    minimum_width_mm: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0,
    border_thickness_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0,
    corner_radius_mm: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] = 0,
    filled: bool = True, inverted: bool = False,
    direction: Literal["left", "right"] = "right",
    start_cap: Literal["square", "rounded", "chamfered", "point", "notch", "skew_forward", "skew_back"] = "square",
    end_cap: Literal["square", "rounded", "chamfered", "point", "notch", "skew_forward", "skew_back"] = "rounded",
    subtitle: Annotated[str, Field(max_length=512)] = "",
    underline: bool = False, inline_format: bool = False,
) -> dict[str, JsonValue]:
    """Compose and place a native Kobee Studio label footprint on a NEW staged board."""
    if layer not in layers:
        raise ToolError("layer must be included in layers")
    if not filled and border_thickness_mm <= 0:
        raise ToolError("Outlined labels require a positive border_thickness_mm")
    return _ipc_board("kobee_label", pcb_path, output_dir, text=text, x_mm=x_mm, y_mm=y_mm,
                      layer=layer, layers=list(dict.fromkeys(layers)), height_mm=height_mm,
                      width_mm=width_mm, font=font, line_spacing=line_spacing,
                      alignment=alignment, shape=shape, padding_x_mm=padding_x_mm,
                      padding_y_mm=padding_y_mm, minimum_width_mm=minimum_width_mm,
                      border_thickness_mm=border_thickness_mm,
                      corner_radius_mm=corner_radius_mm, filled=filled, inverted=inverted,
                      direction=direction, start_cap=start_cap, end_cap=end_cap,
                      subtitle=subtitle, underline=underline, inline_format=inline_format)


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


@mcp.tool(annotations=_EXPORT)
def create_footprint(
    output_dir: str,
    name: Annotated[str, Field(min_length=1, max_length=128,
                               pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,127}$")],
    pads: Annotated[list[FootprintPad], Field(min_length=1, max_length=10000)],
    description: Annotated[str, Field(max_length=10000)] = "",
    keywords: Annotated[str, Field(max_length=10000)] = "",
    allow_missing_courtyard: bool = False,
    allow_solder_mask_bridges: bool = False,
) -> dict[str, JsonValue]:
    """Create a NEW native KiCad .kicad_mod footprint with SMD, PTH and NPTH pads.

    Through-hole drill_x_mm is required; drill_y_mm defaults to drill_x_mm. Unequal drill
    dimensions create an oval slot. The result is reopened by KiCad and returned with pad UUIDs.
    """
    destination = _path(output_dir, new=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        result = _native("footprint_create", destination,
                         output_footprint=str(destination / f"{name}.kicad_mod"), name=name,
                         pads=[pad.model_dump() for pad in pads], description=description,
                         keywords=keywords, allow_missing_courtyard=allow_missing_courtyard,
                         allow_solder_mask_bridges=allow_solder_mask_bridges)
        return {**_result(destination), "operation": result}
    except BaseException:
        shutil.rmtree(destination)
        raise


@mcp.tool(annotations=_EXPORT)
def edit_footprint(
    footprint_path: str, output_dir: str,
    operations: Annotated[list[FootprintPadEdit], Field(min_length=1, max_length=10000)],
    name: Annotated[str, Field(min_length=1, max_length=128,
                               pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+\-]{0,127}$")] | None = None,
    description: Annotated[str, Field(max_length=10000)] | None = None,
    keywords: Annotated[str, Field(max_length=10000)] | None = None,
    allow_missing_courtyard: bool | None = None,
    allow_solder_mask_bridges: bool | None = None,
) -> dict[str, JsonValue]:
    """Edit pads in an existing .kicad_mod and save a NEW footprint; source stays unchanged.

    Operations add, replace or delete one pad. Replace/delete select by pad number plus a
    one-based occurrence for footprints that deliberately repeat numbers. Native KiCad loading
    and saving preserves graphics, text, models and unsupported pad fields not being replaced.
    """
    source = _path(footprint_path, ".kicad_mod")
    destination = _path(output_dir, new=True)
    output_name = name or source.stem
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        result = _native("footprint_edit", destination, input_footprint=str(source),
                         output_footprint=str(destination / f"{output_name}.kicad_mod"),
                         name=output_name, operations=[operation.model_dump() for operation in operations],
                         description=description, keywords=keywords,
                         allow_missing_courtyard=allow_missing_courtyard,
                         allow_solder_mask_bridges=allow_solder_mask_bridges)
        return {**_result(destination), "operation": result}
    except BaseException:
        shutil.rmtree(destination)
        raise


@mcp.tool(annotations=_EXPORT)
def insert_kibuzzard_footprint(
    pcb_path: str, output_dir: str,
    reference: Annotated[str, Field(min_length=1, max_length=64)],
    x_mm: FiniteMM, y_mm: FiniteMM,
    text: Annotated[str, Field(min_length=1, max_length=128)],
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
    """Generate with the installed KiBuzzard engine and insert the footprint into a NEW board."""
    return _native_board("kibuzzard_insert", pcb_path, output_dir, reference=reference,
                         x_mm=x_mm, y_mm=y_mm, text=text, height_mm=height_mm,
                         width_mm=width_mm, font=font, layer=layer, alignment=alignment,
                         left_cap=left_cap, right_cap=right_cap, padding_mm=padding_mm,
                         line_spacing=line_spacing, inline_format=inline_format)


@mcp.tool(annotations=_EXPORT)
def run_pcb_action_tool(
    pcb_path: str, output_dir: str,
    operation: Annotated[SnapToGrid | MoveDrawings | Missing3DReport | AnnularCheck,
                         Field(discriminator="kind")],
) -> dict[str, JsonValue]:
    """Run a typed pcb-action-tools operation on a NEW staged board.

    Supports snap-to-grid, moving exact drawing UUIDs to a board layer, the installed
    missing-3D report, and its annular-ring checker. The source board is preserved.
    """
    parameters = operation.model_dump()
    action = parameters.pop("kind")
    return _native_board("pcb_action_tools", pcb_path, output_dir,
                         _report_dir=True, action=action, **parameters)


@mcp.tool(annotations=_EXPORT)
def apply_via_fence(
    pcb_path: str, output_dir: str,
    track_uuids: Annotated[list[str], Field(min_length=1, max_length=10000)],
    net_name: Annotated[str, Field(min_length=1, max_length=256)],
    pad_references: Annotated[list[str], Field(max_length=10000)] | None = None,
    spacing_mm: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)] = 1,
    gap_mm: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)] = 0.25,
    via_diameter_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.6,
    drill_mm: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.3,
    staggered: bool = False,
) -> dict[str, JsonValue]:
    """Run the installed ViaFence placement engine for exact track/arc UUIDs and footprints.

    Selection is recreated only on a disposable board copy; the active editor and source
    project are never attached or changed. A NEW board is saved after native placement.
    """
    if drill_mm >= via_diameter_mm:
        raise ToolError("drill_mm must be smaller than via_diameter_mm")
    return _native_board("viafence_apply", pcb_path, output_dir,
                         track_uuids=track_uuids, pad_references=pad_references or [],
                         net_name=net_name, spacing_mm=spacing_mm, gap_mm=gap_mm,
                         via_diameter_mm=via_diameter_mm, drill_mm=drill_mm,
                         staggered=staggered)


@mcp.tool(annotations=_EXPORT)
def run_rf_tool(
    pcb_path: str, output_dir: str,
    operation: Annotated[RfTrackLength | RfRoundTracks | RfSelectedTracks | RfRemoveTapers | RfRemoveSolderMask |
                         RfTaper | RfClearance | RfViaFence, Field(discriminator="kind")],
) -> dict[str, JsonValue]:
    """Run a typed installed RF-Tools engine operation on a NEW staged board.

    Exact track UUIDs replace GUI selection. Supports length, rounding/reconnect/removal,
    taper add/remove, solder-mask expansion/removal, trace clearance and RF via fencing.
    """
    parameters = operation.model_dump()
    action = parameters.pop("kind")
    if action in {"round_tracks", "reconnect_tracks"} and len(parameters["track_uuids"]) != 2:
        raise ToolError(f"{action} requires exactly two track UUIDs")
    if action == "via_fence" and parameters["drill_mm"] >= parameters["via_diameter_mm"]:
        raise ToolError("drill_mm must be smaller than via_diameter_mm")
    return _native_board("rf_tools_apply", pcb_path, output_dir, action=action, **parameters)


@mcp.tool(annotations=_EXPORT)
def manage_project_notes(
    project_dir: str, output_dir: str,
    action: Literal["read", "save_notes", "save_todos", "save_version_log", "export_changelog"] = "read",
    notes: Annotated[str, Field(max_length=1_000_000)] | None = None,
    todos: Annotated[list[dict[str, JsonValue]], Field(max_length=10000)] | None = None,
    version_log: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Run KiNotes atomic storage operations on a NEW complete project copy.

    The source tree is symlink-free, fingerprinted before/after and never modified. Only the
    selected typed payload is accepted; no Markdown command, hook or external program is run.
    """
    payloads = {"save_notes": notes, "save_todos": todos, "save_version_log": version_log}
    if action in payloads and payloads[action] is None:
        raise ToolError(f"{action.removeprefix('save_')} is required for {action}")
    source, destination, before = _project_copy(project_dir, output_dir)
    try:
        result = _native("kinotes_manage", destination, project_dir=str(destination),
                         action=action, notes=notes, todos=todos, version_log=version_log)
        (destination / "kinotes-result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        if _project_snapshot(source) != before:
            raise ToolError("The source project changed concurrently; the result was not accepted")
        result["project_dir"] = str(source)
        return {**_result(destination), "operation": result}
    except BaseException:
        shutil.rmtree(destination)
        raise


# Reuse the pinned upstream composition function, preserving every original tool.
for _module in [
    upstream.schematic,
    upstream.pcb,
    upstream.symbol,
    upstream.footprint,
    upstream.project,
    board_batch,
]:
    upstream._copy_tools(_module.mcp, mcp)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
