"""Accounts: sign-in, the chat session and user administration."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import __version__, auth, chat_auth
from .core import audit, connection, execute, now, one, rows, uid
from .web import admin, superadmin

router = APIRouter()

@router.get('/api/v1/aios/auth/status')
def auth_status():
    return {'initialized': bool(one('SELECT id FROM users LIMIT 1'))}

@router.post('/api/v1/aios/auth/bootstrap')
def bootstrap(data: auth.Bootstrap, request: Request):
    identity = 'bootstrap:' + (request.client.host if request.client else 'unknown')
    attempt = one('SELECT * FROM login_attempts WHERE identity=?', (identity,))
    if attempt and attempt['until'] > now():
        raise HTTPException(429, 'Try again later')
    execute('INSERT INTO login_attempts VALUES (?,1,?) ON CONFLICT(identity) DO UPDATE SET until=excluded.until', (identity, now() + 2))
    auth.setup_account(data)
    return {'initialized': True}

@router.post('/api/v1/aios/auth/login')
def login(data: auth.Login, request: Request, response: Response):
    address = request.client.host if request.client else 'unknown'
    # Where and with what a session was opened, so its owner can recognise it.
    client = address + ' · ' + (request.headers.get('user-agent') or 'unknown client')
    token, csrf = auth.authenticate(data, address + ':' + data.username, client)
    response.delete_cookie('aios_session', path='/api')
    response.delete_cookie('token', path='/')
    response.set_cookie('aios_session', token, httponly=True, secure=True, samesite='strict', max_age=28800, path='/')
    return {'csrf': csrf}

@router.get('/api/v1/aios/auth/me')
def me(user=Depends(auth.current_user)):
    return {**{k: user[k] for k in ('id', 'username', 'role', 'csrf')}, 'must_change': bool(user['must_change']), 'version': __version__}

@router.post('/api/v1/aios/auth/logout')
def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    execute('DELETE FROM sessions WHERE token_hash=?', (auth.digest(request.cookies.get('aios_session', '')),))
    response.delete_cookie('aios_session', path='/api', secure=True, httponly=True, samesite='strict')
    response.delete_cookie('aios_session', path='/', secure=True, httponly=True, samesite='strict')
    response.delete_cookie('token', path='/')
    audit(user['id'], 'logout')
    return {'ok': True}

@router.post('/api/v1/aios/auth/password')
def change_password(data: auth.PasswordChange, user=Depends(auth.current_user)):
    if not auth.verify(data.current, user['password_hash']):
        raise HTTPException(403, 'Current password invalid')
    with connection() as db:
        db.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?', (auth.HASHER.hash(data.password), user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
    audit(user['id'], 'password_change')
    return {'login_required': True}

@router.get('/api/v1/aios/users')
def users(user=Depends(admin)):
    return {'items': rows("SELECT u.id,u.username,u.role,u.must_change,u.disabled,u.last_login,"
                          "(SELECT count(*) FROM sessions s WHERE s.user_id=u.id AND s.expires>?) AS sessions "
                          "FROM users u ORDER BY u.username", (now(),))}


@router.get('/api/v1/aios/auth/sessions')
def my_sessions(request: Request, user=Depends(auth.current_user)):
    """Where the signed-in user is signed in: to recognise a session and end it."""
    mine = auth.digest(request.cookies.get('aios_session', ''))
    return {'items': [{'id': row['token_hash'][:16], 'created': row['created'], 'expires': row['expires'], 'client': row['client'],
                       'current': row['token_hash'] == mine}
                      for row in rows('SELECT * FROM sessions WHERE user_id=? AND expires>? ORDER BY created DESC', (user['id'], now()))]}


@router.delete('/api/v1/aios/auth/sessions/{key}')
def end_session(key: str, request: Request, user=Depends(auth.current_user)):
    """End one of your own sessions, by the prefix the list shows."""
    if len(key) != 16:
        raise HTTPException(422, 'Invalid session')
    ended = execute('DELETE FROM sessions WHERE user_id=? AND substr(token_hash,1,16)=?', (user['id'], key))
    if not ended:
        raise HTTPException(404, 'Session not found')
    audit(user['id'], 'session_ended')
    return {'ended': key}


@router.post('/api/v1/aios/users/{key}/signout')
def sign_out_user(key: str, user=Depends(superadmin)):
    """End every session of another account, for a device left signed in."""
    if key == user['id']:
        raise HTTPException(409, 'Use your own session list to sign out elsewhere')
    if not one('SELECT id FROM users WHERE id=?', (key,)):
        raise HTTPException(404, 'User not found')
    count = execute('DELETE FROM sessions WHERE user_id=?', (key,))
    audit(user['id'], 'user_signed_out', key, {'sessions': count})
    return {'ended': count}

@router.post('/api/v1/aios/users')
def create_user(data: auth.UserCreate, user=Depends(superadmin)):
    if len(data.password) < 12:
        raise HTTPException(422, 'Password requires 12 characters')
    key = uid()
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if '@' in data.username and db.execute('SELECT id FROM users WHERE lower(username)=?', (data.username,)).fetchone():
            raise HTTPException(409, 'Email already in use')
        db.execute('INSERT INTO users(id,username,password_hash,role,must_change) VALUES (?,?,?,?,1)', (key, data.username, auth.HASHER.hash(data.password), data.role))
    audit(user['id'], 'user_create', key)
    return {'id': key}

class UserUpdate(BaseModel):
    role: auth.Role
    disabled: bool

@router.patch('/api/v1/aios/users/{key}')
def update_user(key: str, data: UserUpdate, user=Depends(superadmin)):
    if key == user['id']:
        raise HTTPException(409, 'Cannot disable or demote your own account')
    execute('UPDATE users SET role=?,disabled=? WHERE id=?', (data.role, data.disabled, key))
    execute('DELETE FROM sessions WHERE user_id=?', (key,))
    audit(user['id'], 'user_update', key, data.model_dump())
    return {'ok': True}

class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=256)


@router.post('/api/v1/aios/users/{key}/password')
def reset_password(key: str, data: PasswordReset, user=Depends(superadmin)):
    """A temporary password the user must change at the next sign-in. Without
    this, a forgotten password could only be solved by creating a new account."""
    if key == user['id']:
        raise HTTPException(409, 'Change your own password from System → Account')
    if not one('SELECT id FROM users WHERE id=?', (key,)):
        raise HTTPException(404, 'User not found')
    execute('UPDATE users SET password_hash=?,must_change=1 WHERE id=?', (auth.HASHER.hash(data.password), key))
    execute('DELETE FROM sessions WHERE user_id=?', (key,))
    audit(user['id'], 'user_password_reset', key)
    return {'ok': True}


@router.delete('/api/v1/aios/users/{key}')
def delete_user(key: str, user=Depends(superadmin)):
    target = one('SELECT id,username,role FROM users WHERE id=?', (key,))
    if not target:
        raise HTTPException(404, 'User not found')
    if key == user['id']:
        raise HTTPException(409, 'You cannot delete your own account')
    if target['role'] == 'SUPERADMIN' and one("SELECT count(*) AS n FROM users WHERE role='SUPERADMIN' AND disabled=0")['n'] <= 1:
        raise HTTPException(409, 'The last active SUPERADMIN cannot be deleted')
    execute('DELETE FROM sessions WHERE user_id=?', (key,))
    execute('DELETE FROM users WHERE id=?', (key,))
    # The audit keeps the name: after deletion the identifier alone means nothing.
    audit(user['id'], 'user_delete', key, {'username': target['username'], 'role': target['role']})
    return {'ok': True}


@router.get('/_aios/chat-session', include_in_schema=False)
def chat_session(request: Request, user=Depends(auth.current_user)):
    headers = chat_auth.ensure_identity(user, request.cookies.get('aios_session', ''))
    return Response(status_code=204, headers=headers)
