"""Build a checksum-pinned PCM bundle; seed missing content before KiCad starts."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import urllib.request
import venv
import zipfile

CONTENT = {"plugins", "resources", "footprints", "3dmodels", "symbols", "colors"}
REPOSITORY = "https://repository.kicad.org/repository.json"


def disk_id(identifier):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]", identifier):
        raise ValueError(f"Invalid PCM identifier: {identifier!r}")
    return identifier.replace(".", "_")


def compatible(release, target):
    def bound(value, fill):
        parts = tuple(int(n) for n in value.split("."))
        return parts + (fill,) * (3 - len(parts))

    return (bound(release["kicad_version"], 0) <= bound(target, 0)
            and (not release.get("kicad_version_max")
                 or bound(target, 0) <= bound(release["kicad_version_max"], 999))
            and (not release.get("platforms") or "linux" in release["platforms"]))


def unpack(raw, spec, destination, target_version="10.0.7"):
    """Follow KiCad's content-type/package-id mapping, never ZIP member paths."""
    if hashlib.sha256(raw).hexdigest() != spec["download_sha256"]:
        raise ValueError(f"Checksum mismatch: {spec['identifier']}")
    package_id = disk_id(spec["identifier"])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 20000 or sum(m.file_size for m in members) > 1024**3:
            raise ValueError("PCM archive exceeds extraction limits")
        for member in members:
            parts = member.filename.split("/")
            kind = stat.S_IFMT(member.external_attr >> 16)
            if (member.filename.startswith("/") or "\\" in member.filename
                    or any(p in {".", ".."} for p in parts)
                    or kind not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise ValueError(f"Unsafe ZIP member: {member.filename!r}")
        metadata = json.loads(archive.read("metadata.json"))
        if metadata["identifier"] != spec["identifier"]:
            raise ValueError("Archive identifier does not match the lock")
        release = next(v for v in metadata["versions"] if v["version"] == spec["version"])
        if release["status"] != "stable":
            raise ValueError("Only stable PCM releases are bundled")
        if not compatible(release, target_version):
            raise ValueError(f"Incompatible PCM release: {spec['identifier']}")
        written = 0
        for member in members:
            parts = PurePosixPath(member.filename).parts
            if member.is_dir() or len(parts) < 2 or parts[0] not in CONTENT:
                continue
            target = destination / "3rdparty" / parts[0] / package_id / Path(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(archive.read(member))
            written += 1
        if not written:
            raise ValueError("PCM archive contains no installable content")
    return {"package": metadata, "current_version": spec["version"],
            "repository_id": "" if spec.get("source") else hashlib.sha256(REPOSITORY.encode()).hexdigest()[:16],
            "repository_name": "Pinned local bundle" if spec.get("source") else "KiCad official repository",
            "install_timestamp": 0, "pinned": True}


def build(lock_path, destination):
    lock = json.loads(lock_path.read_text())
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    identifiers = set()
    for spec in lock["packages"]:
        if spec["identifier"] in identifiers:
            raise ValueError("Duplicate package in PCM lock")
        identifiers.add(spec["identifier"])
        if not re.fullmatch(r"[0-9a-f]{64}", spec["download_sha256"]):
            raise ValueError("Invalid SHA-256 in PCM lock")
        if not spec["download_url"].startswith("https://"):
            raise ValueError("PCM download must use HTTPS")
        with urllib.request.urlopen(spec["download_url"], timeout=120) as response:
            if not response.url.startswith("https://"):
                raise ValueError("PCM download redirected to an insecure URL")
            raw = response.read(128 * 1024**2 + 1)
        if len(raw) > 128 * 1024**2:
            raise ValueError("PCM download exceeds size limit")
        entries.append(unpack(raw, spec, destination, lock["kicad_version"]))
        print(f"Bundled {spec['identifier']} {spec['version']}", flush=True)
    (destination / "installed_packages.json").write_text(json.dumps({"packages": entries}, indent=2) + "\n")
    aliases = set()
    for kind, suffix in [("footprints", ".kicad_mod"), ("symbols", ".kicad_sym")]:
        for path in (destination / "3rdparty" / kind).rglob("*" + suffix):
            aliases.update(re.findall(r"\bKICAD\d+_(?:3RD_PARTY|3DMODEL_DIR)\b", path.read_text()))
    (destination / "compatibility_aliases.json").write_text(json.dumps(sorted(aliases)) + "\n")


def prepare_environments(lock_path, bundle):
    """Use KiCad 10's own per-plugin environment layout, not a custom runner."""
    environments = {}
    for spec in json.loads(lock_path.read_text())["packages"]:
        if spec.get("runtime") != "ipc":
            continue
        plugin_root = bundle / "3rdparty/plugins" / disk_id(spec["identifier"])
        manifests = list(plugin_root.rglob("plugin.json"))
        if len(manifests) != 1:
            raise ValueError(f"Expected one IPC manifest: {spec['identifier']}")
        manifest = manifests[0]
        identifier = json.loads(manifest.read_text())["identifier"]
        disk_id(identifier)  # Validate, but do not normalize KiCad's IPC cache identifier.
        if identifier in environments:
            raise ValueError(f"Duplicate IPC identifier: {identifier}")
        target = bundle / "python-environments" / identifier
        venv.EnvBuilder(system_site_packages=True, with_pip=True).create(target)
        subprocess.run([str(target / "bin/python"), "-m", "pip", "install", "--no-cache-dir",
                        "--only-binary=:all:", "-r", str(manifest.parent / "requirements.txt"),
                        "-c", str(Path(__file__).with_name("requirements-ipc.txt")),
                        f"kicad-python=={spec['ipc_sdk_version']}"], check=True,
                       env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
                       | {"VIRTUAL_ENV": str(target)})
        (target / "pip.conf").write_text("[global]\ndisable-pip-version-check = true\n")
        environments[identifier] = {"identifier": spec["identifier"], "version": spec["version"]}
    (bundle / "ipc_environments.json").write_text(json.dumps(environments, indent=2) + "\n")


def library_table(template, third_party, kind):
    text = template.read_text().rstrip()
    if not text.endswith(")"):
        raise ValueError(f"Invalid stock library table: {template}")
    root = third_party / ("symbols" if kind == "sym" else "footprints")
    libraries = sorted(root.glob("*/*.kicad_sym" if kind == "sym" else "*/*.pretty"))
    rows = []
    names = set()
    for path in libraries:
        name = "PCM_" + path.stem
        if name in names:
            raise ValueError(f"Duplicate PCM library name: {name}")
        names.add(name)
        rows.append(f"  (lib (name {json.dumps(name)})(type \"KiCad\")"
                    f"(uri {json.dumps(str(path))})(options \"\")(descr \"Bundled PCM library\"))")
    return text[:-1] + "\n" + "\n".join(rows) + "\n)\n"


def seed(bundle, home, stock):
    config_root = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "kicad"
    config = Path(os.environ.get("KICAD_CONFIG_HOME") or config_root) / "10.0"
    documents = Path(os.environ.get("KICAD_DOCUMENTS_HOME") or os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    data = documents / "kicad/10.0/3rdparty"
    config.mkdir(parents=True, exist_ok=True)
    common_path = config / "kicad_common.json"
    if common_path.is_symlink():
        raise ValueError("Refusing symlinked common settings")
    common = json.loads(common_path.read_text()) if common_path.exists() else {}
    original_common = json.dumps(common, sort_keys=True)
    environment = common.setdefault("environment", {})
    if environment.get("vars") is None:
        environment["vars"] = {}
    variables = environment["vars"]
    if not data.is_absolute():
        raise ValueError("KiCad content root must be an absolute path")
    variables.setdefault("KICAD10_3RD_PARTY", str(data))
    aliases = bundle / "compatibility_aliases.json"
    for name in json.loads(aliases.read_text()) if aliases.exists() else []:
        value = str(data) if name.endswith("_3RD_PARTY") else str(stock.parent / "3dmodels")
        variables.setdefault(name, value)
    runtimes_path = bundle / "ipc_environments.json"
    runtimes = json.loads(runtimes_path.read_text()) if runtimes_path.exists() else {}
    if runtimes:
        api = common.setdefault("api", {})
        api.setdefault("enable_server", True)
        api.setdefault("interpreter_path", "/usr/bin/python3")
    registry = config / "installed_packages.json"
    if registry.is_symlink():
        raise ValueError("Refusing a symlinked PCM registry")
    current = json.loads(registry.read_text()) if registry.exists() else {"packages": []}
    if not isinstance(current.get("packages"), list):
        raise ValueError("Invalid existing PCM registry; refusing to replace it")
    installed = {entry["package"]["identifier"] for entry in current["packages"]}
    bundled = json.loads((bundle / "installed_packages.json").read_text())["packages"]
    changed = False
    for entry in bundled:
        identifier = entry["package"]["identifier"]
        if identifier in installed:
            continue  # PCM-managed user versions always win over image defaults.
        package_id = disk_id(identifier)
        sources = [bundle / "3rdparty" / kind / package_id for kind in sorted(CONTENT)]
        sources = [source for source in sources if source.is_dir()]
        if not sources:
            raise ValueError(f"Bundled package has no content: {identifier}")
        if any((data / source.parent.name / package_id).exists()
               or (data / source.parent.name / package_id).is_symlink() for source in sources):
            continue  # Preserve manually installed content absent from the registry, too.
        for source in sources:
            shutil.copytree(source, data / source.parent.name / package_id)
        entry["install_timestamp"] = int(time.time())
        current["packages"].append(entry)
        changed = True
    if changed or not registry.exists():
        temporary = registry.with_suffix(".json.new")
        temporary.write_text(json.dumps(current, indent=2) + "\n")
        temporary.replace(registry)
    if json.dumps(common, sort_keys=True) != original_common:
        temporary = common_path.with_suffix(".json.new")
        temporary.write_text(json.dumps(common, indent=2) + "\n")
        temporary.replace(common_path)
    cache = Path(os.environ.get("KICAD_CACHE_HOME") or os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    versions = {entry["package"]["identifier"]: entry["current_version"] for entry in current["packages"]}
    for identifier, spec in runtimes.items():
        disk_id(identifier)
        target = cache / "kicad/10.0/python-environments" / identifier
        if versions.get(spec["identifier"]) == spec["version"] and not target.exists() and not target.is_symlink():
            shutil.copytree(bundle / "python-environments" / identifier, target, symlinks=True)
    for kind, filename in [("sym", "sym-lib-table"), ("fp", "fp-lib-table")]:
        target = config / filename
        if not target.exists() and not target.is_symlink():
            target.write_text(library_table(stock / filename, data, kind))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("lock", type=Path)
    build_parser.add_argument("destination", type=Path)
    seed_parser = sub.add_parser("seed")
    seed_parser.add_argument("bundle", type=Path)
    seed_parser.add_argument("home", type=Path)
    seed_parser.add_argument("--stock", type=Path, default=Path("/usr/share/kicad/template"))
    runtime_parser = sub.add_parser("runtimes")
    runtime_parser.add_argument("lock", type=Path)
    runtime_parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    if args.command == "build":
        build(args.lock, args.destination)
    elif args.command == "runtimes":
        prepare_environments(args.lock, args.bundle)
    else:
        seed(args.bundle, args.home, args.stock)


if __name__ == "__main__":
    main()
