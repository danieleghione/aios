"""Provision chat identities through upstream trusted-header auth, never shared DB writes."""
import threading
import time
import httpx
from fastapi import HTTPException
from . import auth
from .core import one

_lock = threading.Lock()
_ready: dict[str, float] = {}

def ensure_identity(user, token):
    username = user['username']
    email = username.lower() if '@' in username else user['id'] + '@users.aios.invalid'
    if '@' in username and one('SELECT count(*) AS n FROM users WHERE lower(username)=?', (email,))['n'] != 1:
        raise HTTPException(403, 'Conflicting email identities; contact the administrator')
    headers = {'X-AIOS-Email': email, 'X-AIOS-Name': username,
               'X-AIOS-Role': 'admin' if user['role'] == 'SUPERADMIN' else 'user'}
    key = auth.digest(token) + ':' + headers['X-AIOS-Role']
    # Reconcile once per session/minute, before letting any existing WebUI JWT through.
    # Role changes and password resets revoke AIOS sessions in the authoritative DB.
    with _lock:
        current = time.monotonic()
        if _ready.get(key, 0) > current:
            return headers
        try:
            response = httpx.post('http://127.0.0.1:8080/api/v1/auths/signin',
                                  headers=headers, json={'email': email, 'password': ''},
                                  timeout=15, trust_env=False)
            response.raise_for_status()
            identity = response.json()
            if identity.get('email') != email or identity.get('role') != headers['X-AIOS-Role']:
                raise ValueError('Identity mismatch')
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, 'Chat authentication is not ready; retry shortly') from None
        for expired in [k for k, expiry in _ready.items() if expiry <= current]:
            del _ready[expired]
        if len(_ready) >= 4096:
            _ready.clear()
        _ready[key] = current + 60
    return headers
