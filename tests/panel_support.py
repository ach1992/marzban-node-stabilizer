"""Build test candidates through the product transformer, never copied runtime output."""
import atexit
from pathlib import Path
import shutil
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'lib'))
from patch_panel import build_tree

_temporary = tempfile.TemporaryDirectory(prefix='panel-regressions-')
atexit.register(_temporary.cleanup)
ROOT = Path(_temporary.name)
shutil.copytree(REPO / 'tests/fixtures/panel/baseline', ROOT / 'baseline')
shutil.copy2(REPO / 'tests/fixtures/panel/source-hashes.json', ROOT / 'source-hashes.json')
build_tree(ROOT / 'baseline', ROOT / 'candidate')
