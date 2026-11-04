"""Typed local-toolkit operations registered beside the pinned upstream MCP tools."""
import base64
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import importlib.metadata
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from typing import Annotated, BinaryIO, Iterator, Literal
from urllib.parse import quote
import uuid
import zipfile

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import BlobResourceContents, EmbeddedResource, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model, model_validator

from toolkit_native import REPLICATION_DEFAULTS

import mcp_board_batch as board_batch
from mcp_server_kicad import _cst, server as upstream

PROJECT_ROOT = Path("/projects")
MAX_PROJECT_FILE_BYTES = 16 * 1024 * 1024
MAX_PROJECT_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PROJECT_ARCHIVE_FILES = 10_000
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
_PROJECT_MUTATION_LOCK = threading.RLock()
_PROJECT_LOCK_NAME = ".kicad-mcp.lock"
_PROJECT_LOCK_STATE = threading.local()


@contextmanager
def _project_lock(*, exclusive: bool = True) -> Iterator[None]:
    """Serialize cooperating writers across the GUI and HTTP containers sharing /projects."""
    with _PROJECT_MUTATION_LOCK:  # pairs Python thread safety with the shared filesystem lock
        depth = getattr(_PROJECT_LOCK_STATE, "depth", 0)
        if depth:
            if exclusive and not getattr(_PROJECT_LOCK_STATE, "exclusive", False):
                raise ToolError("Cannot upgrade a nested shared project lock")
            _PROJECT_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _PROJECT_LOCK_STATE.depth -= 1
            return
        lock_path = PROJECT_ROOT / _PROJECT_LOCK_NAME
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            _PROJECT_LOCK_STATE.depth = 1
            _PROJECT_LOCK_STATE.exclusive = exclusive
            yield
        finally:
            _PROJECT_LOCK_STATE.depth = 0
            _PROJECT_LOCK_STATE.exclusive = False
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

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
    path, _ = _project_relative(value)
    current = _symlink_free(path, allow_missing_final=new)
    if new:
        if current is not None:
            raise ToolError(f"Output already exists: {path}; choose a new output directory")
        parent = _symlink_free(path.parent)
        if parent is None or not stat.S_ISDIR(parent.st_mode):
            raise ToolError(f"Output parent is not an existing project directory: {path.parent}")
    elif current is None or not (stat.S_ISDIR(current.st_mode) if directory else stat.S_ISREG(current.st_mode)):
        raise ToolError(f"Input {'directory' if directory else 'file'} does not exist: {path}")
    if suffix and path.suffix != suffix:
        raise ToolError(f"Expected a {suffix} file: {path}")
    return path


