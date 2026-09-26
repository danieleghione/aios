"""One release number: VERSION, the build, both packages, the API, the portal and
the installation instructions all say the same."""
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / 'VERSION').read_text().strip()


def test_every_file_carries_the_same_release_number():
    assert re.fullmatch(r'\d+\.\d+\.\d+', VERSION)
    env = dict(line.split('=', 1) for line in (ROOT / 'build/versions.env').read_text().splitlines() if '=' in line)
    assert env['AIOS_VERSION'] == VERSION
    assert tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'] == VERSION
    assert json.loads((ROOT / 'frontend-admin/package.json').read_text())['version'] == VERSION
    lock = json.loads((ROOT / 'frontend-admin/package-lock.json').read_text())
    assert lock['version'] == lock['packages']['']['version'] == VERSION
    for doc in ('README.md', 'docs/install.md'):
        named = set(re.findall(r'aios-installer-(\d+\.\d+\.\d+)-x86_64', (ROOT / doc).read_text()))
        assert named <= {VERSION}, (doc, named)


def test_no_release_number_is_written_by_hand_in_the_code():
    for path in [*(ROOT / 'backend/aios').rglob('*.py'), *(ROOT / 'frontend-admin/src').rglob('*.tsx')]:
        if path.name.endswith('.test.tsx'):
            continue
        assert VERSION not in path.read_text(), path


def test_the_api_and_the_portal_report_it(admin):
    import aios
    from aios.app import app
    assert aios.__version__ == VERSION and app.version == VERSION
    assert admin.get('/api/v1/aios/auth/me').json()['version'] == VERSION
    assert admin.get('/api/v1/aios/hardware/profile').json()['aios_version'] == VERSION
    assert admin.get('/api/v1/aios/system/dashboard').json()['hardware']['aios_version'] == VERSION
    assert '<footer>AIOS {user?.version' in (ROOT / 'frontend-admin/src/App.tsx').read_text()

