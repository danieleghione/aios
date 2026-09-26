"""Settings shared by the lab checks, from the environment. The defaults match
boot.sh: the appliance forwarded to 127.0.0.1:28443, the lab under build/lab.
The administrator's password is made up on first use and kept in the lab's
work folder, outside the repository; AIOS_LAB_PASSWORD overrides it."""
import os
import secrets
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = os.environ.get('AIOS_LAB_URL', 'https://127.0.0.1:28443')
ADMIN_USER = os.environ.get('AIOS_LAB_ADMIN', 'admin@example.org')
HOSTNAME = 'aios-lab'
LAB = Path(os.environ.get('AIOS_LAB_DIR', REPO / 'build' / 'lab'))
WORK = LAB / 'work'
WORK.mkdir(parents=True, exist_ok=True)


def lab_password():
    stored = WORK / 'admin-password'
    if not stored.exists():
        stored.write_text(secrets.token_urlsafe(18))
        stored.chmod(0o600)
    return stored.read_text().strip()


ADMIN_PASSWORD = os.environ.get('AIOS_LAB_PASSWORD') or lab_password()
ADMIN = (ADMIN_USER, ADMIN_PASSWORD)


def throwaway_password():
    """For accounts a check creates and deletes again."""
    return secrets.token_urlsafe(18)
CONSOLE_LOG = LAB / 'console.log'
SSH_KEY = LAB / 'lab-key'
# A public-domain recording with a known transcript, from the whisper.cpp sources.
SPEECH_SAMPLE_URL = 'https://raw.githubusercontent.com/ggml-org/whisper.cpp/927cfce34f31707e17f2bff35c349632fb9e2c3a/samples/jfk.wav'
