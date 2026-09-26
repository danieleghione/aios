"""Backups: what each archive is, encryption with a passphrase, and retention.

A backup carries the accounts, the credentials and the chat history, so it can
be encrypted with a passphrase the administrator chooses. The archive is sealed
in chunks with AES-256-GCM under a key derived by scrypt; every chunk is bound to
its position and to the header, and the last one is marked as such, so a file
that is altered, reordered or cut short does not decrypt.
"""
import json
import os
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .core import DATA, ETC, atomic_write, now

MAGIC = b'AIOSENC1'
CHUNK = 4 * 1024 ** 2
PLAIN, SEALED = '.tar.gz', '.tar.gz.enc'
NAME = r'[a-f0-9-]{36}\.tar\.gz(?:\.enc)?'


def passphrase_path():
    return ETC / 'secrets' / 'backup-passphrase'


def stored_passphrase():
    try:
        return passphrase_path().read_text()
    except OSError:
        return ''


def set_passphrase(value):
    """Store the passphrase every backup is encrypted with, or remove it."""
    if value:
        atomic_write(passphrase_path(), value, 0o600)
    else:
        passphrase_path().unlink(missing_ok=True)


def _key(passphrase, salt):
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def encrypt_file(source, target, passphrase):
    """Seal an archive. The header is the magic, the scrypt salt and a nonce prefix."""
    salt, prefix = secrets.token_bytes(16), secrets.token_bytes(8)
    header = MAGIC + salt + prefix
    cipher = AESGCM(_key(passphrase, salt))
    size = Path(source).stat().st_size
    with open(source, 'rb') as reader, open(target, 'wb') as writer:
        writer.write(header)
        index, done = 0, 0
        while True:
            chunk = reader.read(CHUNK)
            done += len(chunk)
            last = done >= size
            sealed = cipher.encrypt(prefix + struct.pack('>I', index), chunk, header + (b'\x01' if last else b'\x00'))
            writer.write(struct.pack('>I', len(sealed)) + sealed)
            index += 1
            if last:
                break


def decrypt_file(source, target, passphrase):
    """Open a sealed archive, or refuse with one reason for every kind of damage."""
    with open(source, 'rb') as reader, open(target, 'wb') as writer:
        header = reader.read(len(MAGIC) + 24)
        if not header.startswith(MAGIC) or len(header) != len(MAGIC) + 24:
            raise ValueError('Not an encrypted AIOS backup')
        salt, prefix = header[len(MAGIC):len(MAGIC) + 16], header[len(MAGIC) + 16:]
        cipher = AESGCM(_key(passphrase, salt))
        index, finished = 0, False
        while not finished:
            length = reader.read(4)
            if len(length) != 4:
                raise ValueError('Wrong passphrase or damaged backup')
            sealed = reader.read(struct.unpack('>I', length)[0])
            nonce = prefix + struct.pack('>I', index)
            try:
                chunk = cipher.decrypt(nonce, sealed, header + b'\x00')
            except InvalidTag:
                try:
                    chunk = cipher.decrypt(nonce, sealed, header + b'\x01')
                    finished = True
                except InvalidTag:
                    raise ValueError('Wrong passphrase or damaged backup') from None
            writer.write(chunk)
            index += 1
        if reader.read(1):
            raise ValueError('Wrong passphrase or damaged backup')


def is_encrypted(path):
    with open(path, 'rb') as reader:
        return reader.read(len(MAGIC)) == MAGIC


def describe(path):
    """What the backups page shows for one archive."""
    path = Path(path)
    stem = path.name.split('.', 1)[0]
    try:
        facts = json.loads((path.parent / (stem + '.json')).read_text())
    except (OSError, ValueError):
        facts = {}
    stat = path.stat()
    return {'file': path.name, 'size': stat.st_size, 'created': facts.get('created', stat.st_mtime),
            'encrypted': path.name.endswith('.enc'), 'scheduled': bool(facts.get('scheduled')),
            'include_models': bool(facts.get('include_models')), 'kind': facts.get('kind', 'backup' if facts else 'uploaded')}


def record(path, **facts):
    """Keep what a backup is beside it: when, how, and whether the schedule made it."""
    stem = Path(path).name.split('.', 1)[0]
    target = Path(path).parent / (stem + '.json')
    atomic_write(target, json.dumps({'created': now(), 'kind': 'backup', **facts}), 0o600)
    return target


def archives(folder=None):
    folder = Path(folder or DATA / 'backups')
    return sorted((p for p in folder.glob('*.tar.gz*') if p.name.endswith((PLAIN, SEALED))),
                  key=lambda p: describe(p)['created'], reverse=True)


def prune(keep, folder=None):
    """Keep the newest scheduled backups; the others go. Backups made by hand and
    uploaded archives are never removed by the schedule."""
    removed = []
    scheduled = [p for p in archives(folder) if describe(p)['scheduled']]
    for path in scheduled[max(1, keep):]:
        stem = path.name.split('.', 1)[0]
        path.unlink(missing_ok=True)
        (path.parent / (stem + '.json')).unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def due(schedule, last_run, current):
    """Whether the daily backup should run now: enabled, the hour reached, and not
    already made today (dates in the appliance's own time zone)."""
    if not schedule or not schedule.get('enabled'):
        return False
    import time
    today = time.strftime('%Y-%m-%d', time.localtime(current))
    hour = time.localtime(current).tm_hour
    return last_run != today and hour >= int(schedule.get('hour', 2))


def fix_permissions(path, owner='aios'):
    import shutil
    os.chmod(path, 0o600)
    try:
        os.chown(path, shutil._get_uid(owner), shutil._get_gid(owner))
    except (KeyError, PermissionError, LookupError):
        pass
