"""Real MCP coverage for installed native and IPC addon adapters on disposable boards."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path('/projects/addon-canary')
BOARD = ROOT / 'source.kicad_pcb'

MAKE_BOARD = r'''
import pcbnew,sys
b=pcbnew.BOARD()
net=pcbnew.NETINFO_ITEM(b,'GND');b.Add(net)
fp=pcbnew.FootprintLoad('/usr/share/kicad/footprints/Resistor_SMD.pretty','R_0603_1608Metric')
fp.SetReference('R1');fp.SetValue('10k');fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(5.13),pcbnew.FromMM(5.27)));b.Add(fp)
bga=pcbnew.FootprintLoad('/usr/share/kicad/footprints/Package_BGA.pretty','BGA-16_1.92x1.92mm_Layout4x4_P0.5mm')
bga.SetReference('U1');bga.SetValue('BGA');bga.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(15),pcbnew.FromMM(15)))
for pad in bga.Pads(): pad.SetNet(net)
b.Add(bga)
for start,end in [((5,10),(20,10)),((20,10),(20,20))]:
 t=pcbnew.PCB_TRACK(b);t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]),pcbnew.FromMM(start[1])));t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]),pcbnew.FromMM(end[1])));t.SetWidth(pcbnew.FromMM(.25));t.SetLayer(pcbnew.F_Cu);t.SetNet(net);b.Add(t)
d=pcbnew.PCB_SHAPE(b);d.SetShape(pcbnew.SHAPE_T_SEGMENT);d.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(2),pcbnew.FromMM(2)));d.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(8),pcbnew.FromMM(2)));d.SetWidth(pcbnew.FromMM(.2));d.SetLayer(pcbnew.F_SilkS);b.Add(d)
for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
 z=pcbnew.ZONE(b);z.SetLayer(layer);z.SetNet(net);outline=z.Outline();outline.NewOutline()
 for x,y in [(3,7),(27,7),(27,22),(3,22)]: outline.Append(pcbnew.FromMM(x),pcbnew.FromMM(y))
 b.Add(z)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
for start,end in [((0,0),(30,0)),((30,0),(30,25)),((30,25),(0,25)),((0,25),(0,0))]:
 e=pcbnew.PCB_SHAPE(b);e.SetShape(pcbnew.SHAPE_T_SEGMENT);e.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]),pcbnew.FromMM(start[1])));e.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]),pcbnew.FromMM(end[1])));e.SetWidth(pcbnew.FromMM(.05));e.SetLayer(pcbnew.Edge_Cuts);b.Add(e)
pcbnew.SaveBoard(sys.argv[1],b)
'''

IDS = r'''
import pcbnew,json,sys
b=pcbnew.LoadBoard(sys.argv[1])
def uid(x):
 v=x.m_Uuid
 return v.AsString() if hasattr(v,'AsString') else str(v)
print(json.dumps({'tracks':[uid(x) for x in b.GetTracks() if isinstance(x,pcbnew.PCB_TRACK)],'drawings':[uid(x) for x in b.GetDrawings() if x.GetLayer()==pcbnew.F_SilkS],'mask_drawings':[uid(x) for x in b.GetDrawings() if x.GetLayer() in (pcbnew.F_Mask,pcbnew.B_Mask)]}))
'''

NETLIST = '''<?xml version="1.0" encoding="UTF-8"?>
(export (version "E")
  (design (source "demo.kicad_sch") (date "2026-01-01") (tool "Eeschema 10.0.8"))
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0603_1608Metric")
      (datasheet "~") (fields (field (name "Description") "Resistor"))
      (libsource (lib "Device") (part "R") (description "Resistor"))
      (sheetpath (names "/") (tstamps "/")) (tstamps "1")))
  (libparts
    (libpart (lib "Device") (part "R") (description "Resistor")
      (fields (field (name "Reference") "R") (field (name "Value") "R"))
      (pins (pin (num "1") (name "~") (type "passive"))
            (pin (num "2") (name "~") (type "passive")))))
  (libraries (library (logical "Device") (uri "/usr/share/kicad/symbols/Device.kicad_sym")))
  (nets (net (code "1") (name "GND") (node (ref "R1") (pin "1")))))
'''

BOM_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<export><design><source>demo.kicad_sch</source><date>2026-01-01</date><tool>Eeschema 10.0.8</tool></design>
<components><comp ref="R1"><value>10k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
<datasheet>~</datasheet><fields><field name="Description">Resistor</field></fields>
<libsource lib="Device" part="R" description="Resistor"/></comp></components></export>
'''

async def call(session, name, args):
    result = await session.call_tool(name, args)
    text = '\n'.join(x.text for x in result.content if x.type == 'text')
    assert not result.is_error, (name, text)
    structured = result.model_dump(by_alias=True).get('structuredContent')
    return structured if structured is not None else json.loads(text)

def output_board(result):
    return next(Path(row['path']) for row in result['files'] if row['path'].endswith('.kicad_pcb'))

def board_ids(path):
    return json.loads(subprocess.check_output(['/opt/kicad-tools/bin/python','-c',IDS,str(path)],text=True))

async def main():
    ROOT.mkdir()
    subprocess.run(['/opt/kicad-tools/bin/python','-c',MAKE_BOARD,str(BOARD)],check=True)
    rules=BOARD.with_suffix('.kicad_dru');rules.write_text('(version 1)\n(rule "KEEP_ME" (constraint clearance (min 0.2mm)))\n')
    source_sha = hashlib.sha256(BOARD.read_bytes()).hexdigest()
    rules_sha = hashlib.sha256(rules.read_bytes()).hexdigest()
    ids=board_ids(BOARD)
    async with stdio_client(StdioServerParameters(command='/usr/local/bin/kicad-mcp')) as (read,write):
      async with ClientSession(read,write) as session:
        await session.initialize()
        names={x.name for x in (await session.list_tools()).tools}
        assert {'run_pcb_action_tool','apply_via_fence','run_rf_tool','place_kobee_artwork','separate_pcb','create_stencil',
                'present_pcb','search_skidl_parts','convert_netlist_to_skidl',
                'generate_offline_cost_spreadsheet'} <= names
        ann=await call(session,'run_pcb_action_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'annular'),'operation':{'kind':'annular_check'}})
        assert any(x['path'].endswith('_AR-Check.json') for x in ann['files'])
        missing=await call(session,'run_pcb_action_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'missing3d'),'operation':{'kind':'missing_3d'}})
        assert sum(x['path'].endswith('missing3Dmodels.txt') for x in missing['files'])==2, missing
        snap=await call(session,'run_pcb_action_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'snap'),'operation':{'kind':'snap_to_grid','references':['R1'],'grid_mm':1,'origin':'grid'}})
        assert snap['operation']['references']==['R1']
        moved=await call(session,'run_pcb_action_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'move'),'operation':{'kind':'move_drawings','drawing_uuids':ids['drawings'],'layer':'B.Silkscreen'}})
        assert moved['operation']['layer']=='B.Silkscreen'
        length=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'length'),'operation':{'kind':'track_length','track_uuids':[ids['tracks'][0]]}})
        assert length['operation']['length_mm'] > 0, length
        clearance=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'clearance'),'operation':{'kind':'trace_clearance','track_uuids':[ids['tracks'][0]],'clearance_mm':0.3}})
        assert clearance['operation']['after'] != clearance['operation']['before'], clearance
        rounded=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'rounded'),'operation':{'kind':'round_tracks','track_uuids':ids['tracks'],'distance_mm':1,'segments':8}})
        assert rounded['operation']['after'] != rounded['operation']['before'], rounded
        rounded_board=output_board(rounded); rounded_ids=board_ids(rounded_board)
        unrounded=await call(session,'run_rf_tool',{'pcb_path':str(rounded_board),'output_dir':str(ROOT/'unrounded'),'operation':{'kind':'remove_rounding','track_uuids':rounded_ids['tracks']}})
        assert unrounded['operation']['removed_rounding_items'] > 0, unrounded
        reconnected=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'reconnected'),'operation':{'kind':'reconnect_tracks','track_uuids':ids['tracks']}})
        assert output_board(reconnected).is_file()
        mask=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'mask'),'operation':{'kind':'expand_solder_mask','track_uuids':[ids['tracks'][0]],'clearance_mm':0.2}})
        assert mask['operation']['after'] != mask['operation']['before'], mask
        mask_board=output_board(mask); mask_ids=board_ids(mask_board)
        unmask=await call(session,'run_rf_tool',{'pcb_path':str(mask_board),'output_dir':str(ROOT/'unmask'),'operation':{'kind':'remove_solder_mask','drawing_uuids':mask_ids['mask_drawings']}})
        assert unmask['operation']['removed_mask_items'] > 0, unmask
        taper=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'taper'),'operation':{'kind':'add_taper','track_uuids':[ids['tracks'][0]],'pads':[{'reference':'R1','pad':'1'}]}})
        assert taper['operation']['after'] != taper['operation']['before'], taper
        taper_board=output_board(taper)
        untaper=await call(session,'run_rf_tool',{'pcb_path':str(taper_board),'output_dir':str(ROOT/'untaper'),'operation':{'kind':'remove_tapers','confirm_all':True}})
        assert untaper['operation']['removed_tapers'] > 0, untaper
        rf_fence=await call(session,'run_rf_tool',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'rf-fence'),'operation':{'kind':'via_fence','track_uuids':[ids['tracks'][0]],'net_name':'GND','via_diameter_mm':0.6,'drill_mm':0.3,'pitch_mm':2,'offset_mm':1}})
        assert rf_fence['operation']['placed_vias'] > 0, rf_fence
        fence=await call(session,'apply_via_fence',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'viafence'),'track_uuids':[ids['tracks'][0]],'net_name':'GND','spacing_mm':2,'gap_mm':1,'via_diameter_mm':0.6,'drill_mm':0.3})
        assert fence['operation']['placed_vias'] > 0, fence
        silk=await call(session,'edit_silk_references',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'silk'),'scope':'ref','scope_pattern':'R1','do_center':True,'placement':'center'})
        assert silk['operation'], silk
        z0=await call(session,'apply_impedance_netclasses',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'z0'),'proposals':{'Default':{'track_width':0.3}},'action':'apply'})
        assert z0['operation']['changes'], z0
        nccm=await call(session,'update_clearance_matrix',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'nccm'),'clearances':[{'first':'Default','second':'Default','clearance_mm':0.25}],'action':'apply'})
        assert nccm['operation']['rule_count']==1, nccm
        assert 'KEEP_ME' in (Path(nccm['output_dir'])/'source.kicad_dru').read_text(), nccm
        kobee=await call(session,'place_kobee_label',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'kobee'),'text':'MCP','x_mm':10,'y_mm':5,'layer':'F.SilkS','layers':['F.SilkS']})
        assert kobee['operation']['primitive_count'] > 0, kobee
        qr=await call(session,'place_kobee_artwork',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'kobee-qr'),'feature':{'kind':'machine_code','payload':'https://example.invalid/board','x_mm':10,'y_mm':5}})
        assert qr['operation']['primitive_count'] > 0, qr
        header=await call(session,'place_kobee_artwork',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'kobee-header'),'feature':{'kind':'pin_header','pin_labels':['GND','VCC','SDA','SCL'],'x_mm':10,'y_mm':5}})
        assert header['operation']['primitive_count'] > 0, header
        callout=await call(session,'place_kobee_artwork',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'kobee-callout'),'feature':{'kind':'component_callout','title':'R1','subtitle':'10k','x_mm':10,'y_mm':5}})
        assert callout['operation']['primitive_count'] > 0, callout
        barcode=await call(session,'place_kobee_artwork',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'kobee-code128'),'feature':{'kind':'machine_code','code_type':'code128','payload':'MCP-147','module_size_mm':0.25,'bar_height_mm':4,'x_mm':10,'y_mm':5}})
        assert barcode['operation']['primitive_count'] > 0, barcode
        fanout=await call(session,'fanout_footprint',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'fanout'),'reference':'U1','package':'BGA','alignment':'Quadrant','direction':'','connected_only':False})
        assert fanout['operation']['created_items'] > 0, fanout
        stitch=await call(session,'stitch_vias',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'stitch'),'net_name':'GND','action':'stitch','spacing_mm':3,'pattern':'Square'})
        assert stitch['operation']['placed_vias'] > 0, stitch
        stitch_plan=await call(session,'stitch_vias',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'stitch-plan'),'net_name':'GND','action':'plan','spacing_mm':3,'pattern':'Square'})
        assert stitch_plan['operation']['planned_vias'] > 0, stitch_plan
        buzz=await call(session,'insert_kibuzzard_footprint',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'buzz'),'reference':'TXT1','x_mm':10,'y_mm':4,'text':'MCP'})
        assert buzz['operation']['reference']=='TXT1', buzz
        notes_source=ROOT/'notes-source';notes_source.mkdir();(notes_source/'demo.kicad_pro').write_text('{}')
        notes=await call(session,'manage_project_notes',{'project_dir':str(notes_source),'output_dir':str(ROOT/'notes-output'),'action':'save_notes','notes':'verified'})
        assert any(x['path'].endswith('.md') and '.kinotes/' in x['path'] for x in notes['files']), notes
        separated=await call(session,'separate_pcb',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'separate'),'top_left_x_mm':0,'top_left_y_mm':0,'bottom_right_x_mm':30,'bottom_right_y_mm':25})
        assert output_board(separated).is_file()
        stencil=await call(session,'create_stencil',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'stencil'),'kind':'steel'})
        assert stencil['files'], stencil
        printed_stencil=await call(session,'create_stencil',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'stencil-printed'),'kind':'printed'})
        assert printed_stencil['files'], printed_stencil
        presentation=await call(session,'present_pcb',{'pcb_path':str(BOARD),'output_dir':str(ROOT/'present'),'name':'Addon canary','description':'Offline presentation'})
        assert any(x['path'].endswith('.html') for x in presentation['files']), presentation
        search=await call(session,'search_skidl_parts',{'terms':'Device R','limit':5})
        assert search['count'] > 0, search
        netlist=ROOT/'demo.net';netlist.write_text(NETLIST)
        converted=await call(session,'convert_netlist_to_skidl',{'netlist_path':str(netlist),'output_dir':str(ROOT/'skidl')})
        assert any(x['path'].endswith('.py') for x in converted['files']), converted
        bom=ROOT/'demo.xml';bom.write_text(BOM_XML)
        cost=await call(session,'generate_offline_cost_spreadsheet',{'bom_xml_path':str(bom),'output_dir':str(ROOT/'cost'),'board_quantity':10,'currency':'USD'})
        assert any(x['path'].endswith('.xlsx') for x in cost['files']), cost
    assert hashlib.sha256(BOARD.read_bytes()).hexdigest()==source_sha
    assert hashlib.sha256(rules.read_bytes()).hexdigest()==rules_sha
    print(json.dumps({'tools':len(names),'addon_native':True,'addon_ipc':True,'source_unchanged':True}))

asyncio.run(main())
