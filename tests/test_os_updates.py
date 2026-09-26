"""Operating system updates survive a mirror that is briefly out of step."""
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, failures):
    """updates.sh apply with an apt-get that fails the first `failures` upgrades."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'apt.log'
    (bin_dir / 'apt-get').write_text(f"""#!/bin/bash
echo "$*" >> {log}
if [[ " $* " == *" -y "* ]]; then
  (( $(grep -c -- '-y ' {log}) <= {failures} )) && {{ echo 'E: Failed to fetch libexpat1.deb  404  Not Found' >&2; exit 100; }}
fi
exit 0
""")
    (bin_dir / 'apt-get').chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'AIOS_UPDATES_STATUS': str(tmp_path / 'updates.json'),
           'AIOS_UPDATES_RETRY_WAIT': '0'}
    result = subprocess.run(['bash', str(ROOT / 'installer/updates.sh'), 'apply'], capture_output=True, text=True, env=env, timeout=60)
    upgrades = [line for line in log.read_text().splitlines() if '-y ' in line]
    return result, upgrades, json.loads((tmp_path / 'updates.json').read_text())


def test_a_package_the_mirror_does_not_serve_yet_is_fetched_again(tmp_path):
    result, upgrades, status = run(tmp_path, failures=1)
    assert result.returncode == 0 and 'Updates installed.' in result.stdout
    assert len(upgrades) == 2 and 'attempt 2 of 3' in result.stdout
    assert status['error'] == ''


def test_three_failures_are_reported(tmp_path):
    result, upgrades, _ = run(tmp_path, failures=5)
    assert result.returncode == 1 and len(upgrades) == 3
    assert 'Updates could not be installed' in result.stdout
