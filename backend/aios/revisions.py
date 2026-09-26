"""Newer revisions of installed models.

A repository that publishes a new revision of a file adds a catalogue entry
beside the one installed: same repository, same file, another revision. It is
an update when it came later and its content differs; a commit that touched
only the model card changes the revision and leaves the weights alone.

Updating installs the newer revision beside the installed one and hands over
what the administrator set on it: runtime configuration, notes, publication
and the default of its kind. The previous revision stays, unpublished, until
someone deletes it, which is also the way back.
"""
import json

from . import alerts
from .core import audit, execute, now, one, rows, set_setting, setting


def newer(model_id):
    """The newest later revision of an installed model, or None."""
    current = one('SELECT d.repository_id,d.upstream_key,d.revision,d.discovered_at,i.sha256,a.size FROM installed_models i '
                  'JOIN discovered_models d ON d.id=i.id JOIN model_artifacts a ON a.id=i.id WHERE i.id=?', (model_id,))
    if not current:
        return None
    for row in rows('SELECT d.id,d.revision,d.metadata,d.discovered_at,a.sha256,a.size FROM discovered_models d JOIN model_artifacts a ON a.id=d.id '
                    'WHERE d.repository_id=? AND d.upstream_key=? AND d.revision!=? AND d.discovered_at>? '
                    'AND NOT EXISTS (SELECT 1 FROM installed_models i WHERE i.id=d.id) ORDER BY d.discovered_at DESC',
                    (current['repository_id'], current['upstream_key'], current['revision'], current['discovered_at'])):
        same = (row['sha256'].lower() == current['sha256'].lower()) if row['sha256'] else row['size'] == current['size']
        if not same:
            metadata = json.loads(row['metadata'])
            return {'id': row['id'], 'revision': row['revision'], 'size': row['size'], 'license': metadata.get('license', 'unknown'),
                    'released': metadata.get('release_date')}
    return None


def check_all():
    """Open a notice for every installed model with a newer revision, close the others."""
    found = 0
    for model in rows('SELECT i.id,d.metadata FROM installed_models i JOIN discovered_models d ON d.id=i.id WHERE i.replaced_by IS NULL'):
        update = newer(model['id'])
        if update and not one("SELECT id FROM downloads WHERE model_id=? AND state!='FAILED'", (update['id'],)):
            name = json.loads(model['metadata']).get('display_name', model['id'])
            alerts.raise_alert('revision:' + model['id'], 'INFO', f'A newer revision of {name} is available. Update it from Installed models.')
            found += 1
        else:
            alerts.resolve('revision:' + model['id'])
    return found


def hand_over(previous, current):
    """Give the new revision what the administrator set on the previous one."""
    old = one('SELECT * FROM installed_models WHERE id=?', (previous,))
    if not old or not one('SELECT id FROM installed_models WHERE id=?', (current,)):
        return
    execute('UPDATE installed_models SET config=?,notes=?,published=?,state=? WHERE id=?',
            (old['config'], old['notes'], old['published'], 'PUBLISHED' if old['published'] else 'INSTALLED', current))
    execute("UPDATE installed_models SET published=0,state=CASE WHEN state='PUBLISHED' THEN 'INSTALLED' ELSE state END,replaced_by=? WHERE id=?",
            (current, previous))
    for key in ('default_model', 'default_image_model', 'default_speech_model', 'default_voice_model'):
        if setting(key, '') == previous:
            set_setting(key, current)
    alerts.resolve('revision:' + previous)
    audit('download-worker', 'model_revision_updated', current, {'previous': previous, 'at': now()})
