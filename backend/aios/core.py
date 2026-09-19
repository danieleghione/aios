"""Configuration, transactional SQLite storage and append-only audit."""
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

DATA = Path(os.environ.get("AIOS_DATA", "/var/lib/aios"))
ETC = Path(os.environ.get("AIOS_ETC", "/etc/aios"))
DB = DATA / "database/aios.db"
_ANCHORS: dict = {}


def anchor():
    """Keep one connection open for the life of each process. SQLite deletes the
    -wal and -shm files whenever the last connection closes, and the next opener
    recreates them; with every service opening a connection per statement that
    delete-and-recreate churn is where the intermittent "unable to open database
    file" came from. A connection that stays open keeps both files in place. It
    runs no transaction, so it never holds back a checkpoint."""
    path = str(DB)
    if path in _ANCHORS or not DB.exists():
        return
    try:
        held = sqlite3.connect(path, timeout=30, check_same_thread=False)
        held.execute('PRAGMA schema_version').fetchone()  # maps the WAL index
        _ANCHORS[path] = held
    except sqlite3.Error:
        pass

def now():
    return time.time()

def projector_path(model_id):
    """Where the multimodal projector of an installed model lives, if it has one."""
    return DATA / 'models' / (model_id + '.mmproj.gguf')


def uid():
    return str(uuid.uuid4())

def encode(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)

@contextlib.contextmanager
def connection():
    anchor()
    for attempt in range(6):
        db = sqlite3.connect(DB, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            break
        except sqlite3.OperationalError as exc:
            db.close()
            # A momentary open failure must not propagate: it used to crash the
            # download worker and the runtime manager, which stops every model.
            if 'unable to open' not in str(exc) or attempt == 5:
                raise
            logging.warning('database open failed (attempt %d), retrying: %s', attempt + 1, exc)
            time.sleep(0.05 * 2 ** attempt)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

def rows(sql, args=()):
    with connection() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]

def one(sql, args=()):
    result = rows(sql, args)
    return result[0] if result else None

def execute(sql, args=()):
    with connection() as db:
        return db.execute(sql, args).rowcount

def setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default

def set_setting(key, value):
    execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encode(value)))

def audit(actor, action, target="", detail=None):
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        prev = db.execute("SELECT digest FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        payload = encode({"time": now(), "actor": actor, "action": action, "target": target, "detail": detail or {}})
        previous = prev[0] if prev else "0" * 64
        digest = hashlib.sha256((previous + payload).encode()).hexdigest()
        db.execute("INSERT INTO audit_events(payload,previous,digest) VALUES (?,?,?)", (payload, previous, digest))

def atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uid() + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

# Official publishers plus the quantisers that republish their releases promptly
# and faithfully. ggml-org is llama.cpp's own organisation. Checked against what
# each publisher actually released, September 2026.
_QUANTISERS = ["ggml-org", "unsloth", "lmstudio-community"]
CURATED = {
    "Qwen (GGUF)": {"publishers": ["Qwen", *_QUANTISERS], "search": ["Qwen"], "limit": 30},
    # deepseek-ai publishes no GGUF of its own.
    "DeepSeek (GGUF)": {"publishers": _QUANTISERS, "search": ["DeepSeek"], "limit": 30},
    "Mistral (GGUF)": {"publishers": ["mistralai", *_QUANTISERS],
                       "search": ["Mistral", "Ministral", "Devstral", "Magistral"], "limit": 30},
}
# The fixed lists this appliance first shipped with; they can never see a release.
_RETIRED_CURATED = {
    "Qwen (GGUF)": ["Qwen/Qwen2.5-0.5B-Instruct-GGUF", "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                    "Qwen/Qwen2.5-3B-Instruct-GGUF", "Qwen/Qwen2.5-7B-Instruct-GGUF"],
    "DeepSeek (GGUF)": ["bartowski/DeepSeek-R1-Distill-Qwen-1.5B-GGUF", "bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF",
                        "bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF"],
    "Mistral (GGUF)": ["bartowski/Mistral-7B-Instruct-v0.3-GGUF", "bartowski/Ministral-8B-Instruct-2410-GGUF"],
}


def initialize():
    for sub in ("models", "downloads", "cache", "registry", "database", "system", "backups", "runtime", "webui"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(DATA / 'database', 0o700)
    os.chmod(DATA / 'backups', 0o700)
    (ETC / "secrets").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ETC / "secrets", 0o700)
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(Path(__file__).with_name("schema.sql").read_text())
    os.chmod(DB, 0o600)
    if not one("SELECT id FROM repositories LIMIT 1"):
        # ModelScope ships with the same publishers the Hugging Face families follow,
        # so enabling it discovers models instead of asking for a list first.
        seeded = {"ModelScope": {"publishers": ["Qwen", "unsloth", "lmstudio-community", "ggml-org"], "search": ["GGUF"], "limit": 20}}
        for name, provider, url in [("Hugging Face", "huggingface", "https://huggingface.co"), ("ModelScope", "modelscope", "https://modelscope.cn"), ("GitHub", "github", "https://api.github.com"), ("Internal", "internal", "")]:
            execute("INSERT INTO repositories(id,name,provider,url,config) VALUES (?,?,?,?,?)", (uid(), name, provider, url, encode(seeded.get(name, {}))))
        # Seeded disabled: each family is synchronised only once someone turns it on.
        # Shipping them enabled made switching on one look like it switched on all.
        for name, query in CURATED.items():
            execute("INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,0)",
                    (uid(), name, "huggingface", "https://huggingface.co", encode(query)))
        # Image generation, seeded disabled like the others.
        execute("INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,0)",
                (uid(), "Image models (diffusion)", "diffusion", "https://huggingface.co", '{}'))
    for name, models in _RETIRED_CURATED.items():
        # Databases created by earlier builds, or restored from their backups, still
        # carry the fixed lists; move them to discovery unless someone edited them.
        for row in rows("SELECT id,config FROM repositories WHERE name=?", (name,)):
            if json.loads(row["config"]) == {"models": models}:
                execute("UPDATE repositories SET config=? WHERE id=?", (encode(CURATED[name]), row["id"]))
    if not one("SELECT id FROM users LIMIT 1") and not (ETC / "secrets/bootstrap").exists():
        import secrets
        atomic_write(ETC / "secrets/bootstrap", secrets.token_urlsafe(32))
        set_setting("bootstrap_expires", now() + 86400)
