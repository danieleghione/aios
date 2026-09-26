"""Alerts: conditions an administrator should know about, shown on the dashboard.

Each alert has a stable key — one per repository, per model, per condition — so
the same problem is one entry however often it is seen, and it closes on its own
when the condition clears. An administrator can also dismiss one.
"""
import json
import shutil

from .core import DATA, execute, now, one

SEVERITIES = ('INFO', 'WARNING', 'ERROR')
DISK_FREE_MIN = 5 * 1024 ** 3
DISK_FREE_FRACTION = 0.05


def raise_alert(key, severity, message):
    """Open or refresh an alert. A new occurrence of a closed one opens it again."""
    if severity not in SEVERITIES:
        raise ValueError('Unknown severity')
    current = one('SELECT resolved FROM alerts WHERE id=?', (key,))
    if current and not current['resolved']:
        execute('UPDATE alerts SET severity=?,message=? WHERE id=?', (severity, message[:500], key))
        return False
    # notified=0: the control plane sends it by webhook or e-mail (notify.py).
    execute('INSERT INTO alerts(id,severity,message,created_at,resolved,notified) VALUES (?,?,?,?,0,0) '
            'ON CONFLICT(id) DO UPDATE SET severity=excluded.severity,message=excluded.message,'
            'created_at=excluded.created_at,resolved=0,notified=0', (key, severity, message[:500], now()))
    return True


def resolve(key):
    """Close an alert whose condition has cleared."""
    execute('UPDATE alerts SET resolved=1 WHERE id=? AND resolved=0', (key,))


def check_disk(path=None):
    """Warn before the model storage runs out: downloads, backups and the
    database all need room."""
    usage = shutil.disk_usage(path or DATA)
    if usage.free < DISK_FREE_MIN or usage.free < usage.total * DISK_FREE_FRACTION:
        raise_alert('disk', 'WARNING', f'Model storage is almost full: {usage.free / 1024 ** 3:.1f} GiB free of '
                    f'{usage.total / 1024 ** 3:.0f} GiB. Remove models you do not use, or enlarge the disk.')
    else:
        resolve('disk')


def check_updates():
    """Security updates waiting, and a restart that an installation left pending."""
    try:
        status = json.loads((DATA / 'system' / 'updates.json').read_text())
    except (OSError, ValueError):
        return
    if status.get('reboot_required'):
        raise_alert('updates', 'WARNING', 'System updates were installed: restart the appliance to finish.')
    elif int(status.get('security') or 0):
        raise_alert('updates', 'INFO', f"{status['security']} security updates are available under System, Updates.")
    else:
        resolve('updates')
