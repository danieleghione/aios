#!/usr/bin/env bash
# Set the release number everywhere it appears: the build, the Python package,
# the portal package and the installation instructions. The portal, the API and
# the hardware profile read it from VERSION at run time.
#
#   scripts/set-version.sh 1.12.1
set -Eeuo pipefail
cd "$(dirname "$0")/.."
version=${1:?usage: set-version.sh <major.minor.patch>}
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Not a version: $version" >&2; exit 1; }
echo "$version" > VERSION
sed -i -E "s/^AIOS_VERSION=.*/AIOS_VERSION=$version/" build/versions.env
sed -i -E "s/^version = \"[^\"]*\"/version = \"$version\"/" pyproject.toml
python3 - "$version" <<'PY'
import json, sys
version = sys.argv[1]
path = 'frontend-admin/package.json'
data = json.load(open(path))
data['version'] = version
open(path, 'w').write(json.dumps(data, separators=(',', ':')))
path = 'frontend-admin/package-lock.json'
lock = json.load(open(path))
lock['version'] = version
lock.get('packages', {}).get('', {})['version'] = version
open(path, 'w').write(json.dumps(lock, indent=2) + '\n')
PY
sed -i -E "s/aios-installer-[0-9]+\.[0-9]+\.[0-9]+-x86_64/aios-installer-$version-x86_64/g" README.md docs/install.md
echo "Version $version"
