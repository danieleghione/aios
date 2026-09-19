import hashlib
import hmac
import secrets
import re
from typing import Literal
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from .core import ETC, audit, connection, execute, now, one, setting, uid

HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
Role = Literal['SUPERADMIN', 'ADMIN', 'OPERATOR', 'VIEWER']
LEVEL = {'VIEWER': 0, 'OPERATOR': 1, 'ADMIN': 2, 'SUPERADMIN': 3}

class Login(BaseModel):
    # Email addresses are accepted so an account can match the one a user already
    # has in the chat; the set stays restrictive enough for logs and audit keys.
    username: str = Field(min_length=1, max_length=254, pattern=r'^[a-zA-Z0-9_.@+-]+$')
    password: str = Field(min_length=1, max_length=256)

    @field_validator('username')
    @classmethod
    def normalize_email(cls, value):
        if '@' in value:
            if not re.fullmatch(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+', value):
                raise ValueError('Use a valid email address')
            value = value.lower()
            if value.endswith('@users.aios.invalid'):
                raise ValueError('Reserved internal identity domain')
        return value

class Bootstrap(Login):
    secret: str = Field(max_length=128)

class UserCreate(Login):
    role: Role

class PasswordChange(BaseModel):
    current: str = Field(max_length=256)
    password: str = Field(min_length=12, max_length=256)

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def verify(password, hashed):
    try:
        return HASHER.verify(hashed, password)
    except VerificationError:
        return False

def current_user(request: Request):
    token = request.cookies.get('aios_session', '')
    user = one('SELECT u.*,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.disabled=0', (digest(token), now()))
    if not user:
        raise HTTPException(401, 'Authentication required')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf']):
            raise HTTPException(403, 'CSRF token invalid')
    if user['must_change'] and request.url.path not in ('/api/v1/aios/auth/password', '/api/v1/aios/auth/me', '/api/v1/aios/auth/logout'):
        raise HTTPException(403, 'Password change required')
    return user

def require(role):
    def dependency(user=Depends(current_user)):
        if LEVEL[user['role']] < LEVEL[role]:
            raise HTTPException(403, 'Insufficient role')
        return user
    return dependency

def setup_account(data):
    path = ETC / 'secrets/bootstrap'
    if len(data.password) < 12:
        raise HTTPException(422, 'Password requires at least 12 characters')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT id FROM users LIMIT 1').fetchone():
            raise HTTPException(409, 'Already initialized')
        if not path.exists() or now() > setting('bootstrap_expires', 0) or not hmac.compare_digest(path.read_text().strip(), data.secret):
            raise HTTPException(403, 'Bootstrap secret invalid or expired; regenerate on local console')
        user_id = uid()
        db.execute('INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)', (user_id, data.username, HASHER.hash(data.password), 'SUPERADMIN'))
    path.unlink(missing_ok=True)
    audit(user_id, 'bootstrap')

def console_verify(username, password, identity='console'):
    """Authorise one physical-console action. Shares the login backoff and audit
    trail with the portal, and demands an administrative role: the console can
    erase disks and open a root shell. Each channel keeps its own backoff, so
    guessing over SSH cannot lock out the physical console."""
    attempt = one('SELECT * FROM login_attempts WHERE identity=?', (identity,))
    if attempt and attempt['until'] > now():
        return None, 'Console locked after failed attempts; wait %d seconds.' % (attempt['until'] - now())
    column, value = ('lower(username)', username.lower()) if '@' in username else ('username', username)
    user = one('SELECT * FROM users WHERE ' + column + '=? AND disabled=0', (value,))
    # Always spend the hash so a missing account is not faster than a wrong password.
    valid = verify(password, user['password_hash']) if user else verify(password, HASHER.hash(secrets.token_hex(16)))
    if not user or not valid or LEVEL[user['role']] < LEVEL['ADMIN']:
        count = min((attempt['failures'] if attempt else 0) + 1, 12)
        execute('INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET failures=excluded.failures,until=excluded.until', (identity, count, now() + min(2 ** count, 900)))
        audit(user['id'] if user else 'anonymous', 'console_denied', detail={'username_hash': digest(username)})
        return None, 'Invalid credentials or insufficient role.'
    execute('DELETE FROM login_attempts WHERE identity=?', (identity,))
    return user, None

def authenticate(data, identity):
    attempt = one('SELECT * FROM login_attempts WHERE identity=?', (identity,))
    if attempt and attempt['until'] > now():
        raise HTTPException(429, 'Login temporarily locked')
    matches = one('SELECT count(*) AS n FROM users WHERE lower(username)=?', (data.username,)) if '@' in data.username else None
    if matches and matches['n'] > 1:
        raise HTTPException(409, 'Conflicting email identities; contact the administrator')
    user = one('SELECT * FROM users WHERE ' + ('lower(username)' if '@' in data.username else 'username') + '=? AND disabled=0', (data.username,))
    valid = verify(data.password, user['password_hash']) if user else verify(data.password, HASHER.hash(secrets.token_hex(16)))
    if not user or not valid:
        count = min((attempt['failures'] if attempt else 0) + 1, 12)
        execute('INSERT INTO login_attempts VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET failures=excluded.failures,until=excluded.until', (identity, count, now() + min(2 ** count, 900)))
        audit('anonymous', 'login_failure', detail={'identity_hash': digest(identity)})
        raise HTTPException(401, 'Invalid credentials')
    execute('DELETE FROM login_attempts WHERE identity=?', (identity,))
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    execute('DELETE FROM sessions WHERE expires<?', (now(),))
    execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user['id'], csrf, now() + 28800))
    audit(user['id'], 'login')
    return token, csrf
