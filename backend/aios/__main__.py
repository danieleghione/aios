import argparse
import asyncio
import logging
import signal
from .core import initialize

async def serve(role):
    from .downloads import worker
    from .runtime import manager
    from .platform import broker
    function = {'downloads': worker, 'runtime': manager, 'platform': broker}[role]
    task = asyncio.create_task(function())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        return

def stop(message):
    # These messages are read by an operator at a console or over SSH; argparse's
    # usage dump around them only looked like a malfunction.
    print(message)
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(prog='aios')
    parser.add_argument('role', choices=['init', 'hardware', 'benchmark', 'sync', 'downloads', 'runtime', 'platform', 'bootstrap-reset', 'bootstrap-show', 'console-auth', 'api-key'])
    parser.add_argument('--channel', choices=['console', 'ssh'], default='console')
    parser.add_argument('--action', default='console')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    initialize()
    if args.role == 'hardware':
        from .hardware import profile
        profile()
    elif args.role == 'benchmark':
        from .hardware import benchmark
        benchmark()
    elif args.role == 'sync':
        from .providers import sync_all
        asyncio.run(sync_all())
    elif args.role == 'api-key':
        import sys
        from .auth import console_verify
        from .core import ETC, audit, one
        if not one('SELECT id FROM users LIMIT 1'):
            print('No administrator configured: create one from the portal before retrieving the API key.')
            raise SystemExit(1)
        # Credentials on stdin, read with the echo off by the calling menu.
        username = sys.stdin.readline().strip()
        password = sys.stdin.readline().rstrip('\n')
        user, error = console_verify(username, password, identity='api-key:' + args.channel)
        if not user:
            print(error)
            raise SystemExit(1)
        key = (ETC / 'secrets/inference-key').read_text().strip()
        audit(user['id'], 'api_key_disclosed', detail={'channel': args.channel})
        print()
        print('API key for external tools (n8n, OpenAI clients):')
        print('  ' + key)
        print('Endpoint: https://<appliance-address>/v1  (OpenAI compatible)')
        print('Models:   GET /v1/models   Chat: POST /v1/chat/completions')
        print("Header: Authorization: Bearer <key>. It is the same key Open WebUI uses: treat it like a password.")
    elif args.role == 'console-auth':
        import sys
        from .auth import console_verify
        from .core import audit, one
        if not one('SELECT id FROM users LIMIT 1'):
            print('No administrator configured: the console stays open until one exists.')
            return
        if args.check:
            # An administrator exists, so the caller must collect credentials.
            raise SystemExit(1)
        # Credentials arrive on stdin: the console reads them with echo disabled,
        # which keeps them out of the process arguments as well.
        username = sys.stdin.readline().strip()
        password = sys.stdin.readline().rstrip('\n')
        user, error = console_verify(username, password)
        if not user:
            print(error)
            raise SystemExit(1)
        audit(user['id'], 'console_action', detail={'action': args.action})
        print('Authorised: ' + user['username'])
    elif args.role == 'bootstrap-show':
        import datetime
        from .core import ETC, now, one, setting
        if one('SELECT id FROM users LIMIT 1'):
            stop('An administrator already exists: the bootstrap code is no longer needed. Sign in to the portal with your own account.')
        path = ETC / 'secrets/bootstrap'
        if not path.exists():
            stop('No bootstrap code present: regenerate it.')
        expires = setting('bootstrap_expires', 0)
        if expires and expires < now():
            stop('The bootstrap code has expired: regenerate it.')
        print('Bootstrap secret: ' + path.read_text().strip())
        print('Valid until: ' + datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).isoformat())
    elif args.role == 'bootstrap-reset':
        import secrets
        from .core import ETC, atomic_write, now, one, set_setting
        if one('SELECT id FROM users LIMIT 1'):
            stop('An administrator already exists: the code cannot be regenerated. For lost credentials follow the documented recovery procedure.')
        secret = secrets.token_urlsafe(32)
        atomic_write(ETC / 'secrets/bootstrap', secret)
        set_setting('bootstrap_expires', now() + 86400)
        print('Bootstrap secret (24 hours): ' + secret)
    elif args.role != 'init':
        asyncio.run(serve(args.role))

if __name__ == '__main__':
    main()