@contextmanager
def _open_project_file(value: str) -> Iterator[tuple[Path, BinaryIO, os.stat_result]]:
    """Open one regular /projects file descriptor-relative without following symlinks."""
    path, relative = _project_relative(value)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = -1
    file_fd = -1
    stream = None
    try:
        directory_fd = os.open(PROJECT_ROOT, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, file_flags, dir_fd=directory_fd)
        entry_stat = os.fstat(file_fd)
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ToolError("Only regular project files can be read")
        stream = os.fdopen(file_fd, "rb", closefd=True)
        file_fd = -1
        yield path, stream, entry_stat
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("Project file does not exist, is inaccessible, or its path contains a symlink") from exc
    finally:
        if stream is not None:
            stream.close()
        if file_fd >= 0:
            os.close(file_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


@contextmanager
def _open_project_directory(value: str) -> Iterator[tuple[Path, int]]:
    """Open one project directory by descriptor without following any path symlink."""
    path, relative = _project_relative(value, allow_root=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = -1
    try:
        directory_fd = os.open(PROJECT_ROOT, flags)
        for part in relative.parts:
            next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        yield path, directory_fd
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("Project directory does not exist, is inaccessible, or its path contains a symlink") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


@contextmanager
def _open_project_parent(value: str) -> Iterator[tuple[Path, int, str]]:
    """Pin a project path's parent and yield its display path, parent descriptor and leaf."""
    path, relative = _project_relative(value)
    parent = PROJECT_ROOT / relative.parent
    with _open_project_directory(str(parent)) as (_, parent_fd):
        yield path, parent_fd, relative.name


@contextmanager
def _open_relative_directory(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
    """Walk beneath a pinned directory, optionally creating directories, without symlink traversal."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=directory_fd)
                next_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        yield directory_fd
    except OSError as exc:
        raise ToolError("Project directory path is missing, inaccessible, or contains a symlink") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _stat_at(parent_fd: int, name: str, *, allow_missing: bool = False) -> os.stat_result | None:
    try:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ToolError("Project path does not exist") from None
    if stat.S_ISLNK(entry.st_mode):
        raise ToolError("Project paths must not contain symlinks")
    return entry


def _file_sha256_at(parent_fd: int, name: str) -> str:
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode):
            raise ToolError("Expected a regular project file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError("Project file does not exist, is inaccessible, or is a symlink") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_file_sha256_at(parent_fd: int, name: str, expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ToolError("expected_sha256 must be one lowercase 64-character SHA-256 digest")
    actual = _file_sha256_at(parent_fd, name)
    if actual != expected_sha256:
        raise ToolError(f"Project path changed: expected SHA-256 {expected_sha256}, found {actual}")
    return actual


def _fingerprint_at(parent_fd: int, name: str) -> str:
    entry = _stat_at(parent_fd, name)
    if entry is not None and stat.S_ISREG(entry.st_mode):
        return _file_sha256_at(parent_fd, name)
    if entry is not None and stat.S_ISDIR(entry.st_mode):
        return _tree_fingerprint(Path(f"/proc/self/fd/{parent_fd}") / name)
    raise ToolError("Only regular files and directories are supported")


def _require_fingerprint_at(parent_fd: int, name: str, expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ToolError("expected_sha256 must be one lowercase 64-character SHA-256 digest")
    actual = _fingerprint_at(parent_fd, name)
    if actual != expected_sha256:
        raise ToolError(f"Project path changed: expected SHA-256 {expected_sha256}, found {actual}")
    return actual


def _create_temp_at(parent_fd: int, target_name: str, label: str, *, suffix: str = "") -> tuple[str, int]:
    temporary = f".{target_name}.{label}-{uuid.uuid4().hex}{suffix}"
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        return temporary, descriptor
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _write_temp_at(
    parent_fd: int, target_name: str, content: bytes, label: str, *, suffix: str = "",
) -> str:
    temporary, descriptor = _create_temp_at(parent_fd, target_name, label, suffix=suffix)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _publish_temp_at(
    parent_fd: int, temporary: str, target_name: str, expected_sha256: str | None,
) -> None:
    current = _stat_at(parent_fd, target_name, allow_missing=True)
    if current is None:
        if expected_sha256 is not None:
            raise ToolError("expected_sha256 was supplied but the destination does not exist")
        os.link(temporary, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent_fd)
        return
    if not stat.S_ISREG(current.st_mode):
        raise ToolError("Only a regular file can be replaced")
    if expected_sha256 is None:
        raise ToolError("expected_sha256 is required to replace an existing file")
    _require_file_sha256_at(parent_fd, target_name, expected_sha256)
    os.replace(temporary, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)


def _rename_noreplace_at(source_parent_fd: int, source_name: str, destination_parent_fd: int, destination_name: str) -> None:
    """Atomically publish a new path without replacing a concurrent destination."""
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None:
        raise ToolError("This Linux runtime does not provide renameat2")
    result = renameat2(
        ctypes.c_int(source_parent_fd), ctypes.c_char_p(os.fsencode(source_name)),
        ctypes.c_int(destination_parent_fd), ctypes.c_char_p(os.fsencode(destination_name)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise ToolError(f"Output already exists: {destination_name}; choose a new output path")
    raise ToolError(f"Could not publish output atomically: {os.strerror(error)}")


def _publish_staged_file_at(source: Path, parent_fd: int, target_name: str) -> None:
    source_stat = os.lstat(source)
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size == 0:
        raise ToolError("Backend did not produce one non-empty regular output file")
    source_digest = _file_sha256(source)
    temporary, descriptor = _create_temp_at(parent_fd, target_name, "publish", suffix=source.suffix)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb", closefd=True) as output_stream:
            descriptor = -1
            shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after = os.lstat(source)
        if (after.st_size != source_stat.st_size or after.st_mtime_ns != source_stat.st_mtime_ns
                or _file_sha256(source) != source_digest):
            raise ToolError("Backend output changed while it was being published")
        _publish_temp_at(parent_fd, temporary, target_name, None)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _publish_staged_directory_at(source: Path, parent_fd: int, target_name: str) -> None:
    source_stat = os.lstat(source)
    if not stat.S_ISDIR(source_stat.st_mode):
        raise ToolError("Backend did not produce an output directory")
    source_fingerprint = _tree_fingerprint(source)
    if not any(entry.is_file() for entry in source.rglob("*")):
        raise ToolError("Backend produced no output files")
    temporary = f".{target_name}.publish-{uuid.uuid4().hex}"
    stable_temporary = Path(f"/proc/self/fd/{parent_fd}") / temporary
    try:
        shutil.copytree(source, stable_temporary, symlinks=False)
        if _tree_fingerprint(stable_temporary) != source_fingerprint or _tree_fingerprint(source) != source_fingerprint:
            raise ToolError("Backend output changed while it was being published")
        _rename_noreplace_at(parent_fd, temporary, parent_fd, target_name)
        os.fsync(parent_fd)
    finally:
        shutil.rmtree(stable_temporary, ignore_errors=True)


def _read_project_file_unlocked(value: str) -> tuple[Path, bytes]:
    """Read one bounded regular project file from a symlink-safe descriptor."""
    with _open_project_file(value) as (path, source, before):
        if before.st_size > MAX_PROJECT_FILE_BYTES:
            raise ToolError(
                f"Project file is {before.st_size} bytes; the download limit is {MAX_PROJECT_FILE_BYTES} bytes"
            )
        content = source.read(MAX_PROJECT_FILE_BYTES + 1)
        after = os.fstat(source.fileno())
        if len(content) != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise ToolError("The project file changed while it was being downloaded; retry the request")
        return path, content


def _read_project_file(value: str) -> tuple[Path, bytes]:
    with _project_lock(exclusive=False):
        return _read_project_file_unlocked(value)


def _project_relative(value: str, *, allow_root: bool = False) -> tuple[Path, Path]:
    path = Path(value)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise ToolError("Use an absolute path within /projects without traversal")
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ToolError("The path must be inside /projects") from exc
    if not allow_root and not relative.parts:
        raise ToolError("The path must be inside /projects, not its root")
    if relative.parts and relative.parts[0] == _PROJECT_LOCK_NAME:
        raise ToolError("The project lock is reserved and cannot be accessed through MCP")
    return path, relative


def _symlink_free(path: Path, *, allow_missing_final: bool = False) -> os.stat_result | None:
    _, relative = _project_relative(str(path), allow_root=True)
    current = PROJECT_ROOT
    if not current.is_dir() or current.is_symlink():
        raise ToolError("The configured /projects root is unavailable or is a symlink")
    if not relative.parts:
        return os.lstat(current)
    current_stat: os.stat_result | None = None
    for index, part in enumerate(relative.parts):
        current /= part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_final and index == len(relative.parts) - 1:
                return None
            raise ToolError(f"Project path does not exist: {path}") from None
        if stat.S_ISLNK(current_stat.st_mode):
            raise ToolError(f"Project paths must not contain symlinks: {current}")
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise ToolError(f"Project path parent is not a directory: {current}")
    return current_stat


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for entry in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        if entry.relative_to(path).parts == (_PROJECT_LOCK_NAME,):
            continue
        entry_stat = os.lstat(entry)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ToolError(f"Project trees must not contain symlinks: {entry}")
        relative = entry.relative_to(path).as_posix().encode()
        if stat.S_ISDIR(entry_stat.st_mode):
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISREG(entry_stat.st_mode):
            digest.update(b"F\0" + relative + b"\0" + str(entry_stat.st_size).encode() + b"\0")
            digest.update(bytes.fromhex(_file_sha256(entry)))
        else:
            raise ToolError(f"Project trees must contain only regular files and directories: {entry}")
    return digest.hexdigest()


def _regular_tree_manifest(path: Path) -> dict[str, tuple[str, int, str]]:
    """Describe a regular-file tree so a staged backend can only add new artifacts."""
    manifest: dict[str, tuple[str, int, str]] = {}
    for entry in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = entry.relative_to(path).as_posix()
        if relative == _PROJECT_LOCK_NAME:
            continue
        entry_stat = os.lstat(entry)
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ToolError(f"Project trees must not contain symlinks: {entry}")
        if stat.S_ISDIR(entry_stat.st_mode):
            manifest[relative] = ("directory", 0, "")
        elif stat.S_ISREG(entry_stat.st_mode):
            manifest[relative] = ("file", entry_stat.st_size, _file_sha256(entry))
        else:
            raise ToolError(f"Project trees must contain only regular files and directories: {entry}")
    return manifest


def _publish_jobset_outputs_at(
    staged_project: Path,
    project_fd: int,
    before: dict[str, tuple[str, int, str]],
    after: dict[str, tuple[str, int, str]],
) -> list[str]:
    """Publish only new jobset files, without replacing any live project entry."""
    changed = sorted(relative for relative, entry in before.items() if after.get(relative) != entry)
    if changed:
        raise ToolError(
            "Jobset changed or removed existing project content in staging; nothing was published: "
            + ", ".join(changed[:10])
        )
    added = {relative: entry for relative, entry in after.items() if relative not in before}
    files = sorted(
        (relative for relative, entry in added.items() if entry[0] == "file"),
        key=lambda relative: (relative.count("/"), relative),
    )
    directories = sorted(
        (relative for relative, entry in added.items() if entry[0] == "directory"),
        key=lambda relative: (relative.count("/"), relative),
    )
    if not files:
        raise ToolError("Jobset produced no new regular output files")
    if len(files) > MAX_PROJECT_ARCHIVE_FILES:
        raise ToolError(f"Jobset produced more than {MAX_PROJECT_ARCHIVE_FILES} files")
    total_size = sum(added[relative][1] for relative in files)
    if total_size > MAX_PROJECT_ARCHIVE_BYTES:
        raise ToolError(f"Jobset produced more than {MAX_PROJECT_ARCHIVE_BYTES} output bytes")

    created_directories: list[tuple[str, ...]] = []
    temporaries: list[tuple[tuple[str, ...], str, str]] = []
    published: list[tuple[tuple[str, ...], str]] = []
    try:
        for relative in directories:
            parts = tuple(Path(relative).parts)
            with _open_relative_directory(project_fd, parts[:-1]) as parent_fd:
                if _stat_at(parent_fd, parts[-1], allow_missing=True) is not None:
                    raise ToolError(f"Jobset output already exists: {relative}")
                os.mkdir(parts[-1], 0o755, dir_fd=parent_fd)
                os.fsync(parent_fd)
            created_directories.append(parts)

        for relative in files:
            parts = tuple(Path(relative).parts)
            source = staged_project.joinpath(*parts)
            expected_size = added[relative][1]
            expected_digest = added[relative][2]
            with _open_relative_directory(project_fd, parts[:-1]) as parent_fd:
                if _stat_at(parent_fd, parts[-1], allow_missing=True) is not None:
                    raise ToolError(f"Jobset output already exists: {relative}")
                temporary, descriptor = _create_temp_at(
                    parent_fd, parts[-1], "jobset", suffix=source.suffix,
                )
                try:
                    with source.open("rb") as input_stream, os.fdopen(descriptor, "wb", closefd=True) as output_stream:
                        descriptor = -1
                        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                    if source.stat().st_size != expected_size or _file_sha256(source) != expected_digest:
                        raise ToolError("Jobset output changed while it was being staged for publication")
                except BaseException:
                    if descriptor >= 0:
                        os.close(descriptor)
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
                    raise
            temporaries.append((parts[:-1], temporary, parts[-1]))

        for parent_parts, temporary, name in temporaries:
            with _open_relative_directory(project_fd, parent_parts) as parent_fd:
                _rename_noreplace_at(parent_fd, temporary, parent_fd, name)
                os.fsync(parent_fd)
            published.append((parent_parts, name))
        temporaries.clear()
        return files
    except BaseException:
        for parent_parts, name in reversed(published):
            try:
                with _open_relative_directory(project_fd, parent_parts) as parent_fd:
                    os.unlink(name, dir_fd=parent_fd)
            except (OSError, ToolError):
                pass
        raise
    finally:
        for parent_parts, temporary, _ in temporaries:
            try:
                with _open_relative_directory(project_fd, parent_parts) as parent_fd:
                    os.unlink(temporary, dir_fd=parent_fd)
            except (OSError, ToolError):
                pass
        for parts in reversed(created_directories):
            try:
                with _open_relative_directory(project_fd, parts[:-1]) as parent_fd:
                    os.rmdir(parts[-1], dir_fd=parent_fd)
            except (OSError, ToolError):
                pass


def _project_fingerprint(path: Path) -> str:
    entry_stat = _symlink_free(path)
    if entry_stat is None:
        raise ToolError(f"Project path does not exist: {path}")
    if stat.S_ISREG(entry_stat.st_mode):
        return _file_sha256(path)
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise ToolError("Only regular files and directories are supported")
    return _tree_fingerprint(path)


def _require_fingerprint(path: Path, expected_sha256: str) -> str:
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ToolError("expected_sha256 must be one lowercase 64-character SHA-256 digest")
    actual = _project_fingerprint(path)
    if actual != expected_sha256:
        raise ToolError(f"Project path changed: expected SHA-256 {expected_sha256}, found {actual}")
    return actual


def _project_entry(path: Path, root: Path) -> dict[str, JsonValue]:
    entry_stat = os.lstat(path)
    relative = path.relative_to(PROJECT_ROOT).as_posix()
    if stat.S_ISLNK(entry_stat.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(entry_stat.st_mode):
        kind = "directory"
    elif stat.S_ISREG(entry_stat.st_mode):
        kind = "file"
    else:
        kind = "other"
    result: dict[str, JsonValue] = {
        "path": str(path), "relative_path": relative, "name": path.name, "kind": kind,
        "size": entry_stat.st_size, "modified_ns": entry_stat.st_mtime_ns,
    }
    if kind == "file":
        result["sha256"] = _file_sha256(path)
        result["mime_type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return result


@mcp.tool(annotations=_READ)
def list_project_files(
    directory: str = "/projects", recursive: bool = False,
    max_entries: Annotated[int, Field(ge=1, le=10000)] = 1000,
) -> dict[str, JsonValue]:
    """List one symlink-free project directory, optionally recursively, with hashes for regular files."""
    with _project_lock(exclusive=False):
        root, _ = _project_relative(directory, allow_root=True)
        root_stat = _symlink_free(root)
        if root_stat is None or not stat.S_ISDIR(root_stat.st_mode):
            raise ToolError("directory must name an existing project directory")
        iterator = root.rglob("*") if recursive else root.iterdir()
        entries = []
        for path in sorted(iterator, key=lambda item: item.relative_to(root).as_posix()):
            if path == PROJECT_ROOT / _PROJECT_LOCK_NAME:
                continue
            if len(entries) >= max_entries:
                raise ToolError(f"Directory contains more than max_entries={max_entries}; narrow the request")
            entry = _project_entry(path, root)
            if entry["kind"] == "symlink":
                raise ToolError(f"Project directories must not contain symlinks: {path}")
            entries.append(entry)
        return {"directory": str(root), "recursive": recursive, "count": len(entries), "entries": entries}


@mcp.tool(annotations=_READ)
def stat_project_path(path: str) -> dict[str, JsonValue]:
    """Return type, size, timestamps and a content/tree SHA-256 for one project path."""
    with _project_lock(exclusive=False):
        target, _ = _project_relative(path)
        entry = _project_entry(target, PROJECT_ROOT)
        if entry["kind"] in {"symlink", "other"}:
            raise ToolError("Only symlink-free regular files and directories are supported")
        entry["sha256"] = _project_fingerprint(target)
        return entry


@mcp.tool(annotations=_EXPORT)
def create_project_directory(path: str) -> dict[str, JsonValue]:
    """Create one new directory under an existing symlink-free project parent."""
    target, _ = _project_relative(path)
    with _project_lock(), _open_project_parent(str(target)) as (_, parent_fd, target_name):
        if _stat_at(parent_fd, target_name, allow_missing=True) is not None:
            raise ToolError(f"Output already exists: {target}")
        try:
            os.mkdir(target_name, mode=0o755, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ToolError(f"Output already exists: {target}") from exc
    return {"path": str(target), "kind": "directory", "sha256": hashlib.sha256().hexdigest()}


@mcp.tool(annotations=_EXPORT)
def upload_project_file(
    path: str,
    content_base64: Annotated[str, Field(max_length=(MAX_PROJECT_FILE_BYTES * 4 // 3) + 16)],
    expected_sha256: Annotated[str | None, Field(min_length=64, max_length=64)] = None,
) -> dict[str, JsonValue]:
    """Create or CAS-replace one regular project file from exact base64 bytes, bounded to 16 MiB."""
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolError("content_base64 must be canonical base64") from exc
    if len(content) > MAX_PROJECT_FILE_BYTES:
        raise ToolError(f"Decoded file exceeds the {MAX_PROJECT_FILE_BYTES}-byte upload limit")
    target, _ = _project_relative(path)
    with _project_lock(), _open_project_parent(str(target)) as (_, parent_fd, target_name):
        temporary = _write_temp_at(parent_fd, target_name, content, "upload")
        try:
            _publish_temp_at(parent_fd, temporary, target_name, expected_sha256)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
    return {"path": str(target), "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "mime_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream"}


@mcp.tool(annotations=_EXPORT)
def copy_project_path(source: str, destination: str, expected_sha256: Annotated[str, Field(min_length=64, max_length=64)]) -> dict[str, JsonValue]:
    """Copy one symlink-free file or directory to a new project path after a SHA-256 guard."""
    source_path, _ = _project_relative(source)
    destination_path, _ = _project_relative(destination)
    with _project_lock():
        source_stat = _symlink_free(source_path)
        if source_stat is None:
            raise ToolError("Source does not exist")
        with _open_project_parent(str(destination_path)) as (_, destination_parent_fd, destination_name):
            if _stat_at(destination_parent_fd, destination_name, allow_missing=True) is not None:
                raise ToolError(f"Destination already exists: {destination_path}")
            if stat.S_ISREG(source_stat.st_mode):
                with _open_project_file(str(source_path)) as (_, source_stream, before):
                    source_digest = hashlib.sha256()
                    for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                        source_digest.update(chunk)
                    if source_digest.hexdigest() != expected_sha256:
                        raise ToolError("Source changed before it was copied")
                    source_stream.seek(0)
                    temporary, temporary_fd = _create_temp_at(destination_parent_fd, destination_name, "copy")
                    try:
                        digest = hashlib.sha256()
                        with os.fdopen(temporary_fd, "wb", closefd=True) as output_stream:
                            temporary_fd = -1
                            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                                digest.update(chunk)
                                output_stream.write(chunk)
                            output_stream.flush()
                            os.fsync(output_stream.fileno())
                        after = os.fstat(source_stream.fileno())
                        if digest.hexdigest() != expected_sha256 or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                            raise ToolError("Source changed while it was being copied")
                        os.link(temporary, destination_name, src_dir_fd=destination_parent_fd,
                                dst_dir_fd=destination_parent_fd, follow_symlinks=False)
                    finally:
                        if temporary_fd >= 0:
                            os.close(temporary_fd)
                        try:
                            os.unlink(temporary, dir_fd=destination_parent_fd)
                        except FileNotFoundError:
                            pass
                destination_digest = expected_sha256
            elif stat.S_ISDIR(source_stat.st_mode):
                with _open_project_directory(str(source_path)) as (_, source_fd):
                    stable_source = Path(f"/proc/self/fd/{source_fd}")
                    if _tree_fingerprint(stable_source) != expected_sha256:
                        raise ToolError("Source changed before it was copied")
                    temporary = f".{destination_name}.copy-{uuid.uuid4().hex}"
                    os.mkdir(temporary, mode=0o700, dir_fd=destination_parent_fd)
                    stable_destination = Path(f"/proc/self/fd/{destination_parent_fd}") / temporary
                    try:
                        shutil.copytree(stable_source, stable_destination, dirs_exist_ok=True, symlinks=True)
                        if _tree_fingerprint(stable_destination) != expected_sha256 or _tree_fingerprint(stable_source) != expected_sha256:
                            raise ToolError("Source changed while it was being copied")
                        _rename_noreplace_at(
                            destination_parent_fd, temporary, destination_parent_fd, destination_name,
                        )
                    except BaseException:
                        shutil.rmtree(stable_destination, ignore_errors=True)
                        raise
                destination_digest = expected_sha256
            else:
                raise ToolError("Only regular files and directories can be copied")
    return {"source": str(source_path), "destination": str(destination_path),
            "sha256": destination_digest}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
def move_project_path(source: str, destination: str, expected_sha256: Annotated[str, Field(min_length=64, max_length=64)]) -> dict[str, JsonValue]:
    """Atomically move/rename one project path after a file/tree SHA-256 guard."""
    source_path, _ = _project_relative(source)
    destination_path, _ = _project_relative(destination)
    with _project_lock(), _open_project_parent(str(source_path)) as (_, source_parent_fd, source_name), \
            _open_project_parent(str(destination_path)) as (_, destination_parent_fd, destination_name):
        source_stat = _stat_at(source_parent_fd, source_name)
        if _stat_at(destination_parent_fd, destination_name, allow_missing=True) is not None:
            raise ToolError(f"Destination already exists: {destination_path}")
        if source_stat is not None and stat.S_ISREG(source_stat.st_mode):
            _require_file_sha256_at(source_parent_fd, source_name, expected_sha256)
        elif source_stat is not None and stat.S_ISDIR(source_stat.st_mode):
            stable_source = Path(f"/proc/self/fd/{source_parent_fd}") / source_name
            if _tree_fingerprint(stable_source) != expected_sha256:
                raise ToolError("Source directory changed")
        else:
            raise ToolError("Only regular files and directories can be moved")
        _rename_noreplace_at(source_parent_fd, source_name, destination_parent_fd, destination_name)
    return {"source": str(source_path), "destination": str(destination_path),
            "sha256": expected_sha256}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False))
def delete_project_path(
    path: str, expected_sha256: Annotated[str, Field(min_length=64, max_length=64)],
    confirm: Literal[True], recursive: bool = False,
) -> dict[str, JsonValue]:
    """Delete one guarded project file, or one guarded directory only when recursive=true."""
    target, _ = _project_relative(path)
    with _project_lock(), _open_project_parent(str(target)) as (_, parent_fd, target_name):
        target_stat = _stat_at(parent_fd, target_name)
        if target_stat is not None and stat.S_ISREG(target_stat.st_mode):
            _require_file_sha256_at(parent_fd, target_name, expected_sha256)
            os.unlink(target_name, dir_fd=parent_fd)
        elif target_stat is not None and stat.S_ISDIR(target_stat.st_mode):
            if not recursive:
                raise ToolError("recursive=true is required to delete a directory")
            stable_target = Path(f"/proc/self/fd/{parent_fd}") / target_name
            if _tree_fingerprint(stable_target) != expected_sha256:
                raise ToolError("Project directory changed")
            shutil.rmtree(stable_target)
        else:
            raise ToolError("Only regular files and directories can be deleted")
    return {"path": str(target), "deleted": True, "sha256": expected_sha256}


@mcp.tool(annotations=_EXPORT)
def create_project_archive(
    source_path: str, output_path: str,
    expected_sha256: Annotated[str, Field(min_length=64, max_length=64)],
) -> dict[str, JsonValue]:
    """Create a bounded ZIP from one guarded symlink-free project file or tree."""
    source, _ = _project_relative(source_path)
    destination, _ = _project_relative(output_path)
    if destination.suffix.lower() != ".zip":
        raise ToolError("output_path must end with .zip")
    with _project_lock():
        source_stat = _symlink_free(source)
        source_digest = _require_fingerprint(source, expected_sha256)
        if source_stat is None or not (stat.S_ISREG(source_stat.st_mode) or stat.S_ISDIR(source_stat.st_mode)):
            raise ToolError("source_path must name one regular file or directory")
        if stat.S_ISDIR(source_stat.st_mode) and destination.is_relative_to(source):
            raise ToolError("The archive output must not be inside its source tree")
        if _symlink_free(destination, allow_missing_final=True) is not None:
            raise ToolError(f"Output already exists: {destination}")
        parent_stat = _symlink_free(destination.parent)
        if parent_stat is None or not stat.S_ISDIR(parent_stat.st_mode):
            raise ToolError("The output parent must be a project directory")

        paths = [source] if stat.S_ISREG(source_stat.st_mode) else sorted(
            source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()
        )
        if len(paths) > MAX_PROJECT_ARCHIVE_FILES:
            raise ToolError(f"Source contains more than {MAX_PROJECT_ARCHIVE_FILES} entries")
        total = 0
        for path in paths:
            entry_stat = os.lstat(path)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise ToolError(f"Project archives cannot contain symlinks: {path}")
            if stat.S_ISREG(entry_stat.st_mode):
                total += entry_stat.st_size
                if total > MAX_PROJECT_ARCHIVE_BYTES:
                    raise ToolError(f"Archive input exceeds {MAX_PROJECT_ARCHIVE_BYTES} bytes")
            elif not stat.S_ISDIR(entry_stat.st_mode):
                raise ToolError(f"Project archives support only regular files and directories: {path}")

        with _open_project_parent(str(destination)) as (_, output_parent_fd, output_name):
            if _stat_at(output_parent_fd, output_name, allow_missing=True) is not None:
                raise ToolError(f"Output already exists: {destination}")
            temporary, temporary_fd = _create_temp_at(output_parent_fd, output_name, "archive")
            file_count = 0
            try:
                with os.fdopen(temporary_fd, "w+b", closefd=True) as archive_stream:
                    temporary_fd = -1
                    with zipfile.ZipFile(archive_stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                         allowZip64=True, strict_timestamps=False) as bundle:
                        for path in paths:
                            relative = Path(source.name) if path == source else Path(source.name) / path.relative_to(source)
                            entry_stat = os.lstat(path)
                            if stat.S_ISDIR(entry_stat.st_mode):
                                info = zipfile.ZipInfo(relative.as_posix().rstrip("/") + "/")
                                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                                bundle.writestr(info, b"")
                                continue
                            with _open_project_file(str(path)) as (_, input_stream, before):
                                with bundle.open(relative.as_posix(), "w", force_zip64=True) as output_stream:
                                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                                after = os.fstat(input_stream.fileno())
                            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                                raise ToolError(f"Project file changed while being archived: {path}")
                            file_count += 1
                    archive_stream.flush()
                    os.fsync(archive_stream.fileno())
                    archive_size = os.fstat(archive_stream.fileno()).st_size
                if archive_size > MAX_PROJECT_ARCHIVE_BYTES:
                    raise ToolError(f"Compressed archive exceeds {MAX_PROJECT_ARCHIVE_BYTES} bytes")
                _require_fingerprint(source, expected_sha256)
                os.link(temporary, output_name, src_dir_fd=output_parent_fd,
                        dst_dir_fd=output_parent_fd, follow_symlinks=False)
                archive_digest = _file_sha256_at(output_parent_fd, output_name)
            except BaseException:
                if temporary_fd >= 0:
                    os.close(temporary_fd)
                raise
            finally:
                try:
                    os.unlink(temporary, dir_fd=output_parent_fd)
                except FileNotFoundError:
                    pass
    return {"source_path": str(source), "source_sha256": source_digest, "output_path": str(destination),
            "entry_count": len(paths), "file_count": file_count, "expanded_bytes": total,
            "size": archive_size, "sha256": archive_digest}


@mcp.tool(annotations=_EXPORT)
def extract_project_archive(
    archive_path: str, output_dir: str,
    expected_sha256: Annotated[str, Field(min_length=64, max_length=64)],
) -> dict[str, JsonValue]:
    """Extract one guarded ZIP into a new project directory; reject traversal, links and zip bombs."""
    archive, _ = _project_relative(archive_path)
    destination, _ = _project_relative(output_dir)
    with _project_lock(), _open_project_file(str(archive)) as (_, archive_stream, archive_stat):
        if archive.suffix.lower() != ".zip":
            raise ToolError("archive_path must name one regular .zip file")
        if archive_stat.st_size > MAX_PROJECT_ARCHIVE_BYTES:
            raise ToolError(f"Archive exceeds {MAX_PROJECT_ARCHIVE_BYTES} bytes")
        archive_digest = hashlib.sha256()
        for chunk in iter(lambda: archive_stream.read(1024 * 1024), b""):
            archive_digest.update(chunk)
        if archive_digest.hexdigest() != expected_sha256:
            raise ToolError(f"Project path changed: expected SHA-256 {expected_sha256}, found {archive_digest.hexdigest()}")
        archive_stream.seek(0)
        with _open_project_parent(str(destination)) as (_, destination_parent_fd, destination_name):
            if _stat_at(destination_parent_fd, destination_name, allow_missing=True) is not None:
                raise ToolError(f"Output already exists: {destination}")
            os.mkdir(destination_name, mode=0o700, dir_fd=destination_parent_fd)
            destination_fd = os.open(
                destination_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=destination_parent_fd,
            )
            stable_destination = Path(f"/proc/self/fd/{destination_fd}")
            try:
                with zipfile.ZipFile(archive_stream) as bundle:
                    members = bundle.infolist()
                    if len(members) > MAX_PROJECT_ARCHIVE_FILES:
                        raise ToolError(f"Archive contains more than {MAX_PROJECT_ARCHIVE_FILES} entries")
                    total = 0
                    seen: set[str] = set()
                    file_count = 0
                    for member in members:
                        total += member.file_size
                        if total > MAX_PROJECT_ARCHIVE_BYTES:
                            raise ToolError(f"Archive expands beyond {MAX_PROJECT_ARCHIVE_BYTES} bytes")
                        name = member.filename.replace("\\", "/")
                        relative = Path(name)
                        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                            raise ToolError(f"Unsafe archive path: {member.filename}")
                        normalized = relative.as_posix().rstrip("/")
                        if normalized in seen:
                            raise ToolError(f"Duplicate archive path: {member.filename}")
                        seen.add(normalized)
                        if member.flag_bits & 1:
                            raise ToolError(f"Encrypted archive entries are not supported: {member.filename}")
                        if member.file_size > max(1, member.compress_size) * 1000:
                            raise ToolError(f"Archive entry has an unsafe compression ratio: {member.filename}")
                        mode = member.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        if file_type == stat.S_IFLNK:
                            raise ToolError(f"Archive links are not allowed: {member.filename}")
                        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                            raise ToolError(f"Archive special files are not allowed: {member.filename}")
                        parts = relative.parts
                        if member.is_dir():
                            with _open_relative_directory(destination_fd, parts, create=True):
                                pass
                            continue
                        with _open_relative_directory(destination_fd, parts[:-1], create=True) as output_parent_fd:
                            output_fd = os.open(
                                parts[-1],
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=output_parent_fd,
                            )
                            with bundle.open(member) as member_stream, os.fdopen(output_fd, "wb", closefd=True) as output:
                                shutil.copyfileobj(member_stream, output, length=1024 * 1024)
                                output.flush()
                                os.fsync(output.fileno())
                        file_count += 1
                after = os.fstat(archive_stream.fileno())
                if after.st_size != archive_stat.st_size or after.st_mtime_ns != archive_stat.st_mtime_ns:
                    raise ToolError("The project archive changed while it was being extracted")
                destination_digest = _tree_fingerprint(stable_destination)
                extracted_entry_count = sum(1 for _ in stable_destination.rglob("*"))
            except BaseException:
                shutil.rmtree(destination_name, dir_fd=destination_parent_fd, ignore_errors=True)
                raise
            finally:
                os.close(destination_fd)
    return {"archive_path": str(archive), "output_dir": str(destination), "entry_count": extracted_entry_count,
            "file_count": file_count, "compressed_bytes": archive_stat.st_size, "expanded_bytes": total,
            "sha256": destination_digest}


@mcp.resource("kicad-project:///", name="project-index", title="KiCad project file index", mime_type="application/json")
def project_index_resource() -> str:
    """A bounded non-recursive index of the project root."""
    return json.dumps(list_project_files("/projects", recursive=False, max_entries=10000), sort_keys=True)


@mcp.resource("kicad-project:///{+path}", name="project-file", title="KiCad project file", mime_type="application/octet-stream")
def project_file_resource(path: str) -> bytes:
    """Read one regular project file by URI path through the same symlink-free bounded reader."""
    _, content = _read_project_file(str(PROJECT_ROOT / path.lstrip("/")))
    return content


def _open_kicad_cst(path: Path, expected_head: str):
    with _open_project_file(str(path)) as (_, source, entry_stat):
        if entry_stat.st_size > MAX_PROJECT_ARCHIVE_BYTES:
            raise ToolError(f"KiCad file exceeds the {MAX_PROJECT_ARCHIVE_BYTES}-byte edit limit")
        data = source.read(MAX_PROJECT_ARCHIVE_BYTES + 1)
    try:
        tree = _cst.parse(data)
    except SyntaxError as exc:
        raise ToolError(f"Invalid KiCad S-expression file: {exc}") from exc
    if len(tree.lists) != 1 or tree.lists[0].head != expected_head:
        raise ToolError(f"Expected one ({expected_head} ...) document")
    return tree, tree.lists[0]


def _validate_pcbnew_file(path: Path, *, footprint: bool = False, pass_fds: tuple[int, ...] = ()) -> None:
    pcbnew_python = Path("/usr/lib/python3/dist-packages/pcbnew.py")
    if not pcbnew_python.exists():
        return
    if footprint:
        script = ("import pcbnew,sys; f=pcbnew.FootprintLoad(str(sys.argv[1]),sys.argv[2]); "
                  "assert f is not None")
        command = ["/usr/bin/python3", "-c", script, str(path.parent), path.stem]
    else:
        script = "import pcbnew,sys; b=pcbnew.LoadBoard(sys.argv[1]); assert b is not None"
        command = ["/usr/bin/python3", "-c", script, str(path)]
    _execute(command, path.parent, timeout=120, pass_fds=pass_fds)


def _publish_kicad_cst(source: Path, data: bytes, expected_sha256: str, *, footprint: bool = False) -> str:
    with _open_project_parent(str(source)) as (_, parent_fd, target_name):
        temporary = _write_temp_at(parent_fd, target_name, data, "edit", suffix=source.suffix)
        temporary_path = Path(f"/proc/self/fd/{parent_fd}") / temporary
        try:
            _validate_pcbnew_file(temporary_path, footprint=footprint, pass_fds=(parent_fd,))
            _require_file_sha256_at(parent_fd, target_name, expected_sha256)
            os.replace(temporary, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            raise
    return hashlib.sha256(data).hexdigest()


SchematicItemType = Literal[
    "bus", "bus_entry", "text_box", "text", "polyline", "rectangle", "circle", "arc", "image"
]


class SchematicItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "replace", "delete"]
    item_type: SchematicItemType
    uuid: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F-]{36}$")] = None
    sexpr: Annotated[str | None, Field(max_length=256 * 1024)] = None

    @model_validator(mode="after")
    def validate_shape(self) -> "SchematicItemEdit":
        if self.action == "add" and (self.uuid is not None or self.sexpr is None):
            raise ValueError("add requires sexpr and generates its own UUID")
        if self.action in {"replace", "delete"} and self.uuid is None:
            raise ValueError(f"{self.action} requires uuid")
        if self.action == "delete" and self.sexpr is not None:
            raise ValueError("delete does not accept sexpr")
        if self.action == "replace" and self.sexpr is None:
            raise ValueError("replace requires sexpr")
        return self


def _schematic_item_uuid(node) -> str | None:
    uuid_node = node.find("uuid")
    if uuid_node is None or len(uuid_node.atoms) < 2:
        return None
    value = uuid_node.atoms[1].text
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _schematic_item_node(item_type: str, sexpr: str, item_uuid: str):
    try:
        parsed = _cst.parse(sexpr.encode("utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise ToolError(f"Invalid schematic item S-expression: {exc}") from exc
    if len(parsed.lists) != 1 or parsed.atoms or parsed.lists[0].head != item_type:
        raise ToolError(f"sexpr must contain exactly one ({item_type} ...) item")
    node = parsed.lists[0]
    uuid_node = node.find("uuid")
    if uuid_node is None or len(uuid_node.atoms) != 2:
        raise ToolError("Schematic item sexpr must contain exactly one (uuid ...) field")
    uuid_node.atoms[1].set_text(item_uuid)
    return node


@mcp.tool(annotations=_READ)
def get_schematic_items(schematic_path: str) -> dict[str, JsonValue]:
    """Return full-fidelity editable graphical/bus schematic items with UUIDs and S-expressions."""
    source = _path(schematic_path, ".kicad_sch")
    allowed = set(SchematicItemType.__args__)
    with _project_lock(exclusive=False):
        tree, root, *_ = upstream.schematic._open_sch_cst(str(source))
        items: list[JsonValue] = []
        for node in root.lists:
            if node.head not in allowed:
                continue
            item_uuid = _schematic_item_uuid(node)
            if item_uuid is None:
                raise ToolError(f"Editable schematic item {node.head} has no valid UUID")
            items.append({"item_type": node.head, "uuid": item_uuid,
                          "sexpr": _cst.serialize(node).decode("utf-8", "surrogateescape")})
        return {"schematic_path": str(source), "sha256": _file_sha256(source),
                "count": len(items), "items": items}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def edit_schematic_items(
    schematic_path: str,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    operations: Annotated[list[SchematicItemEdit], Field(min_length=1, max_length=1000)],
) -> dict[str, JsonValue]:
    """CAS-edit buses, bus entries and advanced graphical schematic items by stable UUID."""
    source = _path(schematic_path, ".kicad_sch")
    allowed = set(SchematicItemType.__args__)
    with _project_lock():
        _require_fingerprint(source, expected_sha256)
        tree, root, *_ = upstream.schematic._open_sch_cst(str(source))
        changed: list[JsonValue] = []
        for operation in operations:
            matches = [node for node in root.lists if node.head in allowed and
                       _schematic_item_uuid(node) == operation.uuid]
            if operation.action != "add":
                if len(matches) != 1:
                    raise ToolError(f"Expected one schematic item with UUID {operation.uuid}; found {len(matches)}")
                target = matches[0]
                if target.head != operation.item_type:
                    raise ToolError(f"UUID {operation.uuid} is {target.head}, not {operation.item_type}")
            if operation.action == "delete":
                root.remove_child(matches[0])
                changed.append({"action": "delete", "item_type": operation.item_type,
                                "uuid": operation.uuid or ""})
                continue
            item_uuid = str(uuid.uuid4()) if operation.action == "add" else str(operation.uuid)
            replacement = _schematic_item_node(operation.item_type, operation.sexpr or "", item_uuid)
            if operation.action == "add":
                if any(_schematic_item_uuid(node) == item_uuid for node in root.lists):
                    raise ToolError(f"Generated duplicate schematic UUID: {item_uuid}")
                upstream.schematic._splice_sch_node(root, operation.item_type, replacement)
            else:
                target = matches[0]
                replacement.sep = target.sep
                root.children[root.children.index(target)] = replacement
            changed.append({"action": operation.action, "item_type": operation.item_type, "uuid": item_uuid})

        data = _cst.serialize(tree)
        with _open_project_parent(str(source)) as (_, parent_fd, target_name):
            temporary = _write_temp_at(parent_fd, target_name, data, "schematic", suffix=source.suffix)
            validation = f".{target_name}.validation-{uuid.uuid4().hex}.net"
            stable_parent = Path(f"/proc/self/fd/{parent_fd}")
            try:
                if shutil.which("kicad-cli"):
                    _execute(
                        ["kicad-cli", "sch", "export", "netlist", "-o", str(stable_parent / validation),
                         str(stable_parent / temporary)],
                        stable_parent, timeout=120, pass_fds=(parent_fd,),
                    )
                _require_file_sha256_at(parent_fd, target_name, expected_sha256)
                os.replace(temporary, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                raise
            finally:
                try:
                    os.unlink(validation, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        result = get_schematic_items(str(source))
        result["operations"] = changed
        return result


BoardItemType = Literal[
    "gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve", "gr_text", "gr_text_box",
    "dimension", "zone", "footprint", "image", "target"
]


class BoardItemEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["add", "replace", "delete"]
    item_type: BoardItemType
    uuid: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F-]{36}$")] = None
    sexpr: Annotated[str | None, Field(max_length=4 * 1024 * 1024)] = None

    @model_validator(mode="after")
    def validate_shape(self) -> "BoardItemEdit":
        if self.action == "add" and (self.uuid is not None or self.sexpr is None):
            raise ValueError("add requires sexpr and generates its own UUID")
        if self.action in {"replace", "delete"} and self.uuid is None:
            raise ValueError(f"{self.action} requires uuid")
        if self.action == "delete" and self.sexpr is not None:
            raise ValueError("delete does not accept sexpr")
        if self.action == "replace" and self.sexpr is None:
            raise ValueError("replace requires sexpr")
        return self


def _uuid_item_node(item_type: str, sexpr: str, item_uuid: str):
    try:
        parsed = _cst.parse(sexpr.encode("utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise ToolError(f"Invalid {item_type} S-expression: {exc}") from exc
    if len(parsed.lists) != 1 or parsed.atoms or parsed.lists[0].head != item_type:
        raise ToolError(f"sexpr must contain exactly one ({item_type} ...) item")
    node = parsed.lists[0]
    uuid_node = node.find("uuid")
    if uuid_node is None or len(uuid_node.atoms) != 2:
        raise ToolError(f"{item_type} sexpr must contain exactly one (uuid ...) field")
    uuid_node.atoms[1].set_text(item_uuid)
    return node


@mcp.tool(annotations=_READ)
def get_board_items(pcb_path: str) -> dict[str, JsonValue]:
    """Return full-fidelity editable board graphics, text, dimensions, zones and footprints."""
    source = _path(pcb_path, ".kicad_pcb")
    allowed = set(BoardItemType.__args__)
    with _project_lock(exclusive=False):
        _, root = _open_kicad_cst(source, "kicad_pcb")
        items: list[JsonValue] = []
        for node in root.lists:
            if node.head not in allowed:
                continue
            item_uuid = _schematic_item_uuid(node)
            if item_uuid is None:
                raise ToolError(f"Editable board item {node.head} has no valid UUID")
            items.append({"item_type": node.head, "uuid": item_uuid,
                          "sexpr": _cst.serialize(node).decode("utf-8", "surrogateescape")})
        return {"pcb_path": str(source), "sha256": _file_sha256(source), "count": len(items), "items": items}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def edit_board_items(
    pcb_path: str,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    operations: Annotated[list[BoardItemEdit], Field(min_length=1, max_length=1000)],
) -> dict[str, JsonValue]:
    """CAS-edit complete board graphics/text/dimensions/zones/keepouts/footprints by UUID."""
    source = _path(pcb_path, ".kicad_pcb")
    allowed = set(BoardItemType.__args__)
    with _project_lock():
        _require_fingerprint(source, expected_sha256)
        tree, root = _open_kicad_cst(source, "kicad_pcb")
        changed: list[JsonValue] = []
        for operation in operations:
            matches = [node for node in root.lists if node.head in allowed and
                       _schematic_item_uuid(node) == operation.uuid]
            if operation.action != "add":
                if len(matches) != 1:
                    raise ToolError(f"Expected one board item with UUID {operation.uuid}; found {len(matches)}")
                target = matches[0]
                if target.head != operation.item_type:
                    raise ToolError(f"UUID {operation.uuid} is {target.head}, not {operation.item_type}")
            if operation.action == "delete":
                root.remove_child(matches[0])
                changed.append({"action": "delete", "item_type": operation.item_type,
                                "uuid": operation.uuid or ""})
                continue
            item_uuid = str(uuid.uuid4()) if operation.action == "add" else str(operation.uuid)
            replacement = _uuid_item_node(operation.item_type, operation.sexpr or "", item_uuid)
            if operation.action == "add":
                if any(_schematic_item_uuid(node) == item_uuid for node in root.lists):
                    raise ToolError(f"Generated duplicate board UUID: {item_uuid}")
                tail = root.find("embedded_fonts")
                if tail is not None:
                    root.insert_before(tail, replacement)
                else:
                    root.append_child(replacement, b"\n  ")
            else:
                target = matches[0]
                replacement.sep = target.sep
                root.children[root.children.index(target)] = replacement
            changed.append({"action": operation.action, "item_type": operation.item_type, "uuid": item_uuid})
        new_digest = _publish_kicad_cst(source, _cst.serialize(tree), expected_sha256)
        result = get_board_items(str(source))
        result["sha256"] = new_digest
        result["operations"] = changed
        return result


BoardSection = Literal["general", "paper", "layers", "setup"]


@mcp.tool(annotations=_READ)
def get_board_section(pcb_path: str, section: BoardSection) -> dict[str, JsonValue]:
    """Read one full-fidelity board setup section, including stackup and design settings."""
    source = _path(pcb_path, ".kicad_pcb")
    with _project_lock(exclusive=False):
        _, root = _open_kicad_cst(source, "kicad_pcb")
        matches = root.find_all(section)
        if len(matches) != 1:
            raise ToolError(f"Expected one ({section} ...) section; found {len(matches)}")
        return {"pcb_path": str(source), "sha256": _file_sha256(source), "section": section,
                "sexpr": _cst.serialize(matches[0]).decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def replace_board_section(
    pcb_path: str, section: BoardSection,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    sexpr: Annotated[str, Field(min_length=3, max_length=4 * 1024 * 1024)],
) -> dict[str, JsonValue]:
    """CAS-replace general/paper/layers/setup; setup includes physical stackup and board defaults."""
    source = _path(pcb_path, ".kicad_pcb")
    with _project_lock():
        _require_fingerprint(source, expected_sha256)
        tree, root = _open_kicad_cst(source, "kicad_pcb")
        matches = root.find_all(section)
        if len(matches) != 1:
            raise ToolError(f"Expected one ({section} ...) section; found {len(matches)}")
        try:
            parsed = _cst.parse(sexpr.encode("utf-8"))
        except (SyntaxError, UnicodeError) as exc:
            raise ToolError(f"Invalid board section S-expression: {exc}") from exc
        if len(parsed.lists) != 1 or parsed.atoms or parsed.lists[0].head != section:
            raise ToolError(f"sexpr must contain exactly one ({section} ...) section")
        replacement = parsed.lists[0]
        target = matches[0]
        replacement.sep = target.sep
        root.children[root.children.index(target)] = replacement
        digest = _publish_kicad_cst(source, _cst.serialize(tree), expected_sha256)
        return {"pcb_path": str(source), "sha256": digest, "section": section,
                "sexpr": _cst.serialize(replacement).decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=_READ)
def get_footprint_definition(footprint_path: str) -> dict[str, JsonValue]:
    """Read a complete native footprint definition, including graphics, texts, padstacks and 3D models."""
    source = _path(footprint_path, ".kicad_mod")
    with _project_lock(exclusive=False):
        tree, _ = _open_kicad_cst(source, "footprint")
        return {"footprint_path": str(source), "sha256": _file_sha256(source),
                "sexpr": _cst.serialize(tree).decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def replace_footprint_definition(
    footprint_path: str,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    sexpr: Annotated[str, Field(min_length=10, max_length=16 * 1024 * 1024)],
) -> dict[str, JsonValue]:
    """CAS-replace a complete footprint and validate it with KiCad before atomic publication."""
    source = _path(footprint_path, ".kicad_mod")
    with _project_lock():
        _require_fingerprint(source, expected_sha256)
        try:
            tree = _cst.parse(sexpr.encode("utf-8"))
        except (SyntaxError, UnicodeError) as exc:
            raise ToolError(f"Invalid footprint S-expression: {exc}") from exc
        if len(tree.lists) != 1 or tree.atoms or tree.lists[0].head != "footprint":
            raise ToolError("sexpr must contain exactly one (footprint ...) document")
        digest = _publish_kicad_cst(source, _cst.serialize(tree), expected_sha256, footprint=True)
        return {"footprint_path": str(source), "sha256": digest,
                "sexpr": _cst.serialize(tree).decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=_READ)
def get_project_settings(project_path: str) -> dict[str, JsonValue]:
    """Read the complete .kicad_pro JSON, preserving every KiCad 10 project setting and text variable."""
    source = _path(project_path, ".kicad_pro")
    path, content = _read_project_file(str(source))
    try:
        settings = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolError(f"Invalid KiCad project JSON: {exc}") from exc
    if not isinstance(settings, dict):
        raise ToolError("A .kicad_pro document must be one JSON object")
    return {"project_path": str(path), "sha256": hashlib.sha256(content).hexdigest(), "settings": settings}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def replace_project_settings(
    project_path: str,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    settings: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """CAS-replace the complete .kicad_pro JSON with deterministic UTF-8 output."""
    source = _path(project_path, ".kicad_pro")
    content = (json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True,
                          allow_nan=False) + "\n").encode("utf-8")
    result = upload_project_file(str(source), base64.b64encode(content).decode("ascii"), expected_sha256)
    return {"project_path": str(source), "sha256": result["sha256"], "settings": settings}


LibraryTableKind = Literal["sym", "fp"]


def _validate_library_uri(value: str) -> None:
    variable = re.compile(r"^\$\{KICAD\d+_(?:SYMBOL|FOOTPRINT)_DIR\}(?:/.*)?$")
    if value == "${KIPRJMOD}" or value.startswith("${KIPRJMOD}/"):
        suffix = value.removeprefix("${KIPRJMOD}").lstrip("/")
        if any(part in {"", ".", ".."} for part in Path(suffix).parts):
            raise ToolError("${KIPRJMOD} library URIs must not contain traversal")
        return
    if variable.fullmatch(value):
        return
    path = Path(value)
    if path.is_absolute():
        try:
            _project_relative(value)
        except ToolError:
            _validate_trusted_library_path(path)
        return
    raise ToolError("Library URIs must use ${KIPRJMOD}, a pinned KiCad library variable, /projects, or a trusted library root")


def _validate_library_table(root) -> None:
    for row in root.find_all("lib"):
        uri = row.find("uri")
        if uri is None or len(uri.atoms) != 2:
            raise ToolError("Each library table row must contain exactly one (uri ...) field")
        _validate_library_uri(uri.atoms[1].text)


@mcp.tool(annotations=_READ)
def get_project_library_table(directory: str, kind: LibraryTableKind) -> dict[str, JsonValue]:
    """Read a complete sym-lib-table or fp-lib-table with all plugins, options and descriptions."""
    root = _path(directory, directory=True)
    name = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    table = _path(str(root / name))
    _, content = _read_project_file(str(table))
    try:
        tree = _cst.parse(content)
    except SyntaxError as exc:
        raise ToolError(f"Invalid {name}: {exc}") from exc
    expected_head = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    if len(tree.lists) != 1 or tree.lists[0].head != expected_head:
        raise ToolError(f"{name} must contain one ({expected_head} ...) document")
    return {"directory": str(root), "kind": kind, "path": str(table),
            "sha256": hashlib.sha256(content).hexdigest(),
            "sexpr": content.decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def replace_project_library_table(
    directory: str, kind: LibraryTableKind,
    sexpr: Annotated[str, Field(min_length=10, max_length=4 * 1024 * 1024)],
    expected_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None,
) -> dict[str, JsonValue]:
    """Create or CAS-replace a complete project symbol/footprint library table."""
    root = _path(directory, directory=True)
    name = "sym-lib-table" if kind == "sym" else "fp-lib-table"
    expected_head = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    try:
        tree = _cst.parse(sexpr.encode("utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise ToolError(f"Invalid {name}: {exc}") from exc
    if len(tree.lists) != 1 or tree.atoms or tree.lists[0].head != expected_head:
        raise ToolError(f"sexpr must contain exactly one ({expected_head} ...) document")
    _validate_library_table(tree.lists[0])
    content = _cst.serialize(tree)
    table = root / name
    result = upload_project_file(str(table), base64.b64encode(content).decode("ascii"), expected_sha256)
    return {"directory": str(root), "kind": kind, "path": str(table), "sha256": result["sha256"],
            "sexpr": content.decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=_READ)
def get_design_rules(rules_path: str) -> dict[str, JsonValue]:
    """Read a complete .kicad_dru rules DSL document without losing comments or unknown rule fields."""
    source = _path(rules_path, ".kicad_dru")
    _, content = _read_project_file(str(source))
    try:
        tree = _cst.parse(content)
    except SyntaxError as exc:
        raise ToolError(f"Invalid KiCad design rules: {exc}") from exc
    if not tree.find_all("version") or any(node.head not in {"version", "rule"} for node in tree.lists):
        raise ToolError("A .kicad_dru document must contain version and rule forms only")
    return {"rules_path": str(source), "sha256": hashlib.sha256(content).hexdigest(),
            "sexpr": content.decode("utf-8", "surrogateescape")}


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                      open_world_hint=False))
def replace_design_rules(
    rules_path: str,
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
    sexpr: Annotated[str, Field(min_length=10, max_length=4 * 1024 * 1024)],
) -> dict[str, JsonValue]:
    """CAS-replace a complete .kicad_dru rules DSL after lossless syntax validation."""
    source = _path(rules_path, ".kicad_dru")
    try:
        tree = _cst.parse(sexpr.encode("utf-8"))
    except (SyntaxError, UnicodeError) as exc:
        raise ToolError(f"Invalid KiCad design rules: {exc}") from exc
    if not tree.find_all("version") or any(node.head not in {"version", "rule"} for node in tree.lists):
        raise ToolError("A .kicad_dru document must contain version and rule forms only")
    content = _cst.serialize(tree)
    result = upload_project_file(str(source), base64.b64encode(content).decode("ascii"), expected_sha256)
    return {"rules_path": str(source), "sha256": result["sha256"],
            "sexpr": content.decode("utf-8", "surrogateescape")}


def _execute(
    command: list[str], cwd: Path, timeout: int = 180, request: dict[str, object] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> str:
    """No shell and no arbitrary user argv. Kill the entire child group on timeout."""
    with subprocess.Popen(command, cwd=cwd, text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                          pass_fds=pass_fds) as child:
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
def _staged_output_directory(output_dir: str) -> Iterator[tuple[Path, Path]]:
    """Build an output privately, then publish the complete directory without replacement."""
    destination = _project_relative(output_dir)[0]
    with _open_project_parent(str(destination)) as (_, parent_fd, target_name):
        if _stat_at(parent_fd, target_name, allow_missing=True) is not None:
            raise ToolError(f"Output already exists: {destination}; choose a new output directory")
        with tempfile.TemporaryDirectory(prefix="kicad-output-") as temporary:
            staged_output = Path(temporary) / target_name
            staged_output.mkdir()
            yield staged_output, destination
            _publish_staged_directory_at(staged_output, parent_fd, target_name)


def _copy_project_file(source: Path, destination: Path) -> str:
    """Copy one project file through a no-follow descriptor and return its fingerprint."""
    digest = hashlib.sha256()
    with _open_project_file(str(source)) as (_, input_stream, before), destination.open("xb") as output_stream:
        for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
            output_stream.write(chunk)
            digest.update(chunk)
        after = os.fstat(input_stream.fileno())
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ToolError("The source file changed while it was being staged")
    return digest.hexdigest()


@contextmanager
def _job(pcb_path: str, output_dir: str) -> Iterator[tuple[Path, Path, Path, Path]]:
    source = _path(pcb_path, ".kicad_pcb")
    with _staged_output_directory(output_dir) as (staged_output, destination):
        with tempfile.TemporaryDirectory(prefix="kicad-job-") as temporary:
            workspace = Path(temporary)
            board = workspace / source.name
            original = _copy_project_file(source, board)
            project = source.with_suffix(".kicad_pro")
            if _symlink_free(project, allow_missing_final=True) is not None:
                _copy_project_file(project, board.with_suffix(".kicad_pro"))
            rules = source.with_suffix(".kicad_dru")
            if _symlink_free(rules, allow_missing_final=True) is not None:
                _copy_project_file(rules, board.with_suffix(".kicad_dru"))
            for table in _project_tables(source):
                _copy_project_file(table, workspace / table.name)
            yield board, staged_output, workspace, destination
            with _open_project_file(str(source)) as (_, current, _):
                current_digest = hashlib.sha256()
                for chunk in iter(lambda: current.read(1024 * 1024), b""):
                    current_digest.update(chunk)
            if current_digest.hexdigest() != original:
                raise ToolError("The source board changed concurrently; the result was not accepted")


@contextmanager
def _artifact_job(input_path: str, output_dir: str, suffix: str) -> Iterator[tuple[Path, Path, Path, Path]]:
    """Stage one non-board input while retaining the output and source guards used by board jobs."""
    source = _path(input_path, suffix)
    with _staged_output_directory(output_dir) as (staged_output, destination):
        with tempfile.TemporaryDirectory(prefix="kicad-artifact-") as temporary:
            workspace = Path(temporary)
            staged = workspace / source.name
            original = _copy_project_file(source, staged)
            yield staged, staged_output, workspace, destination
            with _open_project_file(str(source)) as (_, current, _):
                current_digest = hashlib.sha256()
                for chunk in iter(lambda: current.read(1024 * 1024), b""):
                    current_digest.update(chunk)
            if current_digest.hexdigest() != original:
                raise ToolError("The source file changed concurrently; the result was not accepted")


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
    with _artifact_job(netlist_path, output_dir, ".net") as (netlist, destination, work, display_destination):
        _execute(["netlist_to_skidl", "--input", str(netlist), "--output", str(destination),
                  "--overwrite", "--nobackup"], work)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


@mcp.tool(annotations=_EXPORT)
def generate_offline_cost_spreadsheet(bom_xml_path: str, output_dir: str,
                                      board_quantity: Annotated[int, Field(ge=1, le=1000000)] = 1,
                                      currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "USD") -> dict[str, JsonValue]:
    """Create a KiCost workbook from a KiCad XML BOM with every supplier API disabled."""
    with _artifact_job(bom_xml_path, output_dir, ".xml") as (bom, destination, work, display_destination):
        output = destination / "cost.xlsx"
        _execute(["kicost", "--input", str(bom), "--output", str(output), "--overwrite",
                  "--no_price", "--board_qty", str(board_quantity), "--currency", currency,
                  "--quiet"], work, timeout=600)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        for path in schematics:
            target = work / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        config = work / "fabrication.kibot.yaml"
        config.write_text(json.dumps({"kibot": {"version": 1}, "outputs": [definitions[key] for key in selected]}))
        _execute(["kibot", "-A", "-b", str(board), "-c", str(config), "-d", str(destination)], work, timeout=600)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


@mcp.tool(annotations=_EXPORT)
def generate_interactive_bom(pcb_path: str, output_dir: str, include_tracks: bool = True,
                             include_nets: bool = True, dark_mode: bool = False,
                             layer_view: Literal["F", "FB", "B"] = "FB") -> dict[str, JsonValue]:
    """Generate a self-contained InteractiveHtmlBom assembly HTML using the pinned plugin engine.

    Runs without a GUI/browser, preserves the source and requires a new output directory.
    """
    if layer_view not in {"F", "FB", "B"}:
        raise ToolError("layer_view must be F, FB or B")
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
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
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        _execute(["kikit", "panelize", "--layout", layout, "--tabs", tabs,
                  "--cuts", cuts_section, "--framing", framing_section,
                  str(board), str(destination / "panel.kicad_pcb")], work, timeout=600)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        _execute(["kikit", "separate", "--source", source, str(board),
                  str(destination / "separated.kicad_pcb")], work, timeout=600)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


@mcp.tool(annotations=_EXPORT)
def create_stencil(pcb_path: str, output_dir: str,
                   kind: Literal["steel", "printed"] = "steel") -> dict[str, JsonValue]:
    """Create typed KiKit steel or 3D-printable stencil artifacts without uploaded presets."""
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        command = ["kikit", "stencil", "create" if kind == "steel" else "createprinted"]
        _execute(command + [str(board), str(destination)], work, timeout=900)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


@mcp.tool(annotations=_EXPORT)
def present_pcb(pcb_path: str, output_dir: str, name: Annotated[str, Field(min_length=1, max_length=120)],
                description: Annotated[str, Field(max_length=20000)] = "") -> dict[str, JsonValue]:
    """Create a self-contained KiKit board presentation page from typed text and a staged board."""
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        description_path = work / "description.md"
        description_path.write_text(description)
        _execute(["kikit", "present", "boardpage", "--name", name,
                  "--description", str(description_path), "--board", name, "", str(board),
                  str(destination)], work, timeout=900)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        output = destination / ("board." + format)
        if mode == "stylized":
            command = ["pcbdraw", "plot", "--side", side, "--dpi", str(dpi),
                       "--components" if components else "--no-components",
                       "--copper" if copper else "--no-copper", str(board), str(output)]
        else:
            command = ["pcbdraw", "render", "--side", side, "--renderer", "normal",
                       "--projection", "orthographic", "--transparent", str(board), str(output)]
        _execute(command, work, timeout=600)
        artifacts = _result(destination)
    return _restore_display_paths(artifacts, {str(destination): str(display_destination)})


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


def _sheet_file_references(root_node) -> Iterator[str]:
    for sheet in root_node.find_all("sheet"):
        for prop in sheet.find_all("property"):
            atoms = prop.atoms
            if len(atoms) >= 3 and atoms[1].text in {"Sheetfile", "Sheet file"}:
                yield atoms[2].text


def _embedded_project_path(parent: Path, reference: str, kind: str) -> Path:
    normalized = reference.replace("\\", "/")
    parts = normalized.split("/")
    if (not normalized or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)):
        raise ToolError(f"Embedded {kind} must be a relative path without traversal: {reference}")
    child = parent.joinpath(*parts)
    _project_relative(str(child))
    return child


def _project_top_level_schematics(project: Path) -> list[Path]:
    """Validate existing KiCad 10 top-level schematic references in one project JSON."""
    with _open_project_file(str(project)) as (_, source, before):
        if before.st_size > MAX_PROJECT_FILE_BYTES:
            raise ToolError("KiCad project JSON is too large to validate safely")
        try:
            document = json.load(source)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []  # KiCad's hierarchy resolver also ignores invalid project JSON
    try:
        listed = document["schematic"]["top_level_sheets"]
    except (KeyError, TypeError):
        return []
    if not isinstance(listed, list):
        return []
    found = []
    for entry in listed:
        reference = entry.get("filename") if isinstance(entry, dict) else None
        if not isinstance(reference, str) or not reference:
            continue
        schematic = _embedded_project_path(project.parent, reference, "top-level schematic path")
        schematic_stat = _symlink_free(schematic, allow_missing_final=True)
        if schematic_stat is None:
            continue
        if not stat.S_ISREG(schematic_stat.st_mode):
            raise ToolError(f"Top-level schematic must be a regular file: {schematic}")
        found.append(schematic)
    return found


def _discovered_project_schematics(schematic: Path) -> list[Path]:
    """Mirror upstream's nearest-project search, but stop at the /projects trust boundary."""
    found = []
    directory = schematic.parent
    while True:
        project_files = sorted(directory.glob("*.kicad_pro"))
        for project in project_files:
            project_stat = _symlink_free(project)
            if project_stat is None or not stat.S_ISREG(project_stat.st_mode):
                raise ToolError(f"Project metadata must be a regular file: {project}")
        if len(project_files) == 1:
            root = project_files[0].with_suffix(".kicad_sch")
            root_stat = _symlink_free(root, allow_missing_final=True)
            if root_stat is not None:
                if not stat.S_ISREG(root_stat.st_mode):
                    raise ToolError(f"Project root schematic must be a regular file: {root}")
                found.append(root)
                found.extend(_project_top_level_schematics(project_files[0]))
        if directory == PROJECT_ROOT:
            break
        directory = directory.parent
    return found


def _schematic_hierarchy(
    root: Path, *, boundary: Path | None = None, allow_missing_root: bool = False,
) -> list[Path]:
    """Parse and bound every existing Sheetfile below one schematic."""
    root, _ = _project_relative(str(root))
    root_stat = _symlink_free(root, allow_missing_final=allow_missing_root)
    if root_stat is None:
        return []
    if not stat.S_ISREG(root_stat.st_mode):
        raise ToolError("Schematic hierarchy roots must be regular files")
    pending = [root]
    found: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in found:
            continue
        if len(found) >= 4096:
            raise ToolError("Schematic hierarchy contains more than 4096 files")
        found.add(path)
        with _open_project_file(str(path)) as (_, source, _):
            try:
                tree = _cst.parse(source.read())
            except Exception as exc:
                raise ToolError(f"Invalid KiCad schematic in hierarchy: {path}") from exc
        root_node = tree.find("kicad_sch")
        if root_node is None:
            raise ToolError(f"Expected a native KiCad schematic: {path}")
        for reference in _sheet_file_references(root_node):
            child = _embedded_project_path(path.parent, reference, "sheet file")
            if boundary is not None and not child.is_relative_to(boundary):
                raise ToolError("Schematic hierarchy must stay within the source project directory")
            child_stat = _symlink_free(child, allow_missing_final=True)
            if child_stat is None:
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise ToolError(f"Embedded sheet file must be a regular file: {child}")
            pending.append(child)
    return sorted(found)


def _validate_schematic_arguments(arguments: dict[str, object]) -> None:
    """Apply the /projects boundary to paths embedded inside schematic documents."""
    roots: set[Path] = set()
    for parameter in ("schematic_path", "parent_schematic_path"):
        value = arguments.get(parameter)
        if isinstance(value, str) and value:
            roots.add(_project_relative(value)[0])
    project_value = arguments.get("project_path")
    if isinstance(project_value, str) and project_value:
        project = _project_relative(project_value)[0]
        root = project.with_suffix(".kicad_sch")
        if _symlink_free(root, allow_missing_final=True) is not None:
            roots.add(root)
        roots.update(_project_top_level_schematics(project))
    for root in list(roots):
        roots.update(_discovered_project_schematics(root))
    for root in roots:
        _schematic_hierarchy(root)


def _schematics(board: Path) -> list[Path]:
    """Copy only the referenced hierarchy, not unrelated or previous output trees."""
    return _schematic_hierarchy(
        board.with_suffix(".kicad_sch"), boundary=board.parent, allow_missing_root=True,
    )


def _native_board(operation: str, pcb_path: str, output_dir: str, **parameters: object) -> dict[str, JsonValue]:
    source = _path(pcb_path, ".kicad_pcb")
    # Enumerate before creating the staging tree; never discover our own copies.
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
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
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


def _ipc_board(operation: str, pcb_path: str, output_dir: str, **parameters: object) -> dict[str, JsonValue]:
    source = _path(pcb_path, ".kicad_pcb")
    schematics = _schematics(source)
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
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
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


@contextmanager
def _project_copy(project_dir: str, output_dir: str) -> Iterator[tuple[Path, Path, Path]]:
    source = _project_relative(project_dir)[0]
    destination = _project_relative(output_dir)[0]
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ToolError("Source project and output directory must not contain each other")
    with _open_project_directory(str(source)) as (_, source_fd):
        stable_source = Path(f"/proc/self/fd/{source_fd}")
        before = _tree_fingerprint(stable_source)
        with _staged_output_directory(output_dir) as (staged_output, display_destination):
            shutil.copytree(stable_source, staged_output, copy_function=shutil.copyfile,
                            symlinks=False, dirs_exist_ok=True)
            if (_tree_fingerprint(stable_source) != before
                    or _tree_fingerprint(staged_output) != before):
                raise ToolError("The source project changed while its private copy was created")
            yield source, staged_output, display_destination
            if _tree_fingerprint(stable_source) != before:
                raise ToolError("The source project changed concurrently; the result was not accepted")


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
    project = _project_relative(project_dir)[0]
    destination = _project_relative(output_dir)[0]
    if destination.is_relative_to(project) or project.is_relative_to(destination):
        raise ToolError("Input project and output directory must not overlap")
    with _open_project_directory(str(project)) as (_, project_fd):
        stable_project = Path(f"/proc/self/fd/{project_fd}")
        before = _tree_fingerprint(stable_project)
        with _staged_output_directory(output_dir) as (staged_output, display_destination):
            with tempfile.TemporaryDirectory(prefix="kicad-localize-") as temporary:
                staged_project = Path(temporary) / project.name
                shutil.copytree(stable_project, staged_project, copy_function=shutil.copyfile, symlinks=False)
                if (_tree_fingerprint(stable_project) != before
                        or _tree_fingerprint(staged_project) != before):
                    raise ToolError("The source project changed while its private copy was created")
                result = _native(
                    "dependency_localize", staged_project,
                    input_project_dir=str(staged_project),
                    output_project_dir=str(staged_output / project.name),
                    include_standard=include_standard, selection_keys=selection_keys,
                    archive_path=(
                        str(staged_output / (project.name + ".zip")) if create_archive else None
                    ),
                )
                if _tree_fingerprint(stable_project) != before:
                    raise ToolError("The source project changed concurrently; the result was not accepted")
                response = {**_result(staged_output), "operation": result}
    return _restore_display_paths(response, {str(staged_output): str(display_destination)})


@mcp.tool(annotations=_EXPORT)
def export_testpoints(pcb_path: str, output_dir: str,
                      pads: Annotated[list[TestPointPad], Field(min_length=1, max_length=10000)] | None = None,
                      use_aux_origin: bool = False) -> dict[str, JsonValue]:
    """Export the pinned KiCad Test Points CSV from explicit ref/pad pairs or test-point pad properties.

    Omitting pads selects pads with KiCad's test-point property. Coordinates match Gerber
    Cartesian orientation; use_aux_origin applies the board auxiliary origin. Source is preserved.
    """
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        result = _native("testpoints_export", work, input_pcb=str(board),
                         output_file=str(destination / "testpoints.csv"),
                         pads=None if pads is None else [item.model_dump() for item in pads],
                         use_aux_origin=use_aux_origin)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        result = _native("pinout_export", work, input_pcb=str(board),
                         output_file=str(destination / ("pinout" + suffix)),
                         references=references, format=format, pin_name_filter=pin_name_filter)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        for path in schematics:
            target = work / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        result = _native("prefab_gate", work, input_pcb=str(board), command=command,
                         strict=strict, no_ampacity=no_ampacity, no_parity=no_parity,
                         output_dir=str(destination / "fab") if command == "package" else None)
        (destination / "prefab-gate.json").write_text(json.dumps(result["report"], indent=2, sort_keys=True))
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _job(pcb_path, output_dir) as (board, destination, work, display_destination):
        result = _native("pinout_image_export", work, input_pcb=str(board),
                         output_svg=str(destination / "pinout.svg"), references=references,
                         export_png=export_png, png_dpi=png_dpi,
                         label_orientation=label_orientation)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _staged_output_directory(output_dir) as (destination, display_destination):
        result = _native("kibuzzard_generate", destination,
                         output_file=str(destination / "kibuzzard.kicad_mod"), text=text,
                         height_mm=height_mm, width_mm=width_mm, font=font, layer=layer,
                         alignment=alignment, left_cap=left_cap, right_cap=right_cap,
                         padding_mm=padding_mm, line_spacing=line_spacing,
                         inline_format=inline_format)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _staged_output_directory(output_dir) as (destination, display_destination):
        result = _native("footprint_create", destination,
                         output_footprint=str(destination / f"{name}.kicad_mod"), name=name,
                         pads=[pad.model_dump() for pad in pads], description=description,
                         keywords=keywords, allow_missing_courtyard=allow_missing_courtyard,
                         allow_solder_mask_bridges=allow_solder_mask_bridges)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    output_name = name or source.stem
    with _artifact_job(footprint_path, output_dir, ".kicad_mod") as (
        staged_source, destination, _, display_destination,
    ):
        result = _native("footprint_edit", destination, input_footprint=str(staged_source),
                         output_footprint=str(destination / f"{output_name}.kicad_mod"),
                         name=output_name, operations=[operation.model_dump() for operation in operations],
                         description=description, keywords=keywords,
                         allow_missing_courtyard=allow_missing_courtyard,
                         allow_solder_mask_bridges=allow_solder_mask_bridges)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


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
    with _project_copy(project_dir, output_dir) as (source, destination, display_destination):
        result = _native("kinotes_manage", destination, project_dir=str(destination),
                         action=action, notes=notes, todos=todos, version_log=version_log)
        (destination / "kinotes-result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        result["project_dir"] = str(source)
        response = {**_result(destination), "operation": result}
    return _restore_display_paths(response, {str(destination): str(display_destination)})


_TRUSTED_LIBRARY_ROOTS = tuple(Path(value) for value in (
    "/usr/share/kicad", "/opt/kicad-addons", "/config/.local/share/kicad",
))
_EXTERNAL_READ_PARAMETERS = {
    ("list_lib_symbols", "symbol_lib_path"), ("get_symbol_info", "symbol_lib_path"),
    ("export_symbol_svg", "symbol_lib_path"), ("list_lib_footprints", "pretty_dir"),
    ("get_footprint_info", "footprint_path"), ("export_footprint_svg", "footprint_path"),
    ("place_component", "symbol_lib_path"), ("add_lib_symbol", "symbol_lib_path"),
    ("add_power_symbol", "symbol_lib_path"), ("auto_place_decoupling_cap", "symbol_lib_path"),
    ("place_footprint", "library"),
}
_PATH_PARAMETER_NAMES = {"directory", "library", "pretty_dir", "output", "sheet_file", "file_name", "new_file_name"}
_CREATION_TOOLS = {"create_project", "create_schematic", "create_symbol_library"}
_UPSTREAM_OUTPUT_DIRECTORIES = {
    "autoroute_pcb": "output_dir",
    "export_3d": "output_dir",
    "export_bom": "output_dir",
    "export_footprint_svg": "output_dir",
    "export_gerbers": "output_dir",
    "export_hierarchical_netlist": "output_dir",
    "export_netlist": "output_dir",
    "export_pcb": "output_dir",
    "export_positions": "output_dir",
    "export_schematic": "output_dir",
    "export_symbol_svg": "output_dir",
    "list_unconnected_pins": "output_dir",
    "run_drc": "output_dir",
    "run_erc": "output_dir",
}
_UPSTREAM_OUTPUT_FILES = {"export_ipc2581": "output"}


class CreatePcbPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["create"]


class UpdatePcbPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["update"]
    expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


PcbPublication = Annotated[
    CreatePcbPublication | UpdatePcbPublication,
    Field(discriminator="mode"),
]


_MUTATION_TARGETS = {
    "place_component": "schematic_path", "remove_component": "schematic_path",
    "remove_label": "schematic_path", "remove_wire": "schematic_path", "remove_junction": "schematic_path",
    "add_wires": "schematic_path", "add_label": "schematic_path", "add_junctions": "schematic_path",
    "add_lib_symbol": "schematic_path", "move_component": "schematic_path",
    "set_component_property": "schematic_path", "set_page_size": "schematic_path",
    "add_global_label": "schematic_path", "add_hierarchical_label": "schematic_path",
    "remove_hierarchical_label": "schematic_path", "modify_hierarchical_label": "schematic_path",
    "add_power_symbol": "schematic_path", "auto_place_decoupling_cap": "schematic_path",
    "add_text": "schematic_path", "remove_text": "schematic_path", "wire_pins_to_net": "schematic_path",
    "connect_pins": "schematic_path", "no_connect_pin": "schematic_path", "remove_no_connect": "schematic_path",
    "place_footprint": "pcb_path", "move_footprint": "pcb_path", "remove_footprint": "pcb_path",
    "add_trace": "pcb_path", "add_via": "pcb_path", "add_pcb_text": "pcb_path", "add_pcb_line": "pcb_path",
    "add_copper_zone": "pcb_path", "add_keepout_zone": "pcb_path", "fill_zones": "pcb_path",
    "update_pcb_from_schematic": "pcb_path", "set_trace_width": "pcb_path", "remove_traces": "pcb_path",
    "add_thermal_vias": "pcb_path", "set_net_class": "pcb_path", "remove_dangling_tracks": "pcb_path",
    "add_symbol": "symbol_lib_path", "upgrade_symbol_lib": "symbol_lib_path",
    "upgrade_footprint_lib": "footprint_path", "add_hierarchical_sheet": "parent_schematic_path",
    "remove_hierarchical_sheet": "parent_schematic_path", "modify_hierarchical_sheet": "schematic_path",
    "add_sheet_pin": "schematic_path", "remove_sheet_pin": "schematic_path",
    "annotate_schematic": "schematic_path", "move_hierarchical_sheet": "schematic_path",
    "reorder_sheet_pages": "schematic_path", "duplicate_sheet": "schematic_path",
    "create_sym_lib_table": "directory", "flatten_hierarchy": "schematic_path",
    "run_jobset": "jobset_path",
}


@contextmanager
def _open_project_creation_directory(value: str) -> Iterator[tuple[Path, int]]:
    """Pin an existing project directory or create and pin its missing final component."""
    path, relative = _project_relative(value, allow_root=True)
    if not relative.parts:
        with _open_project_directory(str(path)) as (_, directory_fd):
            yield path, directory_fd
        return
    with _open_project_parent(str(path)) as (_, parent_fd, name):
        current = _stat_at(parent_fd, name, allow_missing=True)
        created = current is None
        if created:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        elif not stat.S_ISDIR(current.st_mode):
            raise ToolError("Project directory must be a directory")
        directory_fd = -1
        try:
            directory_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            yield path, directory_fd
        except BaseException:
            if created:
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
            raise
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)


def _safe_project_name(value: object) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or Path(value).name != value:
        raise ToolError("name must be one non-empty project filename stem without path separators")
    if value.endswith((".kicad_pro", ".kicad_sch")):
        raise ToolError("name must be a project stem without a KiCad filename suffix")
    return value


def _replace_project_files_at(
    parent_fd: int, updates: list[tuple[str, bytes, str]],
) -> None:
    """CAS-check, stage and replace one cooperating multi-file project mutation."""
    temporaries: dict[str, str] = {}
    backups: dict[str, str] = {}
    try:
        for name, content, expected_sha256 in updates:
            _require_file_sha256_at(parent_fd, name, expected_sha256)
            temporaries[name] = _write_temp_at(parent_fd, name, content, "multi", suffix=Path(name).suffix)
        for name, _, expected_sha256 in updates:
            _require_file_sha256_at(parent_fd, name, expected_sha256)
            backup = f".{name}.backup-{uuid.uuid4().hex}"
            os.link(name, backup, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            backups[name] = backup
        for name, _, _ in updates:
            os.replace(temporaries[name], name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporaries.pop(name)
        os.fsync(parent_fd)
    except BaseException:
        for name, backup in backups.items():
            try:
                os.replace(backup, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary in temporaries.values():
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        for backup in backups.values():
            try:
                os.unlink(backup, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _run_staged_hierarchical_sheet(
    original, arguments: dict[str, object], expected_parent_sha256: str,
    expected_sheet_sha256: str, expected_root_sha256: str | None,
):
    parent = _project_relative(str(arguments["parent_schematic_path"]))[0]
    sheet_file = arguments.get("sheet_file")
    if not isinstance(sheet_file, str) or Path(sheet_file).name != sheet_file:
        raise ToolError("sheet_file must be one child schematic filename beside the parent")
    child = parent.parent / sheet_file
    if child.suffix != ".kicad_sch":
        raise ToolError("sheet_file must end with .kicad_sch")
    if child == parent:
        raise ToolError("sheet_file must name a different child schematic")
    project_value = arguments.get("project_path")
    project = _project_relative(str(project_value))[0] if isinstance(project_value, str) and project_value else None
    if project is not None and project.parent != parent.parent:
        raise ToolError("project_path must be beside the parent schematic")
    root_schematic = project.with_suffix(".kicad_sch") if project is not None else None
    expected_by_name = {parent.name: expected_parent_sha256, child.name: expected_sheet_sha256}
    if root_schematic is not None and root_schematic.name not in expected_by_name:
        if expected_root_sha256 is None:
            raise ToolError("expected_root_schematic_sha256 is required when project_path names another root schematic")
        expected_by_name[root_schematic.name] = expected_root_sha256
    elif expected_root_sha256 is not None:
        raise ToolError("expected_root_schematic_sha256 is only accepted for a distinct root schematic")

    with _open_project_directory(str(parent.parent)) as (_, directory_fd):
        stable_directory = Path(f"/proc/self/fd/{directory_fd}")
        for name, expected in expected_by_name.items():
            _require_file_sha256_at(directory_fd, name, expected)
        before = _tree_fingerprint(stable_directory)
        with tempfile.TemporaryDirectory(prefix="kicad-sheet-project-") as staging_value:
            staged_project = Path(staging_value) / "project"
            shutil.copytree(stable_directory, staged_project, symlinks=False)
            if _tree_fingerprint(staged_project) != before or _tree_fingerprint(stable_directory) != before:
                raise ToolError("Project directory changed while it was staged")
            staged_arguments = dict(arguments)
            staged_arguments["parent_schematic_path"] = str(staged_project / parent.name)
            staged_arguments["sheet_file"] = str(staged_project / child.name)
            if project is not None:
                staged_arguments["project_path"] = str(staged_project / project.name)
            result = original(**staged_arguments)
            if _tree_fingerprint(stable_directory) != before:
                raise ToolError("Project directory changed while the hierarchical edit was running")
            updates = [
                (name, (staged_project / name).read_bytes(), expected)
                for name, expected in expected_by_name.items()
            ]
            _replace_project_files_at(directory_fd, updates)
            replacements = {
                str(staged_project / parent.name): str(parent),
                str(staged_project / child.name): str(child),
            }
            if project is not None:
                replacements[str(staged_project / project.name)] = str(project)
            return _restore_display_paths(result, replacements)


def _validate_jobset_paths(jobset_path: str) -> None:
    _, content = _read_project_file_unlocked(jobset_path)
    try:
        document = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ToolError(f"Invalid KiCad jobset JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list) or not isinstance(document.get("outputs"), list):
        raise ToolError("A jobset must contain jobs and outputs arrays")
    path_key = re.compile(r"(?:^|_)(?:path|file|filename|directory|dir|output)(?:$|_)", re.IGNORECASE)
    forbidden_key = re.compile(r"(?:^|_)(?:command|executable|script|hook)(?:$|_)", re.IGNORECASE)
    jobset_directory = _project_relative(jobset_path)[0].parent

    def validate_path(key: str, value: str) -> None:
        normalized = value.replace("\\", "/")
        if normalized.startswith("${KIPRJMOD}/"):
            normalized = normalized.removeprefix("${KIPRJMOD}/")
        normalized = normalized.removeprefix("./")
        candidate = Path(normalized)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ToolError(f"Jobset field {key} must stay relative to its /projects directory")
        absolute = jobset_directory / candidate
        _project_relative(str(absolute))
        current = absolute
        while not current.exists() and current != jobset_directory:
            current = current.parent
        current_stat = _symlink_free(current)
        if current_stat is None or not (
            stat.S_ISDIR(current_stat.st_mode) or (current == absolute and stat.S_ISREG(current_stat.st_mode))
        ):
            raise ToolError(f"Jobset field {key} must stay below an existing project directory")

    def walk(value: object, key: str = "") -> None:
        if forbidden_key.search(key) and value is not None and value != "" and value is not False:
            raise ToolError(f"Jobset field {key} is not allowed through MCP")
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and value and path_key.search(key):
            validate_path(key, value)

    walk(document)


def _is_path_parameter(name: str) -> bool:
    return name in _PATH_PARAMETER_NAMES or name.endswith("_path") or name.endswith("_dir")


def _validate_trusted_library_path(path: Path) -> None:
    if not path.is_absolute():
        raise ToolError("Library paths must be absolute or use a registered KiCad library nickname")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"Trusted library path does not exist: {path}") from exc
    roots = [root.resolve() for root in _TRUSTED_LIBRARY_ROOTS if root.exists()]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ToolError("External library reads are limited to the pinned KiCad and PCM library roots")


def _validate_registered_paths(tool_name: str, arguments: dict[str, object]) -> None:
    for parameter, raw in arguments.items():
        if not _is_path_parameter(parameter) or not isinstance(raw, str) or not raw:
            continue
        if parameter in {"sheet_file", "file_name", "new_file_name"}:
            relative = Path(raw)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ToolError(f"{parameter} must be a safe relative project path")
            if (parameter == "new_file_name"
                    and (len(relative.parts) != 1 or relative.suffix != ".kicad_sch")):
                raise ToolError("new_file_name must be one .kicad_sch filename without directories")
            continue
        if (tool_name, parameter) in _EXTERNAL_READ_PARAMETERS:
            if parameter == "library" and not Path(raw).is_absolute() and "/" not in raw and "\\" not in raw:
                continue
            _validate_trusted_library_path(Path(raw))
            continue
        path, _ = _project_relative(raw, allow_root=parameter in {"directory", "project_dir", "output_dir"})
        current = _symlink_free(path, allow_missing_final=True)
        if current is None:
            parent = _symlink_free(path.parent)
            if parent is None or not stat.S_ISDIR(parent.st_mode):
                raise ToolError(f"{parameter} parent must be an existing project directory")


def _restore_display_paths(value, replacements: dict[str, str]):
    if isinstance(value, str):
        for stable, display in replacements.items():
            value = value.replace(stable, display)
        return value
    if isinstance(value, BaseModel):
        restored = _restore_display_paths(value.model_dump(), replacements)
        return type(value).model_validate(restored)
    if isinstance(value, list):
        return [_restore_display_paths(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _restore_display_paths(item, replacements) for key, item in value.items()}
    return value


def _guard_upstream_tool(tool_name: str) -> None:
    tool = mcp._tool_manager._tools[tool_name]
    original = tool.fn

    if tool_name in _CREATION_TOOLS:
        def guarded_creation(*, _original=original, _name=tool_name, **arguments):
            _validate_registered_paths(_name, arguments)
            with _project_lock():
                if _name == "create_project":
                    raw_directory = arguments.get("directory")
                    if not isinstance(raw_directory, str) or not raw_directory:
                        raise ToolError("directory must be an explicit absolute path inside /projects")
                    name = _safe_project_name(arguments.get("name"))
                    destination = _project_relative(raw_directory, allow_root=True)[0]
                    with tempfile.TemporaryDirectory(prefix="kicad-create-project-") as staging_value:
                        staging = Path(staging_value)
                        staged_arguments = dict(arguments)
                        staged_arguments["directory"] = str(staging)
                        result = _original(**staged_arguments)
                        staged_files = [staging / f"{name}.kicad_pro", staging / f"{name}.kicad_sch"]
                        with _open_project_creation_directory(str(destination)) as (_, directory_fd):
                            for staged_file in staged_files:
                                if _stat_at(directory_fd, staged_file.name, allow_missing=True) is not None:
                                    raise ToolError(f"Project output already exists: {destination / staged_file.name}")
                            published: list[str] = []
                            try:
                                for staged_file in staged_files:
                                    _publish_staged_file_at(staged_file, directory_fd, staged_file.name)
                                    published.append(staged_file.name)
                                os.fsync(directory_fd)
                            except BaseException:
                                for published_name in published:
                                    try:
                                        os.unlink(published_name, dir_fd=directory_fd)
                                    except FileNotFoundError:
                                        pass
                                raise
                        return _restore_display_paths(result, {str(staging): str(destination)})

                parameter = "schematic_path" if _name == "create_schematic" else "symbol_lib_path"
                raw_target = arguments.get(parameter)
                if not isinstance(raw_target, str) or not raw_target:
                    raise ToolError(f"{parameter} must be an explicit absolute path inside /projects")
                target = _project_relative(raw_target)[0]
                expected_suffix = ".kicad_sch" if _name == "create_schematic" else ".kicad_sym"
                if target.suffix != expected_suffix:
                    raise ToolError(f"{parameter} must end with {expected_suffix}")
                with _open_project_parent(str(target)) as (_, parent_fd, target_name):
                    if _stat_at(parent_fd, target_name, allow_missing=True) is not None:
                        raise ToolError(f"Project output already exists: {target}")
                    with tempfile.TemporaryDirectory(prefix=f"kicad-{_name}-") as staging_value:
                        staged_target = Path(staging_value) / target_name
                        staged_arguments = dict(arguments)
                        staged_arguments[parameter] = str(staged_target)
                        result = _original(**staged_arguments)
                        _publish_staged_file_at(staged_target, parent_fd, target_name)
                        os.fsync(parent_fd)
                    return _restore_display_paths(result, {str(staged_target): str(target)})

        tool.fn = guarded_creation
        tool.is_async = False
        return

    output_parameter = _UPSTREAM_OUTPUT_DIRECTORIES.get(tool_name) or _UPSTREAM_OUTPUT_FILES.get(tool_name)
    if output_parameter is not None:
        original_model = tool.fn_metadata.arg_model
        output_annotation = original_model.model_fields[output_parameter].annotation
        guarded_model = create_model(
            f"{tool_name.title().replace('_', '')}GuardedArguments",
            __base__=original_model,
            **{output_parameter: (output_annotation, ...)},
        )
        tool.fn_metadata.arg_model = guarded_model
        tool.parameters = guarded_model.model_json_schema(by_alias=True)
        tool.description = (tool.description or "") + (
            f" The {output_parameter} argument is required and must name a new path inside /projects;"
            " generated files are staged privately and published without replacement."
        )
        directory_output = tool_name in _UPSTREAM_OUTPUT_DIRECTORIES

        def guarded_output(*, _original=original, _name=tool_name, _parameter=output_parameter,
                           _directory=directory_output, **arguments):
            raw_output = arguments.get(_parameter)
            if not isinstance(raw_output, str) or not raw_output:
                raise ToolError(f"{_parameter} must be an explicit new path inside /projects")
            _validate_registered_paths(_name, arguments)
            destination = _project_relative(raw_output)[0]
            with _project_lock(), _open_project_parent(str(destination)) as (_, parent_fd, target_name):
                _validate_schematic_arguments(arguments)
                if _stat_at(parent_fd, target_name, allow_missing=True) is not None:
                    raise ToolError(f"Output already exists: {destination}; choose a new output path")
                with tempfile.TemporaryDirectory(prefix=f"kicad-{_name}-") as staging_value:
                    staging = Path(staging_value)
                    staged_output = staging / ("output" if _directory else target_name)
                    staged_arguments = dict(arguments)
                    staged_arguments[_parameter] = str(staged_output)
                    result = _original(**staged_arguments)
                    if _name == "autoroute_pcb":
                        drc_violations = (result.get("drc_violations") if isinstance(result, dict)
                                          else getattr(result, "drc_violations", None))
                        drc_unconnected = (result.get("drc_unconnected") if isinstance(result, dict)
                                           else getattr(result, "drc_unconnected", None))
                        if drc_violations is None or drc_unconnected is None:
                            raise ToolError("Autoroute output was discarded because the post-route DRC did not complete")
                    if _directory:
                        _publish_staged_directory_at(staged_output, parent_fd, target_name)
                    else:
                        _publish_staged_file_at(staged_output, parent_fd, target_name)
                        os.fsync(parent_fd)
                    return _restore_display_paths(result, {str(staged_output): str(destination)})

        tool.fn = guarded_output
        tool.is_async = False
        return

    target_parameter = _MUTATION_TARGETS.get(tool_name)
    if target_parameter is not None:
        original_model = tool.fn_metadata.arg_model
        guard_fields: dict[str, tuple[object, object]] = (
            {"publication": (PcbPublication, ...)}
            if tool_name == "update_pcb_from_schematic"
            else {"expected_sha256": (Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...)}
        )
        if tool_name == "add_hierarchical_sheet":
            guard_fields["expected_sheet_sha256"] = (
                Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...,
            )
            guard_fields["expected_root_schematic_sha256"] = (
                Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")], None,
            )
        guarded_model = create_model(
            f"{tool_name.title().replace('_', '')}GuardedArguments",
            __base__=original_model,
            **guard_fields,
        )
        tool.fn_metadata.arg_model = guarded_model
        tool.parameters = guarded_model.model_json_schema(by_alias=True)
        if tool_name == "update_pcb_from_schematic":
            tool.description = (tool.description or "") + (
                " publication is required: mode=create stages and publishes a missing PCB without"
                " replacement; mode=update requires expected_sha256 for an existing PCB."
            )

        def guarded_mutation(*, _original=original, _name=tool_name, _target=target_parameter, **arguments):
            expected_sha256 = arguments.pop("expected_sha256", None)
            publication = arguments.pop("publication", None)
            expected_sheet_sha256 = arguments.pop("expected_sheet_sha256", None)
            expected_root_sha256 = arguments.pop("expected_root_schematic_sha256", None)
            _validate_registered_paths(_name, arguments)
            raw_target = arguments.get(_target)
            if not isinstance(raw_target, str) or not raw_target:
                raise ToolError(f"{_target} must be an explicit absolute path inside /projects")
            target = _project_relative(raw_target, allow_root=_name == "create_sym_lib_table")[0]
            if _name == "set_net_class":
                target = target.with_suffix(".kicad_pro")
            if _name == "remove_hierarchical_sheet" and arguments.get("delete_child_file") is True:
                raise ToolError("delete_child_file is not accepted; delete the guarded child with delete_project_path")
            if _name == "create_sym_lib_table":
                for entry in arguments.get("entries", []):
                    uri = entry.get("uri") if isinstance(entry, dict) else getattr(entry, "uri", None)
                    if not isinstance(uri, str):
                        raise ToolError("Every symbol library entry must contain one URI")
                    _validate_library_uri(uri)
            elif _name == "flatten_hierarchy":
                output = arguments.get("output_path")
                if not isinstance(output, str) or not output:
                    raise ToolError("output_path must be an explicit new .kicad_sch path inside /projects")
                output_path = _project_relative(output)[0]
                if output_path.suffix != ".kicad_sch":
                    raise ToolError("output_path must end with .kicad_sch")
                if _symlink_free(output_path, allow_missing_final=True) is not None:
                    raise ToolError("flatten_hierarchy requires a new output_path")
            elif _name == "run_jobset":
                target = target.parent
            with _project_lock():
                _validate_schematic_arguments(arguments)
                if _name == "update_pcb_from_schematic":
                    if target.suffix != ".kicad_pcb":
                        raise ToolError("pcb_path must end with .kicad_pcb")
                    try:
                        if isinstance(publication, dict) and publication.get("mode") == "create":
                            publication = CreatePcbPublication.model_validate(publication)
                        elif isinstance(publication, dict) and publication.get("mode") == "update":
                            publication = UpdatePcbPublication.model_validate(publication)
                    except ValueError as exc:
                        raise ToolError(f"Invalid PCB publication contract: {exc}") from exc
                    if not isinstance(publication, (CreatePcbPublication, UpdatePcbPublication)):
                        raise ToolError("publication must explicitly select mode=create or mode=update")
                    with _open_project_parent(str(target)) as (_, target_parent_fd, target_name):
                        current = _stat_at(target_parent_fd, target_name, allow_missing=True)
                        if isinstance(publication, CreatePcbPublication):
                            if current is not None:
                                raise ToolError("mode=create requires a missing pcb_path")
                            with tempfile.TemporaryDirectory(prefix="kicad-create-pcb-") as staging_value:
                                staged_target = Path(staging_value) / target_name
                                staged_arguments = dict(arguments)
                                staged_arguments[_target] = str(staged_target)
                                result = _original(**staged_arguments)
                                _publish_staged_file_at(staged_target, target_parent_fd, target_name)
                                os.fsync(target_parent_fd)
                            return _restore_display_paths(result, {str(staged_target): str(target)})
                        if current is None:
                            raise ToolError("mode=update requires an existing pcb_path")
                        _require_fingerprint_at(
                            target_parent_fd, target_name, publication.expected_sha256,
                        )
                        stable_target = Path(f"/proc/self/fd/{target_parent_fd}") / target_name
                        stable_arguments = dict(arguments)
                        stable_arguments[_target] = str(stable_target)
                        result = _original(**stable_arguments)
                        return _restore_display_paths(result, {str(stable_target): str(target)})
                if _name == "add_hierarchical_sheet":
                    if not isinstance(expected_sheet_sha256, str):
                        raise ToolError("expected_sheet_sha256 is required")
                    return _run_staged_hierarchical_sheet(
                        _original, arguments, expected_sha256,
                        expected_sheet_sha256, expected_root_sha256,
                    )
                if _name in {"create_sym_lib_table", "run_jobset"}:
                    if _name == "run_jobset":
                        _validate_jobset_paths(raw_target)
                    with _open_project_directory(str(target)) as (_, target_fd):
                        stable_directory = Path(f"/proc/self/fd/{target_fd}")
                        if _tree_fingerprint(stable_directory) != expected_sha256:
                            raise ToolError("Project directory changed")
                        stable_arguments = dict(arguments)
                        if _name == "create_sym_lib_table":
                            rows = upstream.project._lib_table_rows(stable_arguments["entries"])
                            tree = _cst.parse(upstream.project._SYM_LIB_TABLE_TPL)
                            table = tree.lists[0]
                            row_template = table.find("lib")
                            table.remove_child(row_template)
                            anchor = table.find("version")
                            for name, uri in rows:
                                row = row_template.copy()
                                row.find("name").atoms[1].set_text(name)
                                row.find("uri").atoms[1].set_text(uri)
                                table.insert_after(anchor, row)
                                anchor = row
                            table_name = "sym-lib-table"
                            current = _stat_at(target_fd, table_name, allow_missing=True)
                            if current is not None and not stat.S_ISREG(current.st_mode):
                                raise ToolError("sym-lib-table must be a regular file")
                            temporary = _write_temp_at(target_fd, table_name, _cst.serialize(tree), "table")
                            try:
                                if current is None:
                                    os.link(temporary, table_name, src_dir_fd=target_fd,
                                            dst_dir_fd=target_fd, follow_symlinks=False)
                                else:
                                    os.replace(temporary, table_name, src_dir_fd=target_fd, dst_dir_fd=target_fd)
                            finally:
                                try:
                                    os.unlink(temporary, dir_fd=target_fd)
                                except FileNotFoundError:
                                    pass
                            return f"Created sym-lib-table with {len(rows)} entries at {target / table_name}"
                        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                            raise ToolError("expected_sha256 must be one lowercase 64-character SHA-256 digest")
                        before_fingerprint = _tree_fingerprint(stable_directory)
                        if before_fingerprint != expected_sha256:
                            raise ToolError("Project directory changed")
                        before_manifest = _regular_tree_manifest(stable_directory)
                        with tempfile.TemporaryDirectory(prefix="kicad-jobset-") as staging_value:
                            staged_project = Path(staging_value) / "project"
                            shutil.copytree(
                                stable_directory,
                                staged_project,
                                symlinks=False,
                                ignore=shutil.ignore_patterns(_PROJECT_LOCK_NAME),
                            )
                            if (_tree_fingerprint(staged_project) != before_fingerprint
                                    or _tree_fingerprint(stable_directory) != before_fingerprint):
                                raise ToolError("Project directory changed while it was staged")
                            stable_arguments["jobset_path"] = str(staged_project / Path(raw_target).name)
                            previous_run_cli = upstream.project._run_cli

                            def staged_run_cli(cli_arguments, check=True, cwd=None):
                                executable = shutil.which(os.environ.get("KICAD_CLI_PATH", "kicad-cli"))
                                if executable is None:
                                    raise ToolError("kicad-cli not found")
                                try:
                                    completed = subprocess.run(
                                        [executable, *cli_arguments], capture_output=True, text=True,
                                        errors="replace", timeout=600, cwd=cwd,
                                        env=os.environ.copy(),
                                    )
                                except subprocess.TimeoutExpired as exc:
                                    raise ToolError("kicad-cli jobset did not finish within 600 seconds") from exc
                                if check and completed.returncode:
                                    raise ToolError(completed.stderr.strip() or
                                                    f"kicad-cli exited {completed.returncode}")
                                return completed

                            upstream.project._run_cli = staged_run_cli
                            try:
                                result = _original(**stable_arguments)
                            finally:
                                upstream.project._run_cli = previous_run_cli
                            after_manifest = _regular_tree_manifest(staged_project)
                            if _tree_fingerprint(stable_directory) != before_fingerprint:
                                raise ToolError("Project directory changed while the jobset was running")
                            _publish_jobset_outputs_at(
                                staged_project, target_fd, before_manifest, after_manifest,
                            )
                        return _restore_display_paths(result, {str(staged_project): str(target)})

                with _open_project_parent(str(target)) as (_, target_parent_fd, target_name):
                    _require_fingerprint_at(target_parent_fd, target_name, expected_sha256)
                    stable_target = Path(f"/proc/self/fd/{target_parent_fd}") / target_name
                    stable_arguments = dict(arguments)
                    stable_arguments[_target] = (
                        str(stable_target.with_suffix(".kicad_pcb"))
                        if _name == "set_net_class" else str(stable_target)
                    )
                    if _name == "flatten_hierarchy":
                        output_path = _project_relative(str(arguments["output_path"]))[0]
                        with _open_project_parent(str(output_path)) as (_, output_parent_fd, output_name):
                            if _stat_at(output_parent_fd, output_name, allow_missing=True) is not None:
                                raise ToolError("flatten_hierarchy requires a new output_path")
                            with tempfile.TemporaryDirectory(prefix="kicad-flatten-") as staging_value:
                                staged_output = Path(staging_value) / output_name
                                stable_arguments["output_path"] = str(staged_output)
                                result = _original(**stable_arguments)
                                _publish_staged_file_at(staged_output, output_parent_fd, output_name)
                                os.fsync(output_parent_fd)
                            return _restore_display_paths(result, {
                                str(stable_target): str(target), str(staged_output): str(output_path),
                            })
                    result = _original(**stable_arguments)
                    return _restore_display_paths(result, {str(stable_target): str(target)})

        tool.fn = guarded_mutation
        tool.is_async = False
        return

    def guarded_call(*, _original=original, _name=tool_name, **arguments):
        _validate_registered_paths(_name, arguments)
        with _project_lock():
            _validate_schematic_arguments(arguments)
            return _original(**arguments)

    tool.fn = guarded_call
    tool.is_async = False


def _guard_local_tool(tool_name: str) -> None:
    tool = mcp._tool_manager._tools[tool_name]
    original = tool.fn
    read_only = bool(tool.annotations and tool.annotations.read_only_hint)
    if tool.is_async:
        async def guarded_async(*args, _original=original, _read_only=read_only, **arguments):
            with _project_lock(exclusive=not _read_only):
                return await _original(*args, **arguments)
        tool.fn = guarded_async
    else:
        def guarded_sync(*args, _original=original, _read_only=read_only, **arguments):
            with _project_lock(exclusive=not _read_only):
                return _original(*args, **arguments)
        tool.fn = guarded_sync


# Reuse the pinned upstream composition function, preserving every original tool.
_LOCAL_TOOL_NAMES = set(mcp._tool_manager._tools)
_BOARD_BATCH_TOOL_NAMES = set(board_batch.mcp._tool_manager._tools)
for _module in [
    upstream.schematic,
    upstream.pcb,
    upstream.symbol,
    upstream.footprint,
    upstream.project,
    board_batch,
]:
    upstream._copy_tools(_module.mcp, mcp)

for _name in set().union(*(
    set(_module.mcp._tool_manager._tools) for _module in [
        upstream.schematic, upstream.pcb, upstream.symbol, upstream.footprint, upstream.project,
    ]
)):
    _guard_upstream_tool(_name)

for _name in _LOCAL_TOOL_NAMES | _BOARD_BATCH_TOOL_NAMES:
    _guard_local_tool(_name)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
