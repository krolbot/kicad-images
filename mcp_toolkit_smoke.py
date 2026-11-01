"""Real tools/list + tools/call checks inside the candidate image; no Lab access."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


MAKE_BOARD = '''
from pathlib import Path
import pcbnew
import sys
board=pcbnew.BOARD()
for start,end in [((0,0),(30,0)),((30,0),(30,20)),((30,20),(0,20)),((0,20),(0,0))]:
 edge=pcbnew.PCB_SHAPE();edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
 edge.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]),pcbnew.FromMM(start[1])))
 edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]),pcbnew.FromMM(end[1])))
 edge.SetWidth(pcbnew.FromMM(.05));edge.SetLayer(pcbnew.Edge_Cuts);board.Add(edge)
for lib,name,ref,value,x in [('Resistor_SMD','R_0603_1608Metric','R1','10k',6),('Resistor_SMD','R_0603_1608Metric','R2','10k',14),('MountingHole','MountingHole_2.2mm_M2','H1','MountingHole',24)]:
 fp=pcbnew.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty',name)
 assert fp is not None
 fp.SetFPID(pcbnew.LIB_ID(lib,name));fp.SetReference(ref);fp.SetValue(value)
 if ref=='R1': fp.SetField('Var','Power low(10k) high(22k)')
 fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(10)))
 path=pcbnew.KIID_PATH();path.push_back(pcbnew.KIID('11111111-1111-4111-8111-111111111111'));path.push_back(pcbnew.KIID())
 fp.SetPath(path);fp.SetSheetfile('manufacturing.kicad_sch');fp.SetSheetname('Root');board.Add(fp)
pcbnew.SaveBoard(sys.argv[1],board)
'''


async def checked(session, name, arguments):
    result = await session.call_tool(name, arguments)
    text = '\n'.join(item.text for item in result.content if item.type == 'text')
    assert not result.is_error, (name, text)
    structured = result.model_dump(by_alias=True).get('structuredContent')
    payload = structured if structured is not None else json.loads(text)
    assert isinstance(payload, dict), (name, payload)
    return payload


def artifacts(result):
    files = result['files']
    assert files and result['source_modified'] is False
    for row in files:
        path = Path(row['path'])
        assert path.is_file() and path.stat().st_size == row['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256']
    return [Path(row['path']) for row in files]


def native_poses(board):
    code = ("import pcbnew,json,sys;b=pcbnew.LoadBoard(sys.argv[1]);"
            "print(json.dumps({f.GetReference():[pcbnew.ToMM(f.GetPosition().x),pcbnew.ToMM(f.GetPosition().y),"
            "f.GetOrientationDegrees(),bool(f.IsFlipped())] for f in b.GetFootprints()}))")
    return json.loads(subprocess.check_output(['/opt/kicad-tools/bin/python', '-c', code, str(board)], text=True))


CHECK_REPLICATION = r"""
import sys,json,pcbnew
sys.path.insert(0,'/opt/kicad-addons/3rdparty/plugins/com_github_MitjaNemec_ReplicateLayout')
sys.path.insert(0,'/opt/kicad-build/replication-fixture')
from replicate_layout import Replicator
from test_helpers import geometric_consistency
b=pcbnew.LoadBoard(sys.argv[1]);rep=Replicator(b,'U701',lambda *a,**k: None)
src=rep.get_fp_by_ref('U701');src_fps=rep.get_footprints_on_sheet(src.sheet_id[:2])
destinations=[rep.get_footprints_on_sheet(s) for s in json.loads(sys.argv[2])]
dst_refs=[[rep.match_fp_in_list(f,items).ref for f in src_fps] for items in destinations]
ok,issues=geometric_consistency(b,'U701',[f.ref for f in src_fps],dst_refs)
assert ok,issues
assert len(list(b.GetTracks()))>0 and len(list(b.GetFootprints()))==105
print('replication native geometry verified')
"""


async def main():
    with tempfile.TemporaryDirectory(prefix='toolkit-mcp-smoke-', dir='/projects') as temporary:
        root = Path(temporary)
        board = root / 'manufacturing.kicad_pcb'
        subprocess.run(['/opt/kicad-tools/bin/python', '-c', MAKE_BOARD, str(board)], check=True)
        before = hashlib.sha256(board.read_bytes()).hexdigest()
        async with asyncio.timeout(600):
            async with stdio_client(StdioServerParameters(command='/usr/local/bin/kicad-mcp')) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    page = await session.list_tools()
                    assert not page.model_dump(by_alias=True).get('nextCursor')
                    names = {t.name for t in page.tools}
                    required = {'get_toolkit_status', 'manufacture_pcb', 'generate_interactive_bom', 'panelize_pcb', 'render_pcb', 'discover_replication', 'replicate_layout', 'place_footprints', 'place_parts', 'scan_project_dependencies', 'localize_project_dependencies', 'export_testpoints', 'generate_pinout', 'generate_pinout_image', 'generate_kibuzzard_footprint', 'apply_variant', 'solve_trace_impedance', 'run_prefab_gate', 'get_board_edit_state', 'edit_board_copper_batch', 'autoroute_pcb', 'run_drc', 'get_symbol_info'}
                    assert required <= names, sorted(required - names)
                    status = await checked(session, 'get_toolkit_status', {})
                    assert status['kicad'] == os.environ['KICAD_VERSION'], status
                    manufacturing = await checked(session, 'manufacture_pcb', {'pcb_path': str(board), 'output_dir': str(root / 'fabrication')})
                    fab = artifacts(manufacturing)
                    for suffix in ['.gbr', '.drl', '.csv', '.pdf']:
                        assert any(p.suffix == suffix and p.stat().st_size for p in fab), suffix
                    bom = await checked(session, 'generate_interactive_bom', {'pcb_path': str(board), 'output_dir': str(root / 'assembly')})
                    html = [p for p in artifacts(bom) if p.suffix == '.html']
                    assert len(html) == 1 and 'pcbdata' in html[0].read_text()
                    panel = await checked(session, 'panelize_pcb', {'pcb_path': str(board), 'output_dir': str(root / 'panel'), 'rows': 1, 'columns': 2})
                    panels = [p for p in artifacts(panel) if p.suffix == '.kicad_pcb']
                    assert len(panels) == 1
                    subprocess.run(['/opt/kicad-tools/bin/python', '-c', 'import pcbnew,sys;b=pcbnew.LoadBoard(sys.argv[1]);assert len(list(b.GetFootprints()))==6', str(panels[0])], check=True)
                    drawing = await checked(session, 'render_pcb', {'pcb_path': str(board),
                        'output_dir': str(root / 'drawing'), 'mode': 'stylized', 'format': 'svg'})
                    drawing_svg = next(p for p in artifacts(drawing) if p.suffix == '.svg')
                    assert '<svg' in drawing_svg.read_text()
                    duplicate = await session.call_tool('generate_interactive_bom', {'pcb_path': str(board), 'output_dir': str(root / 'assembly')})
                    assert duplicate.is_error, 'Existing output must be rejected through real MCP'
                    invalid = await session.call_tool('panelize_pcb', {'pcb_path': str(board), 'output_dir': str(root / 'invalid'), 'rows': 0})
                    assert invalid.is_error and not (root / 'invalid').exists()
                    testpoints = await checked(session, 'export_testpoints', {'pcb_path': str(board),
                        'output_dir': str(root / 'testpoints'), 'pads': [{'reference': 'R1', 'pad': '1'}]})
                    testpoint_csv = next(p for p in artifacts(testpoints) if p.suffix == '.csv')
                    assert 'source ref des' in testpoint_csv.read_text() and 'R1' in testpoint_csv.read_text()
                    pinout = await checked(session, 'generate_pinout', {'pcb_path': str(board),
                        'output_dir': str(root / 'pinout'), 'references': ['R1'], 'format': 'markdown'})
                    pinout_md = next(p for p in artifacts(pinout) if p.suffix == '.md')
                    assert 'Pinout for R1' in pinout_md.read_text()
                    pinout_image = await checked(session, 'generate_pinout_image', {'pcb_path': str(board),
                        'output_dir': str(root / 'pinout-image'), 'references': ['R1'], 'png_dpi': 96})
                    pinout_image_files = artifacts(pinout_image)
                    assert {p.suffix for p in pinout_image_files} >= {'.svg', '.png'}
                    buzzard = await checked(session, 'generate_kibuzzard_footprint', {
                        'output_dir': str(root / 'kibuzzard'), 'text': 'MCP', 'height_mm': 2,
                        'layer': 'F.SilkS', 'left_cap': 'square', 'right_cap': 'square'})
                    buzzard_mod = next(p for p in artifacts(buzzard) if p.suffix == '.kicad_mod')
                    assert '(footprint' in buzzard_mod.read_text()
                    variant = await checked(session, 'apply_variant', {'pcb_path': str(board),
                        'output_dir': str(root / 'variant'), 'choices': {'Power': 'high'}})
                    variant_board = next(p for p in artifacts(variant) if p.suffix == '.kicad_pcb')
                    value = subprocess.check_output(['/opt/kicad-tools/bin/python', '-c',
                        "import pcbnew,sys;print(pcbnew.LoadBoard(sys.argv[1]).FindFootprintByReference('R1').GetValue())",
                        str(variant_board)], text=True).strip()
                    assert value == '22k', value
                    impedance = await checked(session, 'solve_trace_impedance', {'kind': 'microstrip',
                        'width_mm': .3, 'copper_thickness_um': 35, 'dielectric_height_mm': .2,
                        'dielectric_er': 4.2, 'quality': 'schnell'})
                    assert impedance['converged'] and 40 < impedance['z0_ohms'] < 70, impedance
                    gate = await checked(session, 'run_prefab_gate', {'pcb_path': str(board),
                        'output_dir': str(root / 'prefab-gate'), 'no_ampacity': True, 'no_parity': True})
                    assert gate['operation']['passed'] is True, gate
                    assert next(p for p in artifacts(gate) if p.name == 'prefab-gate.json')
                    for kind, layout in {
                        'linear': {'kind': 'linear', 'step_x_mm': 5, 'step_y_mm': 0},
                        'matrix': {'kind': 'matrix', 'step_x_mm': 5, 'step_y_mm': 3, 'columns': 2},
                        'circular': {'kind': 'circular', 'radius_mm': 5, 'delta_angle_deg': 90},
                    }.items():
                        placed = await checked(session, 'place_footprints', {'pcb_path': str(board),
                            'output_dir': str(root / kind), 'anchor_ref': 'R1', 'references': ['R1', 'R2'], 'layout': layout})
                        outputs = [p for p in artifacts(placed) if p.suffix == '.kicad_pcb']
                        assert len(outputs) == 1
                        poses = native_poses(outputs[0])
                        assert poses['R2'][:2] != native_poses(board)['R2'][:2], (kind, poses)
                        if kind == 'linear':
                            assert poses['R2'][:2] == [11, 10], poses
                    placed = await checked(session, 'place_parts', {'pcb_path': str(board),
                        'output_dir': str(root / 'parts'), 'origin_mm': [0, 20], 'group_name': 'smoke',
                        'placements': [{'ref': 'R2', 'x_mm': 15, 'y_mm': 8, 'rotation_deg': 90, 'side': 'back'}]})
                    parts = [p for p in artifacts(placed) if p.suffix == '.kicad_pcb']
                    assert native_poses(parts[0])['R2'] == [15, 12, 90, True], native_poses(parts[0])
                    bad_ref = await session.call_tool('place_parts', {'pcb_path': str(board),
                        'output_dir': str(root / 'bad-ref'), 'placements': [{'ref': 'MISSING', 'x_mm': 1, 'y_mm': 1}]})
                    assert bad_ref.is_error and not (root / 'bad-ref').exists()

                    project = root / 'dependency-input'
                    project.mkdir()
                    shutil.copyfile(board, project / board.name)
                    (project / 'fp-lib-table').write_text('(fp_lib_table (version 7) '
                        '(lib (name "Resistor_SMD")(type "KiCad")(uri "/usr/share/kicad/footprints/Resistor_SMD.pretty")(options "")(descr "")))')
                    scanned = await checked(session, 'scan_project_dependencies', {'project_dir': str(project), 'include_standard': True})
                    keys = [row['key'] for row in scanned['items'] if row['type'] == 'footprint' and row['ref'] == 'R1']
                    assert len(keys) == 1, scanned
                    localized = await checked(session, 'localize_project_dependencies', {'project_dir': str(project),
                        'output_dir': str(root / 'localized'), 'include_standard': True, 'selection_keys': keys, 'create_archive': True})
                    localized_files = artifacts(localized)
                    assert any(p.suffix == '.zip' for p in localized_files)
                    assert any(p.suffix == '.kicad_mod' for p in localized_files)
                    localized_board = Path(localized['operation']['output_project_dir']) / board.name
                    assert localized_board in localized_files
                    assert 'Resistor_SMD_local' in localized_board.read_text()
                    assert (project / board.name).read_bytes() == board.read_bytes()

                    replication = root / 'replication-input'
                    shutil.copytree('/opt/kicad-build/replication-fixture', replication)
                    rep_board = replication / 'replicate_layout_test_project.kicad_pcb'
                    rep_before = rep_board.read_bytes()
                    discovery = await checked(session, 'discover_replication', {'pcb_path': str(rep_board), 'anchor_ref': 'U701'})
                    level = discovery['levels'][1]
                    targets = [level['destination_sheets'][i] for i in [1, 3, 7]]
                    replicated = await checked(session, 'replicate_layout', {'pcb_path': str(rep_board),
                        'output_dir': str(root / 'replicated'), 'anchor_ref': 'U701', 'level_index': 1,
                        'destination_sheets': targets, 'remove_duplicates': True})
                    replica = next(p for p in artifacts(replicated) if p.suffix == '.kicad_pcb')
                    subprocess.run(['/opt/kicad-tools/bin/python', '-c', CHECK_REPLICATION,
                                    str(replica), json.dumps(targets)], check=True)
                    assert rep_board.read_bytes() == rep_before
                    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '-o', str(root / 'native-drc.json'), str(board)], check=True)
                    report = json.loads((root / 'native-drc.json').read_text())
                    drc_errors = [row for row in report['violations'] if row['severity'] == 'error']
                    drc_warnings = [row for row in report['violations'] if row['severity'] == 'warning']
                    assert not drc_errors and not report['unconnected_items'], report
                    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
                    print(json.dumps({'kicad': status['kicad'], 'mcp_tools': len(names), 'manufacturing_files': len(fab),
                                      'ibom': True, 'panel_footprints': 6, 'pcbdraw': True,
                                      'native_placement_modes': 3, 'parts_placer': True,
                                      'localization_archive': True, 'replication_sections': 3, 'testpoints': True,
                                      'pinout': True, 'pinout_image': True, 'kibuzzard': True,
                                      'variant': True, 'impedance': True, 'prefab_gate': True,
                                      'native_drc_errors': 0, 'native_drc_warnings': len(drc_warnings),
                                      'source_unchanged': True,
                                      'existing_output_rejected': True, 'invalid_request_rejected': True}))


if __name__ == '__main__':
    asyncio.run(main())
