"""Install only the isolated P0 prototype dependencies inside the project."""
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
environment = os.environ.copy()
environment['LIVE_VIEWER_BUILD_NO_WMI'] = '1'
environment['PYTHONPATH'] = str(ROOT / 'tools' / 'build_support')
python = ROOT / '.venv-p0' / 'Scripts' / 'python.exe'
if not python.is_file():
    print('Creating an isolated prototype environment...', flush=True)
    venv.EnvBuilder(with_pip=True).create(ROOT / '.venv-p0')
print('Installing the pinned Qt Widgets runtime...', flush=True)
subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                '-r', str(ROOT / 'prototypes' / 'p0' / ('requirements-build.txt' if '--build' in sys.argv else 'requirements.txt'))],
               cwd=ROOT, env=environment, check=True,
               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
print('P0 environment ready.', flush=True)
