import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
root = Path(__file__).resolve().parents[1]
versions = dict(line.split('=', 1) for line in (root / 'build/versions.env').read_text().splitlines() if '=' in line)
locks = {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'packages': p.read_text().splitlines()} for p in (root/'build').glob('*.lock')}
image = root/'dist/aios-x86_64.img'
result = {'version': versions['AIOS_VERSION'], 'versions': versions, 'built_at': datetime.now(timezone.utc).isoformat(), 'architecture': 'x86_64', 'boot': 'UEFI GPT', 'image_bytes': image.stat().st_size, 'sha256': (root/'dist/aios-x86_64.img.sha256').read_text().split()[0], 'host': platform.platform(), 'git_commit': subprocess.run(['git','-c','safe.directory='+str(root),'rev-parse','HEAD'], cwd=root, capture_output=True, text=True).stdout.strip() or 'uncommitted', 'dependency_locks': locks, 'reproducibility': 'Version-pinned application/runtime; Ubuntu security repository snapshot recorded by package versions. Filesystem UUIDs, build timestamps and security updates mean builds are not byte-identical.'}
provenance = root/'build/openwebui-patch-provenance.json'
if provenance.exists():
    result['openwebui_downstream_patch'] = json.loads(provenance.read_text())
(root/'dist/aios-x86_64-build-info.json').write_text(json.dumps(result, indent=2)+'\n')
