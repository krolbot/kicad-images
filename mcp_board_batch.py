"""Bounded, native, two-phase PCB batch editing for the Lab KiCad MCP."""

import fcntl
import ctypes
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from mcp_server_kicad import pcb
from mcp_server_kicad._shared import ToolError, build_server

mcp = build_server(
    "kicad-board-batch",
    instructions="Read board UUID state and run CAS-guarded native dry-run/apply batches.",
)
_LOCK = threading.Lock()
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WORKER = Path("/usr/local/share/kicad/mcp_board_batch_worker.py")
_NATIVE_PYTHON = os.environ.get("KICAD_PYTHON", "/usr/bin/python3")
_AT_FDCWD = -100
_RENAME_EXCHANGE = 2


class FootprintMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=64)
    x_mm: float
    y_mm: float
    rotation_deg: float | None = None
    layer: Literal["F.Cu", "B.Cu"] | None = None


class TraceAdd(BaseModel):
    net_name: str = Field(min_length=1, max_length=255)
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    width_mm: float = Field(gt=0, le=20)
    layer: str = Field(default="F.Cu", min_length=1, max_length=64)


class ViaAdd(BaseModel):
    net_name: str = Field(min_length=1, max_length=255)
    x_mm: float
    y_mm: float
    diameter_mm: float = Field(default=0.6, gt=0, le=20)
    drill_mm: float = Field(default=0.3, gt=0, le=20)
    layers: list[str] = Field(default_factory=lambda: ["F.Cu", "B.Cu"], min_length=2, max_length=2)


class RequiredPad(BaseModel):
    reference: str = Field(min_length=1, max_length=64)
    pad_number: str = Field(min_length=1, max_length=64)
    net_name: str = Field(min_length=1, max_length=255)


class ProtectedPadGroup(BaseModel):
    reference: str = Field(min_length=1, max_length=64)
    pad_numbers: list[str] = Field(default_factory=list, max_length=256)


class NetWidth(BaseModel):
    net_name: str = Field(min_length=1, max_length=255)
    width_mm: float = Field(gt=0, le=20)


