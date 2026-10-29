"""Apply the exact PR974 class backport to the checksummed official 2.5.0 JAR."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

BASE_SHA = 'f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7'
ENTRY = 'app/freerouting/autoroute/path/FoundConnectionInserter.class'
jar, classes = map(Path, sys.argv[1:])
sha = lambda data: hashlib.sha256(data).hexdigest()
assert sha(jar.read_bytes()) == BASE_SHA, 'Unexpected Freerouting base artifact'
compiled = list(classes.rglob('*.class'))
assert [p.relative_to(classes).as_posix() for p in compiled] == [ENTRY]
replacement = compiled[0].read_bytes()
assert int.from_bytes(replacement[6:8], 'big') == 69, 'Expected Java25 class'
target = jar.with_suffix('.jar.new')
with zipfile.ZipFile(jar) as original, zipfile.ZipFile(target, 'w') as patched:
    assert not any(n.upper().endswith(('.SF', '.RSA', '.DSA')) and n.startswith('META-INF/') for n in original.namelist()), 'Signed archive cannot be modified'
    for info in original.infolist():
        patched.writestr(info, replacement if info.filename == ENTRY else original.read(info.filename))
with zipfile.ZipFile(jar) as original, zipfile.ZipFile(target) as patched:
    assert original.namelist() == patched.namelist()
    changed = [n for n in original.namelist() if original.read(n) != patched.read(n)]
    assert changed == [ENTRY], f'Unexpected changed archive entries: {changed}'
    assert patched.testzip() is None
manifest = {
    'version': '2.5.0+fanout-974',
    'upstream_version': '2.5.0',
    'upstream_jar_sha256': BASE_SHA,
    'upstream_source': 'b647a2f824cfbc54bc4dcaee71b03b7cd945eb76',
    'backported_fix': 'b51633f0ccfe9ddd921ef55fa709d673d101c14e',
    'patched_source_sha256': '207aacb5dd07cc5e733883871a32143e354be25f9be36727bc15c2263ce0bdae',
    'jar_sha256': sha(target.read_bytes()),
    'changed_entries': changed,
    'compiled_class_sha256': sha(replacement),
    'compiler_image': 'eclipse-temurin:25-jdk@sha256:8c052273471ca5128e67f18ffe9e9c32abf1b104bb0090fa10175eda51004145',
}
target.chmod(0o644)
target.replace(jar)
jar.with_suffix('.provenance.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest))
