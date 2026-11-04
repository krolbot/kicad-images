"""Real tools/list + tools/call checks inside the candidate image; no Lab access."""
import asyncio
import base64
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


JOBSET_JOBS = [
    {
        'description': 'Export board fabrication layers',
        'id': '11111111-2222-4333-8444-555555555555',
        'settings': {
            'black_and_white': True,
            'check_zones': True,
            'create_gerber_job_file': True,
            'crossout_dnp_footprints_on_fab_layers': True,
            'description': '',
            'disable_aperture_macros': False,
            'drawing_sheet': '',
            'drill_shape': 'full',
            'hide_dnp_footprints_on_fab_layers': False,
            'include_netlist_attributes': True,
            'layers': ['F.Cu', 'B.Cu', 'F.Mask', 'B.Mask', 'F.SilkS', 'B.SilkS', 'Edge.Cuts'],
            'layers_to_include_on_all_layers': [],
            'mirror': False,
            'negative': False,
            'output_filename': 'gerbers',
            'plot_drawing_sheet': False,
            'plot_footprint_values': True,
            'plot_pad_numbers': False,
            'plot_ref_des': True,
            'precision': 5,
            'sketch_dnp_footprints_on_fab_layers': True,
            'sketch_pads_on_fab_layers': False,
            'subtract_solder_mask_from_silk': False,
            'use_drill_origin': False,
            'use_protel_file_extension': False,
            'use_x2_format': True,
        },
        'type': 'pcb_export_gerbers',
    },
    {
        'description': 'Export drill files',
        'id': '22222222-3333-4444-8555-666666666666',
        'settings': {
            'description': '',
            'drill_origin': 'abs',
            'excellon.combine_pth_npth': True,
            'excellon.minimal_header': False,
            'excellon.mirror_y': False,
            'excellon.oval_drill_route': False,
            'format': 'excellon',
            'generate_map': False,
            'generate_report': False,
            'generate_tenting': False,
            'gerber_precision': 5,
            'map_format': 'pdf',
            'output_dir': 'drill',
            'report_filename': '',
            'units': 'mm',
            'zero_format': 'decimal',
        },
        'type': 'pcb_export_drill',
    },
    {
        'description': 'Export schematic PDF',
        'id': '33333333-4444-4555-8666-777777777777',
        'settings': {
            'black_and_white': False,
            'color_theme': 'KiCad Default',
            'description': '',
            'drawing_sheet': '',
            'format': 'pdf',
            'hpgl_page_size': 'default',
            'hpgl_pen_size': 1.016,
            'hpgl_plot_origin': 'A3',
            'min_pen_width': 847,
            'output_dir': '',
            'page_size': 'auto',
            'pdf_hierarchical_links': True,
            'pdf_metadata': True,
            'pdf_property_popups': True,
            'plot_all': True,
            'plot_drawing_sheet': True,
            'use_background_color': True,
        },
        'type': 'sch_export_plot_pdf',
    },
]


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
                    required = {'get_toolkit_status', 'manufacture_pcb', 'generate_interactive_bom', 'panelize_pcb', 'render_pcb', 'discover_replication', 'replicate_layout', 'place_footprints', 'place_parts', 'scan_project_dependencies', 'localize_project_dependencies', 'export_testpoints', 'generate_pinout', 'generate_pinout_image', 'generate_kibuzzard_footprint', 'create_footprint', 'edit_footprint', 'download_project_file', 'apply_variant', 'solve_trace_impedance', 'run_prefab_gate', 'get_board_edit_state', 'edit_board_copper_batch', 'autoroute_pcb', 'run_drc', 'get_symbol_info', 'list_project_files', 'stat_project_path', 'create_project_directory', 'upload_project_file', 'copy_project_path', 'move_project_path', 'delete_project_path', 'create_project_archive', 'extract_project_archive', 'get_schematic_items', 'edit_schematic_items', 'get_board_items', 'edit_board_items', 'get_board_section', 'replace_board_section', 'get_footprint_definition', 'replace_footprint_definition', 'get_project_settings', 'replace_project_settings', 'get_project_library_table', 'replace_project_library_table', 'get_design_rules', 'replace_design_rules', 'create_sym_lib_table', 'flatten_hierarchy', 'run_jobset'}
                    assert required <= names, sorted(required - names)
                    schemas = {tool.name: tool.input_schema for tool in page.tools}
                    for guarded in ['create_sym_lib_table', 'flatten_hierarchy', 'run_jobset']:
                        assert 'expected_sha256' in schemas[guarded]['required'], guarded
                    for guarded in ['autoroute_pcb', 'export_3d', 'export_bom', 'export_footprint_svg',
                                    'export_gerbers', 'export_hierarchical_netlist', 'export_netlist',
                                    'export_pcb', 'export_positions', 'export_schematic', 'export_symbol_svg',
                                    'list_unconnected_pins', 'run_drc', 'run_erc']:
                        assert 'output_dir' in schemas[guarded]['required'], guarded
                    assert 'output' in schemas['export_ipc2581']['required']
                    assert 'expected_sheet_sha256' in schemas['add_hierarchical_sheet']['required']
                    assert 'publication' in schemas['update_pcb_from_schematic']['required']
                    assert 'expected_sha256' not in schemas['update_pcb_from_schematic']['required']
                    resources = await session.list_resources()
                    templates = await session.list_resource_templates()
                    assert len(resources.resources) == 1 and len(templates.resource_templates) == 1
                    status = await checked(session, 'get_toolkit_status', {})
                    assert status['kicad'] == os.environ['KICAD_VERSION'], status
                    manufacturing = await checked(session, 'manufacture_pcb', {'pcb_path': str(board), 'output_dir': str(root / 'fabrication')})
                    fab = artifacts(manufacturing)
                    for suffix in ['.gbr', '.drl', '.csv', '.pdf']:
                        assert any(p.suffix == suffix and p.stat().st_size for p in fab), suffix
                    legacy_drc_dir = root / 'legacy-drc'
                    await checked(session, 'run_drc', {
                        'pcb_path': str(board), 'output_dir': str(legacy_drc_dir)})
                    assert any(p.is_file() and p.stat().st_size for p in legacy_drc_dir.rglob('*'))
                    legacy_gerbers_dir = root / 'legacy-gerbers'
                    await checked(session, 'export_gerbers', {
                        'pcb_path': str(board), 'output_dir': str(legacy_gerbers_dir)})
                    assert any(p.suffix == '.gbr' and p.stat().st_size for p in legacy_gerbers_dir.rglob('*'))
                    legacy_ipc = root / 'legacy-ipc2581.xml'
                    await checked(session, 'export_ipc2581', {
                        'pcb_path': str(board), 'output': str(legacy_ipc)})
                    assert legacy_ipc.is_file() and legacy_ipc.stat().st_size
                    duplicate_legacy = await session.call_tool('run_drc', {
                        'pcb_path': str(board), 'output_dir': str(legacy_drc_dir)})
                    assert duplicate_legacy.is_error, 'Legacy output publication must not replace an existing path'
                    demo = root / 'demo-source'
                    shutil.copytree('/usr/share/kicad/template/Arduino_Micro', demo)
                    demo_board = next(demo.glob('*.kicad_pcb'))
                    demo_project = demo_board.with_suffix('.kicad_pro')
                    demo_project_before = hashlib.sha256(demo_project.read_bytes()).hexdigest()
                    await checked(session, 'set_net_class', {
                        'name': 'MCP_CANARY', 'nets': ['GND'], 'track_width': 0.5,
                        'pcb_path': str(demo_board), 'expected_sha256': demo_project_before})
                    assert hashlib.sha256(demo_project.read_bytes()).hexdigest() != demo_project_before
                    extended = await checked(session, 'manufacture_pcb', {'pcb_path': str(demo_board),
                        'output_dir': str(root / 'extended-fabrication'),
                        'outputs': ['bom', 'netlist_ipc', 'ipc2581', 'step', 'svg']})
                    extended_suffixes = {p.suffix.lower() for p in artifacts(extended)}
                    assert {'.csv', '.d356', '.zip', '.step', '.svg'} <= extended_suffixes, extended_suffixes
                    bom = await checked(session, 'generate_interactive_bom', {'pcb_path': str(board), 'output_dir': str(root / 'assembly')})
                    html = [p for p in artifacts(bom) if p.suffix == '.html']
                    assert len(html) == 1 and 'pcbdata' in html[0].read_text()
                    panel = await checked(session, 'panelize_pcb', {'pcb_path': str(board),
                        'output_dir': str(root / 'panel'), 'rows': 1, 'columns': 2,
                        'horizontal_spacing_mm': 4, 'vertical_spacing_mm': 2,
                        'rotation_deg': 180, 'alternation': 'rows',
                        'horizontal_tab_count': 2, 'vertical_tab_count': 1,
                        'cuts': 'vcuts', 'framing': 'railstb',
                        'frame_width_mm': 5, 'frame_spacing_mm': 2})
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
                    created = await checked(session, 'create_footprint', {
                        'output_dir': str(root / 'created-footprint'), 'name': 'MCP_Mixed_Pads',
                        'description': 'Native MCP footprint authoring canary', 'keywords': 'mcp smd pth npth slot',
                        'pads': [
                            {'number': '1', 'kind': 'smd', 'shape': 'roundrect', 'x_mm': 0, 'y_mm': 0,
                             'size_x_mm': 1.2, 'size_y_mm': 2.0, 'roundrect_ratio': 0.2},
                            {'number': '2', 'kind': 'thru_hole', 'shape': 'circle', 'x_mm': 3, 'y_mm': 0,
                             'size_x_mm': 2.2, 'size_y_mm': 2.2, 'drill_x_mm': 1.0},
                            {'number': '', 'kind': 'np_thru_hole', 'shape': 'oval', 'x_mm': 6, 'y_mm': 0,
                             'size_x_mm': 2.0, 'size_y_mm': 3.4, 'drill_x_mm': 0.9, 'drill_y_mm': 2.3},
                        ]})
                    created_mod = next(p for p in artifacts(created) if p.suffix == '.kicad_mod')
                    created_pads = created['operation']['pads']
                    created_by_number = {pad['number']: pad for pad in created_pads}
                    assert len(created_pads) == 3 and created_by_number['1']['kind'] == 'smd'
                    assert created_by_number['2']['kind'] == 'thru_hole'
                    assert created_by_number['']['kind'] == 'np_thru_hole'
                    assert created_by_number['2']['drill_shape'] == 'circle'
                    assert created_by_number['']['drill_shape'] == 'slot'
                    stock_footprint = Path('/usr/share/kicad/footprints/Connector_PinHeader_2.54mm.pretty/PinHeader_1x02_P2.54mm_Vertical.kicad_mod')
                    source_footprint = root / 'source-footprint.kicad_mod'
                    shutil.copy2(stock_footprint, source_footprint)
                    source_before = hashlib.sha256(source_footprint.read_bytes()).hexdigest()
                    edited = await checked(session, 'edit_footprint', {
                        'footprint_path': str(source_footprint), 'output_dir': str(root / 'edited-footprint'),
                        'description': 'Edited without losing the original graphics or model',
                        'operations': [
                            {'kind': 'replace', 'number': '1', 'pad': {
                                'number': '1', 'kind': 'thru_hole', 'shape': 'oval', 'x_mm': 0, 'y_mm': 0,
                                'size_x_mm': 2.4, 'size_y_mm': 3.4, 'drill_x_mm': 0.9, 'drill_y_mm': 2.2}},
                            {'kind': 'delete', 'number': '2'},
                            {'kind': 'add', 'pad': {
                                'number': '', 'kind': 'np_thru_hole', 'shape': 'oval', 'x_mm': 3, 'y_mm': 1.27,
                                'size_x_mm': 2.0, 'size_y_mm': 3.0, 'drill_x_mm': 0.8, 'drill_y_mm': 2.1}},
                        ]})
                    edited_mod = next(p for p in artifacts(edited) if p.suffix == '.kicad_mod')
                    operation = edited['operation']
                    assert operation['changes'] == {'added': 1, 'replaced': 1, 'deleted': 1}
                    assert operation['source_unchanged'] is True and operation['pad_count'] == 2
                    assert operation['graphical_item_count'] > 0 and operation['model_count'] > 0
                    assert [pad['drill_shape'] for pad in operation['pads']] == ['slot', 'slot']
                    assert hashlib.sha256(source_footprint.read_bytes()).hexdigest() == source_before
                    verify = ("import pcbnew,sys,pathlib;path=pathlib.Path(sys.argv[1]);"
                              "io=pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP);"
                              "f=io.FootprintLoad(str(path.parent),path.stem,True);p=list(f.Pads());"
                              "assert len(p)==2 and all(x.GetDrillShape()==pcbnew.PAD_DRILL_SHAPE_OBLONG for x in p);"
                              "assert any(x.GetAttribute()==pcbnew.PAD_ATTRIB_NPTH for x in p)")
                    subprocess.run(['/opt/kicad-tools/bin/python', '-c', verify, str(edited_mod)], check=True)
                    assert created_mod.is_file()

                    lifecycle_dir = root / 'file-lifecycle'
                    await checked(session, 'create_project_directory', {'path': str(lifecycle_dir)})
                    payload = b'KiCad MCP exact upload\x00\xff'
                    uploaded_path = lifecycle_dir / 'uploaded.bin'
                    uploaded = await checked(session, 'upload_project_file', {
                        'path': str(uploaded_path), 'content_base64': base64.b64encode(payload).decode()})
                    assert uploaded_path.read_bytes() == payload
                    stat_result = await checked(session, 'stat_project_path', {'path': str(uploaded_path)})
                    assert stat_result['sha256'] == hashlib.sha256(payload).hexdigest()
                    copy_path = lifecycle_dir / 'copy.bin'
                    await checked(session, 'copy_project_path', {'source': str(uploaded_path),
                        'destination': str(copy_path), 'expected_sha256': stat_result['sha256']})
                    moved_path = lifecycle_dir / 'moved.bin'
                    await checked(session, 'move_project_path', {'source': str(copy_path),
                        'destination': str(moved_path), 'expected_sha256': stat_result['sha256']})
                    listing = await checked(session, 'list_project_files', {
                        'directory': str(lifecycle_dir), 'recursive': True})
                    assert {row['name'] for row in listing['entries']} == {'uploaded.bin', 'moved.bin'}
                    lifecycle_digest = (await checked(session, 'stat_project_path', {'path': str(lifecycle_dir)}))['sha256']
                    archive_path = root / 'file-lifecycle.zip'
                    archive = await checked(session, 'create_project_archive', {'source_path': str(lifecycle_dir),
                        'output_path': str(archive_path), 'expected_sha256': lifecycle_digest})
                    extracted_dir = root / 'file-lifecycle-extracted'
                    extracted = await checked(session, 'extract_project_archive', {'archive_path': str(archive_path),
                        'output_dir': str(extracted_dir), 'expected_sha256': archive['sha256']})
                    assert (extracted_dir / 'file-lifecycle/uploaded.bin').read_bytes() == payload
                    await checked(session, 'delete_project_path', {'path': str(moved_path),
                        'expected_sha256': stat_result['sha256'], 'confirm': True})

                    footprint_definition = await checked(session, 'get_footprint_definition', {
                        'footprint_path': str(created_mod)})
                    footprint_sexpr = footprint_definition['sexpr'].replace(
                        'Native MCP footprint authoring canary', 'Full fidelity MCP footprint canary')
                    full_footprint = await checked(session, 'replace_footprint_definition', {
                        'footprint_path': str(created_mod), 'expected_sha256': footprint_definition['sha256'],
                        'sexpr': footprint_sexpr})
                    assert 'Full fidelity MCP footprint canary' in full_footprint['sexpr']

                    editable_board = root / 'editable.kicad_pcb'
                    shutil.copy2(board, editable_board)
                    board_state = await checked(session, 'get_board_items', {'pcb_path': str(editable_board)})
                    board_edit = await checked(session, 'edit_board_items', {'pcb_path': str(editable_board),
                        'expected_sha256': board_state['sha256'], 'operations': [{
                            'action': 'add', 'item_type': 'gr_line',
                            'sexpr': '(gr_line (start 2 2) (end 8 2) (stroke (width 0.2) (type default)) '
                                     '(layer "F.SilkS") (uuid 00000000-0000-0000-0000-000000000000))'}]})
                    added_uuid = board_edit['operations'][0]['uuid']
                    assert any(row['uuid'] == added_uuid for row in board_edit['items'])
                    setup = await checked(session, 'get_board_section', {'pcb_path': str(editable_board), 'section': 'setup'})
                    setup_result = await checked(session, 'replace_board_section', {'pcb_path': str(editable_board),
                        'section': 'setup', 'expected_sha256': setup['sha256'], 'sexpr': setup['sexpr']})
                    board_delete = await checked(session, 'edit_board_items', {'pcb_path': str(editable_board),
                        'expected_sha256': setup_result['sha256'], 'operations': [{
                            'action': 'delete', 'item_type': 'gr_line', 'uuid': added_uuid}]})
                    assert not any(row['uuid'] == added_uuid for row in board_delete['items'])

                    full_project = root / 'full-project'
                    created_project = await session.call_tool('create_project', {
                        'directory': str(full_project), 'name': 'full'})
                    assert not created_project.is_error
                    schematic = full_project / 'full.kicad_sch'
                    child_schematic = full_project / 'child.kicad_sch'
                    await checked(session, 'create_schematic', {'schematic_path': str(child_schematic)})
                    parent_before_sheet = hashlib.sha256(schematic.read_bytes()).hexdigest()
                    child_before_sheet = hashlib.sha256(child_schematic.read_bytes()).hexdigest()
                    await checked(session, 'add_hierarchical_sheet', {
                        'parent_schematic_path': str(schematic), 'sheet_name': 'Child',
                        'sheet_file': child_schematic.name,
                        'pins': [{'name': 'DATA', 'direction': 'input'}],
                        'project_path': str(full_project / 'full.kicad_pro'),
                        'expected_sha256': parent_before_sheet,
                        'expected_sheet_sha256': child_before_sheet})
                    assert hashlib.sha256(schematic.read_bytes()).hexdigest() != parent_before_sheet
                    assert hashlib.sha256(child_schematic.read_bytes()).hexdigest() != child_before_sheet
                    nested_duplicate = await session.call_tool('duplicate_sheet', {
                        'schematic_path': str(schematic),
                        'sheet_uuid': '00000000-0000-0000-0000-000000000000',
                        'new_sheet_name': 'Escape',
                        'new_file_name': 'out/escape.kicad_sch',
                        'expected_sha256': hashlib.sha256(schematic.read_bytes()).hexdigest(),
                    })
                    assert nested_duplicate.is_error
                    assert not (full_project / 'out' / 'escape.kicad_sch').exists()
                    schematic_state = await checked(session, 'get_schematic_items', {'schematic_path': str(schematic)})
                    schematic_edit = await checked(session, 'edit_schematic_items', {
                        'schematic_path': str(schematic), 'expected_sha256': schematic_state['sha256'],
                        'operations': [{'action': 'add', 'item_type': 'bus',
                            'sexpr': '(bus (pts (xy 20 20) (xy 40 20)) (stroke (width 0) (type default)) '
                                     '(uuid 00000000-0000-0000-0000-000000000000))'}]})
                    assert any(row['item_type'] == 'bus' for row in schematic_edit['items'])
                    flattened = full_project / 'full-flat.kicad_sch'
                    flatten_result = await session.call_tool('flatten_hierarchy', {
                        'schematic_path': str(schematic), 'output_path': str(flattened),
                        'expected_sha256': schematic_edit['sha256']})
                    assert not flatten_result.is_error and flattened.is_file()
                    escaped_hierarchy = full_project / 'escaped-hierarchy.kicad_sch'
                    escaped_hierarchy.write_text(
                        '(kicad_sch (sheet (property "Sheetfile" "../../../../etc/host.kicad_sch")))')
                    escaped_result = await session.call_tool('list_hierarchy', {
                        'schematic_path': str(escaped_hierarchy)})
                    assert escaped_result.is_error, 'Embedded hierarchy traversal must be rejected'

                    project_file = full_project / 'full.kicad_pro'
                    project_settings = await checked(session, 'get_project_settings', {'project_path': str(project_file)})
                    settings_payload = project_settings['settings']
                    settings_payload['text_variables'] = {'MCP_CANARY': 'verified'}
                    replaced_settings = await checked(session, 'replace_project_settings', {
                        'project_path': str(project_file), 'expected_sha256': project_settings['sha256'],
                        'settings': settings_payload})
                    assert replaced_settings['settings']['text_variables']['MCP_CANARY'] == 'verified'
                    fp_table = await checked(session, 'replace_project_library_table', {'directory': str(full_project),
                        'kind': 'fp', 'sexpr': '(fp_lib_table (version 7) (lib (name "Local") (type "KiCad") '
                            '(uri "${KIPRJMOD}/Local.pretty") (options "") (descr "local")))'})
                    assert (await checked(session, 'get_project_library_table', {
                        'directory': str(full_project), 'kind': 'fp'}))['sha256'] == fp_table['sha256']
                    directory_state = await checked(session, 'stat_project_path', {'path': str(full_project)})
                    sym_table = await session.call_tool('create_sym_lib_table', {
                        'directory': str(full_project),
                        'entries': [{'name': 'Local', 'uri': '${KIPRJMOD}/Local.kicad_sym'}],
                        'expected_sha256': directory_state['sha256']})
                    assert not sym_table.is_error
                    assert '${KIPRJMOD}/Local.kicad_sym' in (full_project / 'sym-lib-table').read_text()
                    rules_path = full_project / 'full.kicad_dru'
                    rules_content = b'(version 1)\n(rule "mcp" (constraint clearance (min 0.2mm)))\n'
                    await checked(session, 'upload_project_file', {'path': str(rules_path),
                        'content_base64': base64.b64encode(rules_content).decode()})
                    rules = await checked(session, 'get_design_rules', {'rules_path': str(rules_path)})
                    replaced_rules = await checked(session, 'replace_design_rules', {'rules_path': str(rules_path),
                        'expected_sha256': rules['sha256'], 'sexpr': rules['sexpr'].replace('0.2mm', '0.25mm')})
                    assert '0.25mm' in replaced_rules['sexpr']
                    jobset_project = root / 'jobset-project'
                    shutil.copytree('/opt/kicad-build/replication-fixture', jobset_project)
                    jobset = jobset_project / 'replicate_layout_test_project.kicad_jobset'
                    jobset_schematic = jobset.with_suffix('.kicad_sch')
                    jobset_project_file = jobset.with_suffix('.kicad_pro')
                    created_board = jobset_project / 'created-from-schematic.kicad_pcb'
                    await checked(session, 'update_pcb_from_schematic', {
                        'schematic_path': str(jobset_schematic), 'pcb_path': str(created_board),
                        'project_path': str(jobset_project_file), 'publication': {'mode': 'create'}})
                    assert created_board.is_file() and created_board.stat().st_size
                    subprocess.run(['/opt/kicad-tools/bin/python', '-c',
                        'import pcbnew,sys;b=pcbnew.LoadBoard(sys.argv[1]);assert b is not None',
                        str(created_board)], check=True)
                    await checked(session, 'update_pcb_from_schematic', {
                        'schematic_path': str(jobset_schematic), 'pcb_path': str(created_board),
                        'project_path': str(jobset_project_file), 'publication': {
                            'mode': 'update',
                            'expected_sha256': hashlib.sha256(created_board.read_bytes()).hexdigest(),
                        }})
                    jobset.write_text(json.dumps({
                        'meta': {'version': 1},
                        'jobs': JOBSET_JOBS,
                        'outputs': [{
                            'description': 'MCP confined output',
                            'id': '66666666-7777-4888-8999-aaaaaaaaaaaa',
                            'only': [], 'settings': {'output_path': 'jobset-outputs'},
                            'type': 'folder',
                        }],
                    }))
                    jobset_state = await checked(session, 'stat_project_path', {'path': str(jobset_project)})
                    jobset_result = await session.call_tool('run_jobset', {
                        'jobset_path': str(jobset), 'expected_sha256': jobset_state['sha256']})
                    jobset_text = '\n'.join(item.text for item in jobset_result.content if item.type == 'text')
                    assert not jobset_result.is_error and 'Jobset completed successfully' in jobset_text, jobset_text
                    jobset_output = jobset_project / 'jobset-outputs'
                    jobset_files = [path.relative_to(jobset_output).as_posix()
                                    for path in jobset_output.rglob('*') if path.is_file()]
                    assert any(name.endswith('.pdf') for name in jobset_files), jobset_files
                    assert any(name.endswith(('.gbr', '.gtl', '.gbl', '.gm1')) for name in jobset_files), jobset_files
                    assert any(name.endswith('.drl') for name in jobset_files), jobset_files
                    jobset_output_before = {
                        path.relative_to(jobset_output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in jobset_output.rglob('*') if path.is_file()
                    }
                    rerun_state = await checked(session, 'stat_project_path', {'path': str(jobset_project)})
                    jobset_rerun = await session.call_tool('run_jobset', {
                        'jobset_path': str(jobset), 'expected_sha256': rerun_state['sha256']})
                    assert jobset_rerun.is_error, 'Jobset rerun must not replace existing outputs'
                    assert jobset_output_before == {
                        path.relative_to(jobset_output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in jobset_output.rglob('*') if path.is_file()
                    }
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

                    replication = jobset_project
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
                                      'extended_manufacturing': sorted(extended_suffixes),
                                      'ibom': True, 'panel_footprints': 6, 'pcbdraw': True,
                                      'native_placement_modes': 3, 'parts_placer': True,
                                      'localization_archive': True, 'replication_sections': 3, 'testpoints': True,
                                      'pinout': True, 'pinout_image': True, 'kibuzzard': True,
                                      'native_footprint_create_edit': True, 'footprint_slot_drills': True,
                                      'project_file_lifecycle': True, 'project_resources': True,
                                      'full_fidelity_board_schematic_footprint': True,
                                      'project_settings_tables_rules': True,
                                      'jobset_jobs': len(JOBSET_JOBS), 'jobset_files': len(jobset_files),
                                      'variant': True, 'impedance': True, 'prefab_gate': True,
                                      'native_drc_errors': 0, 'native_drc_warnings': len(drc_warnings),
                                      'source_unchanged': True,
                                      'existing_output_rejected': True, 'invalid_request_rejected': True}))


if __name__ == '__main__':
    asyncio.run(main())
