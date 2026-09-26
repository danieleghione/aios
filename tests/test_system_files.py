"""An application release installs the units, the NGINX site and the firewall
rules it carries, each checked before it is used."""
import subprocess
from pathlib import Path

import pytest

from aios import platform, system_files


@pytest.fixture
def layout(tmp_path):
    app, root = tmp_path / 'app', tmp_path / 'root'
    (app / 'systemd').mkdir(parents=True)
    (app / 'config').mkdir()
    (app / 'systemd/aios-control-plane.service').write_text('[Service]\nExecStart=/new\n')
    (app / 'systemd/aios-embedding.socket').write_text('[Socket]\nListenStream=127.0.0.1:8093\n')
    (app / 'systemd/aios-embedding.service').write_text('[Service]\n')
    (app / 'config/nginx.conf').write_text('client_max_body_size 200m;\n')
    (app / 'config/nftables.conf').write_text('flush ruleset\n')
    (app / 'config/enabled-units').write_text('# units\naios-control-plane.service\naios-embedding.socket\n')
    (root / 'etc/systemd/system').mkdir(parents=True)
    (root / 'etc/systemd/system/aios-control-plane.service').write_text('[Service]\nExecStart=/old\n')
    (root / 'etc/nginx/sites-available').mkdir(parents=True)
    (root / 'etc/nginx/sites-available/aios').write_text('client_max_body_size 32m;\n')
    (root / 'etc/nftables.conf').write_text('flush ruleset\n')
    return app, root


class Recorder:
    def __init__(self, enabled=(), failing=()):
        self.calls, self.enabled, self.failing = [], set(enabled), failing

    def __call__(self, command):
        self.calls.append(command)
        if any(word in command for word in self.failing):
            raise subprocess.CalledProcessError(1, command, stderr='nginx: [emerg] unknown directive')
        if command[:3] == ['systemctl', 'is-enabled', '--quiet'] and command[3] not in self.enabled:
            raise subprocess.CalledProcessError(1, command)


def test_a_release_brings_its_units_and_configuration(layout):
    app, root = layout
    run = Recorder(enabled={'aios-control-plane.service'})
    changed = system_files.sync(app, root, run)
    assert (root / 'etc/systemd/system/aios-control-plane.service').read_text().endswith('/new\n')
    assert (root / 'etc/systemd/system/aios-embedding.socket').exists()
    assert (root / 'etc/nginx/sites-available/aios').read_text() == 'client_max_body_size 200m;\n'
    assert str(root / 'etc/nftables.conf') not in changed  # unchanged: left alone
    assert ['nginx', '-t'] in run.calls and ['systemctl', 'try-reload-or-restart', 'nginx'] in run.calls
    assert ['systemctl', 'daemon-reload'] in run.calls
    assert ['systemctl', 'enable', 'aios-embedding.socket'] in run.calls
    assert ['systemctl', 'start', '--no-block', 'aios-embedding.socket'] in run.calls
    # Nothing to do the second time.
    run.enabled.add('aios-embedding.socket')
    assert system_files.sync(app, root, run) == []


def test_a_file_that_fails_its_check_is_put_back(layout):
    app, root = layout
    (app / 'systemd/aios-embedding.socket').unlink()
    run = Recorder(enabled={'aios-control-plane.service', 'aios-embedding.socket'}, failing=('-t',))
    with pytest.raises(ValueError, match='aios from this release does not pass its check.*unknown directive'):
        system_files.sync(app, root, run)
    assert (root / 'etc/nginx/sites-available/aios').read_text() == 'client_max_body_size 32m;\n'
    assert not list(root.rglob('*.aios-previous'))
    assert ['systemctl', 'try-reload-or-restart', 'nginx'] not in run.calls


def test_firewall_rules_are_checked_then_loaded(layout):
    app, root = layout
    (app / 'config/nftables.conf').write_text('flush ruleset\ntable inet aios {}\n')
    run = Recorder(enabled={'aios-control-plane.service', 'aios-embedding.socket'})
    system_files.sync(app, root, run)
    path = str(root / 'etc/nftables.conf')
    assert run.calls.index(['nft', '-c', '-f', path]) < run.calls.index(['nft', '-f', path])


def test_the_repository_ships_every_file_it_enables():
    repo = Path(__file__).resolve().parents[1]
    for unit in system_files.enabled_units(repo):
        assert (repo / 'systemd' / unit).is_file(), unit
    assert 'enabled-units' in (repo / 'scripts/build-image.sh').read_text()
    assert '-m aios.system_files /opt/aios/app' in (repo / 'scripts/firstboot.sh').read_text()


def test_an_application_release_installs_them_and_a_rollback_restores_them(environment, monkeypatch, tmp_path):
    import base64
    import hashlib
    import io
    import tarfile
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    public = tmp_path / 'release.pub'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    opt = tmp_path / 'opt'
    (opt / 'app/backend/aios').mkdir(parents=True)
    (opt / 'app/backend/aios/app.py').write_text('old')
    archive = environment.DATA / 'backups' / (environment.uid() + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        entry = tarfile.TarInfo('backend/aios/app.py')
        entry.size = 3
        tar.addfile(entry, io.BytesIO(b'new'))
    manifest = {'component': 'application', 'version': 'v2', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    original = Path
    monkeypatch.setattr(platform, 'Path', lambda value: public if value == '/etc/aios-release.pub' else opt if value == '/opt/aios' else original(value))
    monkeypatch.setattr(platform.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(platform, 'run', lambda args, **kwargs: 'active')
    synced = []
    monkeypatch.setattr(platform.system_files, 'sync', lambda app: synced.append((Path(app) / 'backend/aios/app.py').read_text()) or [])
    monkeypatch.setattr(platform, 'answers', lambda component, attempts=20: (_ for _ in ()).throw(ValueError('did not answer')))
    payload = {'manifest': manifest, 'signature': base64.b64encode(key.sign(environment.encode(manifest).encode())).decode(), 'file': archive.name}
    with pytest.raises(ValueError, match='previous component restored'):
        platform.update_release(payload)
    # Installed from the new release, then from the one put back.
    assert synced == ['new', 'old']