def _path(value: str) -> Path:
    path = Path(value)
    if path.suffix != ".kicad_pcb" or not path.is_absolute() or path.is_symlink():
        raise ToolError("pcb_path must be an absolute, non-symlink .kicad_pcb path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"board not found: {path}") from exc
    root = Path(os.environ.get("KICAD_PROJECT_ROOT", "/projects")).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ToolError(f"board must be a regular file below {root}")
    return resolved


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_project(source: Path, destination: Path) -> Path:
    """Copy the complete source-project context around a temporary board."""
    for entry in source.parent.iterdir():
        if entry == destination or entry.name.startswith(".kicad-batch-"):
            continue
        target = destination / entry.name
        if entry.is_symlink():
            target.symlink_to(os.readlink(entry))
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            raise ToolError(f"unsupported project entry: {entry}")
    stage = destination / source.name
    if stage.is_symlink() or not stage.is_file():
        raise ToolError("staged board is not a regular file")
    return stage


def _worker(payload: dict, timeout: int = 180) -> dict:
    try:
        result = subprocess.run(
            [_NATIVE_PYTHON, str(_WORKER)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"native pcbnew worker failed: {exc}") from exc
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip().splitlines()
        diagnostic = "\n".join(error[-20:]) if error else "no diagnostics"
        raise ToolError(f"native pcbnew worker failed:\n{diagnostic}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ToolError("native pcbnew worker returned invalid JSON") from exc


def _drc(path: Path, directory: Path, timeout: int) -> dict:
    report = directory / "drc.json"
    try:
        run = subprocess.run(
            [
                "/usr/bin/kicad-cli",
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--exit-code-violations",
                "--output",
                str(report),
                str(path),
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"native DRC failed: {exc}") from exc
    if not report.is_file():
        diagnostic = (run.stderr or run.stdout).strip().splitlines()
        raise ToolError(f"native DRC produced no report: {diagnostic[-1] if diagnostic else run.returncode}")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError("native DRC report is unreadable") from exc
    violations = data.get("violations", [])
    opens = data.get("unconnected_items", [])
    if run.returncode not in {0, 5}:
        diagnostic = (run.stderr or run.stdout).strip().splitlines()
        raise ToolError(
            f"native DRC failed with exit {run.returncode}: "
            f"{diagnostic[-1] if diagnostic else 'no diagnostics'}"
        )
    if not isinstance(violations, list) or not isinstance(opens, list):
        raise ToolError("native DRC report has invalid violation/open lists")
    return {
        "exit_code": run.returncode,
        "violations": violations,
        "unconnected_items": opens,
        "violation_count": len(violations),
        "unconnected_count": len(opens),
    }


def _pad_map(footprint: dict) -> dict[str, dict]:
    return {pad["number"]: pad for pad in footprint["pads"]}


def _pad_identity(pad: dict) -> dict:
    return {key: value for key, value in pad.items() if key not in {"position_mm", "layers"}}


def _pad_copper_side(pad: dict) -> str | None:
    front, back = "F.Cu" in pad["layers"], "B.Cu" in pad["layers"]
    return "F.Cu" if front and not back else "B.Cu" if back and not front else None


def _all_by_uuid(state: dict) -> dict[str, dict]:
    items = dict(state["copper"]) | dict(state["pads"]) | dict(state["zones"])
    items.update({item["uuid"]: item for item in state["footprints"].values()})
    return items


def _net_group(state: dict, name: str) -> dict:
    return {
        "copper": {uuid: item for uuid, item in state["copper"].items() if item["net"] == name},
        "pads": {uuid: item for uuid, item in state["pads"].items() if item["net"] == name},
        "zones": {uuid: item for uuid, item in state["zones"].items() if item["net"] == name},
    }


def _validate_request(
    before: dict,
    moves: list[FootprintMove],
    remove_uuids: list[str],
    traces: list[TraceAdd],
    vias: list[ViaAdd],
    autoroute_nets: list[str],
    widths: list[NetWidth],
    required_pads: list[RequiredPad],
    protected_uuids: list[str],
    protected_nets: list[str],
    protected_pad_groups: list[ProtectedPadGroup],
) -> None:
    if not any((moves, remove_uuids, traces, vias, autoroute_nets)):
        raise ToolError("the batch is empty")
    if sum(map(len, (moves, remove_uuids, traces, vias, autoroute_nets))) > 10_000:
        raise ToolError("the batch exceeds 10000 edit operations")
    if any(len(values) > 10_000 for values in (required_pads, protected_uuids, protected_nets, protected_pad_groups)):
        raise ToolError("a validation/protection list exceeds 10000 items")
    for values, label in (
        (remove_uuids, "remove UUID"),
        (autoroute_nets, "autoroute net"),
        (protected_uuids, "protected UUID"),
        (protected_nets, "protected net"),
    ):
        if len(values) != len(set(values)):
            raise ToolError(f"duplicate {label}")
    if len({move.reference for move in moves}) != len(moves):
        raise ToolError("duplicate footprint move")
    for uuid in remove_uuids + protected_uuids:
        if not _UUID.fullmatch(uuid):
            raise ToolError(f"invalid UUID: {uuid}")
    conflicts = set(remove_uuids) & set(protected_uuids)
    if conflicts:
        raise ToolError(f"UUID cannot be both removed and protected: {sorted(conflicts)}")
    missing_nets = (
        set(autoroute_nets)
        | set(protected_nets)
        | {item.net_name for item in traces + vias + required_pads}
    ) - set(before["nets"])
    if missing_nets:
        raise ToolError(f"board nets not found: {sorted(missing_nets)}")
    width_names = [item.net_name for item in widths]
    if len(widths) > 1000:
        raise ToolError("new_trace_widths exceeds 1000 nets")
    if len(width_names) != len(set(width_names)):
        raise ToolError("duplicate new-trace width net")
    if set(width_names) - set(autoroute_nets):
        raise ToolError("new_trace_widths may name only autoroute_nets")
    for required in required_pads:
        footprint = before["footprints"].get(required.reference)
        pad = _pad_map(footprint).get(required.pad_number) if footprint else None
        if pad is None or pad["net"] != required.net_name:
            raise ToolError(
                f"required pad mismatch: {required.reference}.{required.pad_number} is not on {required.net_name}"
            )
    for group in protected_pad_groups:
        footprint = before["footprints"].get(group.reference)
        if footprint is None:
            raise ToolError(f"protected pad group footprint not found: {group.reference}")
        missing = set(group.pad_numbers) - set(_pad_map(footprint))
        if missing:
            raise ToolError(f"protected pads not found on {group.reference}: {sorted(missing)}")


def _verify(
    before: dict,
    after: dict,
    moves: list[FootprintMove],
    remove_uuids: list[str],
    traces: list[TraceAdd],
    vias: list[ViaAdd],
    autoroute_nets: list[str],
    protected_uuids: list[str],
    protected_nets: list[str],
    protected_pad_groups: list[ProtectedPadGroup],
) -> list[str]:
    failures = []
    removed = set(remove_uuids)
    for uuid, item in before["copper"].items():
        if uuid in removed:
            if uuid in after["copper"]:
                failures.append(f"requested copper UUID still exists: {uuid}")
        elif after["copper"].get(uuid) != item:
            failures.append(f"existing copper changed or disappeared: {uuid}")
    allowed_new_nets = set(autoroute_nets) | {item.net_name for item in traces + vias}
    for uuid, item in after["copper"].items():
        if uuid not in before["copper"] and item["net"] not in allowed_new_nets:
            failures.append(f"unexpected new copper {uuid} on {item['net']}")

    moved = {item.reference: item for item in moves}
    for reference, footprint in before["footprints"].items():
        candidate = after["footprints"].get(reference)
        if candidate is None:
            failures.append(f"footprint disappeared: {reference}")
            continue
        if reference not in moved:
            if candidate != footprint:
                failures.append(f"unrequested footprint changed: {reference}")
            continue
        spec = moved[reference]
        expected_layer = spec.layer or footprint["layer"]
        if candidate["uuid"] != footprint["uuid"] or candidate["layer"] != expected_layer:
            failures.append(f"footprint identity/layer changed: {reference}")
        if candidate["position_mm"] != [round(spec.x_mm, 6), round(spec.y_mm, 6)]:
            failures.append(f"footprint position mismatch: {reference}")
        if spec.rotation_deg is not None and candidate["rotation_deg"] % 360 != round(spec.rotation_deg, 6) % 360:
            failures.append(f"footprint rotation mismatch: {reference}")
        old_pads, new_pads = _pad_map(footprint), _pad_map(candidate)
        flipping = expected_layer != footprint["layer"]
        if set(old_pads) != set(new_pads) or any(
            _pad_identity(old_pads[number]) != _pad_identity(new_pads[number])
            or (not flipping and old_pads[number]["layers"] != new_pads[number]["layers"])
            or (
                flipping
                and _pad_copper_side(old_pads[number]) is not None
                and _pad_copper_side(new_pads[number]) != expected_layer
            )
            for number in old_pads.keys() & new_pads.keys()
        ):
            failures.append(f"footprint pad identity changed: {reference}")
    if before["zones"] != after["zones"]:
        failures.append("zone identity/net/layers changed")

    before_uuid, after_uuid = _all_by_uuid(before), _all_by_uuid(after)
    for uuid in protected_uuids:
        if before_uuid.get(uuid) is None or after_uuid.get(uuid) != before_uuid[uuid]:
            failures.append(f"protected UUID changed or is missing: {uuid}")
    for name in protected_nets:
        if _net_group(before, name) != _net_group(after, name):
            failures.append(f"protected net group changed: {name}")
    for group in protected_pad_groups:
        old_fp, new_fp = before["footprints"].get(group.reference), after["footprints"].get(group.reference)
        if old_fp is None or new_fp is None:
            failures.append(f"protected pad group footprint missing: {group.reference}")
            continue
        old_pads, new_pads = _pad_map(old_fp), _pad_map(new_fp)
        numbers = group.pad_numbers or sorted(old_pads)
        if any(old_pads.get(number) != new_pads.get(number) for number in numbers):
            failures.append(f"protected pad group changed: {group.reference}:{numbers}")
    return failures


def _exchange(left: Path, right: Path) -> None:
    """Atomically exchange two Linux directory entries without a missing-path window."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = libc.renameat2
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(_AT_FDCWD, os.fsencode(left), _AT_FDCWD, os.fsencode(right), _RENAME_EXCHANGE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _identity(path: Path) -> tuple[int, int]:
    current = path.stat()
    return current.st_dev, current.st_ino


def _atomic_replace(source: Path, candidate: Path, expected_sha256: str) -> str:
    with _LOCK, source.open("rb") as lock_stream:
        fcntl.flock(lock_stream, fcntl.LOCK_EX)
        actual = _sha(source)
        if actual != expected_sha256:
            raise ToolError(f"CAS mismatch before write: expected {expected_sha256}, found {actual}")
        source_stat = source.stat()
        os.chmod(candidate, stat.S_IMODE(source_stat.st_mode))
        with candidate.open("rb") as stream:
            os.fsync(stream.fileno())
        candidate_sha = _sha(candidate)
        candidate_identity = _identity(candidate)
        _exchange(candidate, source)
        if _identity(source) != candidate_identity:
            raise ToolError("canonical board was replaced concurrently during atomic write")
        previous_sha = _sha(candidate)
        if previous_sha != expected_sha256:
            if _identity(source) != candidate_identity or _sha(source) != candidate_sha:
                raise ToolError("canonical board changed concurrently and safe rollback is impossible")
            _exchange(candidate, source)
            raise ToolError(f"CAS mismatch at atomic write: expected {expected_sha256}, found {previous_sha}")
        if _identity(source) != candidate_identity or _sha(source) != candidate_sha:
            raise ToolError("canonical board changed concurrently after atomic write")
        with source.open("rb") as stream:
            os.fsync(stream.fileno())
        directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return candidate_sha


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_board_edit_state(
    pcb_path: str,
    net_names: list[str] | None = None,
    references: list[str] | None = None,
    include_drc: bool = False,
    drc_timeout: int = 180,
) -> dict:
    """Read SHA-256 and native footprint/pad/track/via UUID state, optionally filtered.

    This never writes the board. Use net_names to select copper and pads by exact net name,
    and references to select footprints/pads by exact reference. include_drc runs the full
    native KiCad DRC in a temporary directory and returns violations and open connections.
    """
    path = _path(pcb_path)
    sha256 = _sha(path)
    state = _worker({"action": "snapshot", "path": str(path)})
    result = {"pcb_path": str(path), "sha256": sha256, "counts": state["counts"], "nets": state["nets"]}
    names, refs = set(net_names or []), set(references or [])
    result["footprints"] = {
        key: value for key, value in state["footprints"].items() if not refs or key in refs
    }
    result["copper"] = {
        key: value for key, value in state["copper"].items() if not names or value["net"] in names
    }
    result["pads"] = {
        key: value
        for key, value in state["pads"].items()
        if (not names or value["net"] in names) and (not refs or value["reference"] in refs)
    }
    result["zones"] = {
        key: value for key, value in state["zones"].items() if not names or value["net"] in names
    }
    if include_drc:
        with tempfile.TemporaryDirectory(prefix="kicad-state-") as tmp:
            result["drc"] = _drc(path, Path(tmp), drc_timeout)
    if _sha(path) != sha256:
        raise ToolError("board changed during native state read")
    return result


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False))
def edit_board_copper_batch(
    pcb_path: str,
    expected_sha256: str,
    moves: list[FootprintMove] | None = None,
    remove_uuids: list[str] | None = None,
    traces: list[TraceAdd] | None = None,
    vias: list[ViaAdd] | None = None,
    autoroute_nets: list[str] | None = None,
    new_trace_widths: list[NetWidth] | None = None,
    required_pads: list[RequiredPad] | None = None,
    protected_uuids: list[str] | None = None,
    protected_nets: list[str] | None = None,
    protected_pad_groups: list[ProtectedPadGroup] | None = None,
    max_passes: int = 20,
    autoroute_timeout: int = 600,
    drc_timeout: int = 180,
    dry_run: bool = True,
    confirmation_token: str | None = None,
) -> dict:
    """Dry-run or atomically apply one bounded native PCB edit package.

    The package may move footprints (including an optional native F.Cu/B.Cu side flip),
    remove exact segment/arc/via UUIDs, add explicit traces and vias by net NAME, and
    optionally call the installed scoped autoroute_pcb mechanism.
    Autorouting is always single-threaded, preserves old copper, and imports only requested
    nets. new_trace_widths changes only copper newly created by that autoroute call.

    Every call CAS-checks expected_sha256, works on a temporary board, refills zones, reopens
    it with pcbnew, runs full native DRC including open connections, and compares all untouched
    copper plus requested protected UUID/net/pad groups. A passing dry run returns a token.
    Repeat the identical package with dry_run=false and that token to atomically replace the
    same canonical PCB. Any mismatch or failed check leaves the canonical file byte-identical.
    """
    path = _path(pcb_path)
    if not _SHA256.fullmatch(expected_sha256):
        raise ToolError("expected_sha256 must be exactly 64 hexadecimal characters")
    expected_sha256 = expected_sha256.lower()
    actual_sha = _sha(path)
    if actual_sha != expected_sha256:
        raise ToolError(f"CAS mismatch: expected {expected_sha256}, found {actual_sha}")
    moves, remove_uuids = moves or [], remove_uuids or []
    traces, vias, autoroute_nets = traces or [], vias or [], autoroute_nets or []
    widths, required_pads = new_trace_widths or [], required_pads or []
    protected_uuids, protected_nets = protected_uuids or [], protected_nets or []
    protected_pad_groups = protected_pad_groups or []
    before = _worker({"action": "snapshot", "path": str(path)})
    _validate_request(
        before,
        moves,
        remove_uuids,
        traces,
        vias,
        autoroute_nets,
        widths,
        required_pads,
        protected_uuids,
        protected_nets,
        protected_pad_groups,
    )
    package = {
        "moves": [item.model_dump(exclude_none=True) for item in moves],
        "remove_uuids": remove_uuids,
        "traces": [item.model_dump() for item in traces],
        "vias": [item.model_dump() for item in vias],
        "autoroute_nets": autoroute_nets,
        "new_trace_widths": [item.model_dump() for item in widths],
        "required_pads": [item.model_dump() for item in required_pads],
        "protected_uuids": protected_uuids,
        "protected_nets": protected_nets,
        "protected_pad_groups": [item.model_dump() for item in protected_pad_groups],
        "max_passes": max_passes,
    }
    if max_passes < 1 or max_passes > 100 or autoroute_timeout < 1 or drc_timeout < 1:
        raise ToolError("max_passes must be 1..100 and timeouts must be positive")

    with tempfile.TemporaryDirectory(prefix=".kicad-batch-", dir=path.parent) as tmp_name:
        tmp = Path(tmp_name)
        stage = _stage_project(path, tmp)
        stage_state = _worker(
            {
                "action": "edit",
                "path": str(stage),
                "moves": package["moves"],
                "remove_uuids": remove_uuids,
                "traces": package["traces"],
                "vias": package["vias"],
            }
        )
        candidate = stage
        if autoroute_nets:
            if "nets" not in inspect.signature(pcb.autoroute_pcb).parameters:
                raise ToolError("installed autoroute_pcb lacks the required scoped nets interface")
            try:
                routed = pcb.autoroute_pcb(
                    pcb_path=str(stage),
                    max_passes=max_passes,
                    num_threads=1,
                    timeout=autoroute_timeout,
                    output_dir=str(tmp),
                    nets=autoroute_nets,
                )
            except Exception as exc:
                return {
                    "status": "rejected",
                    "source_sha256": expected_sha256,
                    "source_unchanged": _sha(path) == expected_sha256,
                    "failures": [f"scoped autoroute failed: {exc}"],
                }
            candidate = Path(routed.routed_path)
            if candidate.is_symlink() or not candidate.is_file():
                raise ToolError("scoped autoroute returned no task-owned routed board")
            candidate = candidate.resolve()
            if not candidate.is_relative_to(tmp.resolve()):
                raise ToolError("scoped autoroute returned a board outside its task directory")
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = stage.with_suffix(suffix)
                if sidecar.is_file():
                    shutil.copyfile(sidecar, candidate.with_suffix(suffix))
            final_state = _worker(
                {
                    "action": "finalize",
                    "path": str(candidate),
                    "baseline_uuids": list(stage_state["copper"]),
                    "new_trace_widths_mm": {item.net_name: item.width_mm for item in widths},
                }
            )
        else:
            final_state = stage_state

        drc = _drc(candidate, tmp, drc_timeout)
        failures = _verify(
            before,
            final_state,
            moves,
            remove_uuids,
            traces,
            vias,
            autoroute_nets,
            protected_uuids,
            protected_nets,
            protected_pad_groups,
        )
        if drc["violation_count"] or drc["unconnected_count"]:
            failures.append(
                f"native DRC rejected candidate: {drc['violation_count']} violations, {drc['unconnected_count']} opens"
            )
        source_now = _sha(path)
        if source_now != expected_sha256:
            failures.append(f"canonical board changed during dry-run: {source_now}")
        removed_segments = sum(
            before["copper"][uuid]["type"] in {"segment", "arc"} for uuid in remove_uuids
        )
        removed_vias = sum(before["copper"][uuid]["type"] == "via" for uuid in remove_uuids)
        candidate_sha = _sha(candidate)
        token_body = json.dumps(
            {"source": expected_sha256, "package": package},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        token = hashlib.sha256(token_body).hexdigest()
        result = {
            "status": "rejected" if failures else "dry_run_passed",
            "source_sha256": expected_sha256,
            "candidate_sha256": candidate_sha,
            "source_unchanged": source_now == expected_sha256,
            "native_reopen": True,
            "drc": drc,
            "failures": failures,
            "changes": {
                "moves": len(moves),
                "removed": len(remove_uuids),
                "new_segments": final_state["counts"]["segments"] - before["counts"]["segments"] + removed_segments,
                "new_vias": final_state["counts"]["vias"] - before["counts"]["vias"] + removed_vias,
                "autoroute_nets": autoroute_nets,
            },
        }
        if failures:
            return result
        result["confirmation_token"] = token
        if dry_run:
            return result
        if confirmation_token != token:
            raise ToolError("confirmation_token does not match this native dry-run candidate")
        new_sha = _atomic_replace(path, candidate, expected_sha256)
        result.update({"status": "applied", "source_unchanged": False, "new_sha256": new_sha})
        return result
