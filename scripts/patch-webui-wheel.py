"""Repack the official wheel with explicit AIOS security dependency patches.

Upstream code/assets/license are retained. Wheel metadata and RECORD are rebuilt;
local version +aios.1 clearly identifies the distribution as downstream-modified.
"""
import base64
import csv
import hashlib
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = dict(line.split('=', 1) for line in (ROOT/'build/versions.env').read_text().splitlines() if '=' in line)['OPEN_WEBUI_VERSION']
PATCHES = json.loads((ROOT/'config/openwebui-security-overrides.json').read_text())
info = json.load(urllib.request.urlopen(f'https://pypi.org/pypi/open-webui/{VERSION}/json', timeout=60))
wheel = next(f for f in info['urls'] if f['filename'].endswith('.whl'))
original = ROOT/'build'/wheel['filename']
if not original.exists() or hashlib.sha256(original.read_bytes()).hexdigest() != wheel['digests']['sha256']:
    urllib.request.urlretrieve(wheel['url'], original)
assert hashlib.sha256(original.read_bytes()).hexdigest() == wheel['digests']['sha256'], 'Upstream wheel checksum mismatch'
local = VERSION + '+aios.1'
output = ROOT/'build'/wheel['filename'].replace(VERSION, local)
records = []
matched = set()
with zipfile.ZipFile(original) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
    for name in source.namelist():
        if name.endswith('/RECORD'):
            continue
        data = source.read(name)
        new_name = name.replace(f'open_webui-{VERSION}.dist-info', f'open_webui-{local}.dist-info')
        if name.endswith('.dist-info/METADATA'):
            lines = data.decode().splitlines()
            for index, line in enumerate(lines):
                if line == f'Version: {VERSION}':
                    lines[index] = f'Version: {local}'
                match = re.match(r'Requires-Dist: ([A-Za-z0-9_-]+)(.*)', line)
                if match:
                    package = match[1].lower().replace('_', '-')
                    if package in PATCHES:
                        marker = ';' + line.split(';', 1)[1] if ';' in line else ''
                        lines[index] = f'Requires-Dist: {package}=={PATCHES[package]}{marker}'
                        matched.add(package)
            data = ('\n'.join(lines)+'\n').encode()
        target.writestr(new_name, data)
        records.append((new_name, 'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='), len(data)))
    assert matched == set(PATCHES), f'Patch target missing: {set(PATCHES)-matched}'
    record_name = f'open_webui-{local}.dist-info/RECORD'
    records.append((record_name, '', ''))
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\n').writerows(records)
    target.writestr(record_name, buffer.getvalue())
(ROOT/'build/openwebui-patch-provenance.json').write_text(json.dumps({'upstream': VERSION, 'local_version': local, 'source_url': wheel['url'], 'upstream_sha256': wheel['digests']['sha256'], 'patched_sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'dependency_overrides': PATCHES}, indent=2)+'\n')
print(output)
