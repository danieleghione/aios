"""Synchronise every repository the appliance ships against the real services.

Run nightly in CI: publishers rename files, APIs change (ModelScope did), and a
repository that silently returns nothing looks exactly like a quiet day. Uses a
throwaway database; only metadata and GGUF headers are read, no weights.

    AIOS_DATA=$(mktemp -d) AIOS_ETC=$(mktemp -d) .venv/bin/python integration-tests/live_repositories.py
"""
import asyncio
import json
import sys

from aios import core
from aios.providers import sync

# Shipped without a configuration on purpose: they need one from the operator.
NEEDS_CONFIGURATION = {'github', 'internal'}


async def main():
    core.initialize()
    failures = []
    for repo in core.rows('SELECT id,name,provider FROM repositories ORDER BY name'):
        result = await sync(repo['id'])
        expected = 'NOT CONFIGURED' if repo['provider'] in NEEDS_CONFIGURATION else 'ONLINE'
        found = result.get('found', 0)
        ok = result['status'] == expected and (expected != 'ONLINE' or found > 0)
        print(f"{'OK ' if ok else 'BAD'} {repo['name']:28} {repo['provider']:12} {result['status']:15} {found:5} {result.get('error') or ''}", flush=True)
        if not ok:
            failures.append({'repository': repo['name'], **result})
    if failures:
        print(json.dumps(failures, indent=2))
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
