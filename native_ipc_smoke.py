"""Launch real Pcbnew on a disposable profile and query its native IPC server.

Run under xvfb-run as the application UID. No persistent user settings are edited.
The wizard decisions below match KiCad 10's settings/privacy/library providers.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from mcp_toolkit_smoke import MAKE_BOARD


def main():
    interpreters = sorted(Path('/opt/kicad-addons/python-environments').glob('*/bin/python'))
    interpreters.append(Path('/opt/kicad-tools/bin/python'))
    sdk = next((p for p in interpreters if subprocess.run(
        [str(p), '-c', 'import kipy'], capture_output=True, timeout=15, check=False).returncode == 0), None)
    assert sdk is not None, 'No bundled native IPC SDK can be imported'
    print('Native IPC smoke: SDK available', flush=True)
    expected = tuple(map(int, os.environ['KICAD_VERSION'].split('.')))
    with tempfile.TemporaryDirectory(prefix='native-ipc-smoke-', dir='/projects') as temporary:
        root = Path(temporary)
        config = root / 'home/.config/kicad/10.0'
        config.mkdir(parents=True)
        common = {'api': {'enable_server': True, 'interpreter_path': '/usr/bin/python3'},
                  'do_not_show_again': {'data_collection_prompt': True, 'update_check_prompt': True}}
        (config / 'kicad_common.json').write_text(json.dumps(common))
        (config / 'kicad.json').write_text(json.dumps({'update_check': False, 'pcm': {'update_check': False}}))
        for name in ['fp-lib-table', 'sym-lib-table']:
            shutil.copyfile(Path('/usr/share/kicad/template') / name, config / name)
        (config / 'design-block-lib-table').write_text('(design_block_lib_table (version 7))\n')
        env = {**os.environ, 'HOME': str(root / 'home'), 'XDG_CONFIG_HOME': str(root / 'home/.config'),
               'XDG_CACHE_HOME': str(root / 'home/.cache'), 'KICAD_CONFIG_HOME': str(config.parent),
               'KICAD_DOCUMENTS_HOME': str(root / 'data'), 'PYTHONPATH': ''}
        board = root / 'ipc.kicad_pcb'
        subprocess.run(['/opt/kicad-tools/bin/python', '-c', MAKE_BOARD, str(board)], env=env, check=True, timeout=30)
        print('Native IPC smoke: disposable board created', flush=True)
        probe = ('from kipy import KiCad;import json;v=KiCad(timeout_ms=1000).get_version();'
                 'print(json.dumps([v.major,v.minor,v.patch]))')
        with (root / 'pcbnew.log').open('w') as log:
            child = subprocess.Popen(['pcbnew', str(board)], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                print('Native IPC smoke: PCB editor started', flush=True)
                deadline = time.monotonic() + 90
                last_error = ''
                while time.monotonic() < deadline:
                    assert child.poll() is None, 'Native PCB editor exited before IPC became ready'
                    reply = subprocess.run([str(sdk), '-c', probe], env=env, text=True,
                                           capture_output=True, timeout=5, check=False)
                    if reply.returncode == 0:
                        actual = tuple(json.loads(reply.stdout))
                        assert actual == expected, (actual, expected)
                        print(json.dumps({'native_ipc_version': list(actual), 'pcbnew_gui_alive': True,
                                          'isolated_profile': True}))
                        break
                    last_error = reply.stderr
                    time.sleep(1)
                else:
                    raise AssertionError('Native IPC was not ready: ' + last_error[-1200:]
                                         + (root / 'pcbnew.log').read_text()[-2000:])
            finally:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    main()
