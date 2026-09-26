"""Usability items of the September 2026 audit (internal/BACKLOG.md, block 3)."""
import datetime
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'frontend-admin/src'


class _Portal:
    """Every source file of the portal, read as one text."""
    def read_text(self):
        return '\n'.join(p.read_text() for p in sorted(SRC.rglob('*.ts*')) if not p.name.endswith('.test.tsx'))


APP = _Portal()


@pytest.mark.parametrize('name,total,active', [
    ('Qwen3-30B-A3B-Instruct-2507-GGUF', 30e9, 3e9), ('SmolLM2-360M-Instruct', 360e6, None), ('gemma-3n-E4B-it', 4e9, None),
    ('Qwen3-VL-30B-A3B-Thinking-1M-GGUF', 30e9, 3e9), ('DeepSeek-R1-Distill-Qwen-1.5B', 1.5e9, None), ('gpt-oss-20b', 20e9, None),
    ('Mixtral-8x7B-v0.1', 56e9, None), ('phi-4', None, None), ('model-Q4_K_M', None, None)])
def test_parameters_from_name(name, total, active):
    from aios.providers import parameters_from_name
    assert parameters_from_name(name) == (int(total) if total else None, int(active) if active else None)


def test_artifact_marks_derived_parameters():
    from aios.providers import artifact
    row = artifact('Qwen/Qwen3-8B-GGUF', 'Qwen3-8B-Q4_K_M.gguf', 'https://example.com/x.gguf', 100)
    assert row['parameter_count'] == 8_000_000_000 and row['parameter_source'] == 'name'
    declared = artifact('Qwen/Qwen3-8B-GGUF', 'Qwen3-8B-Q4_K_M.gguf', 'https://example.com/x.gguf', 100, parameter_count=8_190_000_000)
    assert declared['parameter_count'] == 8_190_000_000 and 'parameter_source' not in declared


def variant(key, quant, size, fit):
    return {'id': key, 'model_id': 'org/model', 'repository_id': 'r', 'quantization': quant, 'size': size, 'state': 'DISCOVERED',
            'compatibility': {'classification': fit}, 'release_date': '2026-01-0' + key[-1]}


def test_recommended_variant_prefers_fit_then_usual_quantisation():
    from aios.app import recommended_variant
    variants = [variant('a1', 'Q8_0', 800, 'COMPATIBLE'), variant('a2', 'Q4_K_M', 450, 'OPTIMAL'), variant('a3', 'Q2_K', 300, 'OPTIMAL'),
                variant('a4', 'F16', 1600, 'NOT_RECOMMENDED')]
    assert recommended_variant(variants)['id'] == 'a2'
    assert recommended_variant([variant('b1', 'Q2_K', 300, 'OPTIMAL'), variant('b2', 'Q3_K_L', 400, 'OPTIMAL')])['id'] == 'b2'


def test_group_variants_one_entry_per_model():
    from aios.app import group_variants
    items = [variant('a1', 'Q8_0', 800, 'COMPATIBLE'), variant('a2', 'Q4_K_M', 450, 'OPTIMAL'), {**variant('c1', 'Q4_K_M', 10, 'OPTIMAL'), 'model_id': 'org/other'}]
    items[1]['state'] = 'INSTALLED'
    groups = group_variants(items)
    assert [g['model_id'] for g in groups] == ['org/model', 'org/other']
    first = groups[0]
    assert first['recommended'] == 'a2' and first['installed'] == 1 and [v['id'] for v in first['variants']] == ['a2', 'a1']
    assert first['release_date'] == '2026-01-02'


def test_catalog_grouped(admin, discovered):
    discovered(url='https://example.com/a.gguf'), discovered(url='https://example.com/b.gguf')
    flat = admin.get('/api/v1/aios/catalog').json()
    grouped = admin.get('/api/v1/aios/catalog', params={'group': True}).json()
    assert flat['total'] == 2 and grouped['total'] == 1 and len(grouped['items'][0]['variants']) == 2


def test_journal_messages_are_text():
    from aios.app import journal_message
    assert journal_message(list(b'\x1b[32mINFO\x1b[0m started')) == 'INFO started'
    assert journal_message('plain \x1b[1mbold\x1b[0m') == 'plain bold'
    assert journal_message(None) == ''


def test_logs_newest_first_and_more_sources(admin, monkeypatch):
    import subprocess
    lines = [json.dumps({'MESSAGE': m, '__REALTIME_TIMESTAMP': str(t * 1_000_000), 'PRIORITY': p}) for m, t, p in (('old', 1, '6'), ('new', 2, '3'))]
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '\n'.join(lines), ''))
    body = admin.get('/api/v1/aios/logs', params={'source': 'repository-sync'}).json()
    assert [x['message'] for x in body['items']] == ['new', 'old'] and body['items'][0]['level'] == 'error'
    assert {'nvidia-driver', 'hardware-profiler', 'update-check'} <= set(body['sources'])
    assert admin.get('/api/v1/aios/logs', params={'source': 'sshd'}).status_code == 422


def test_audit_names_the_actor(admin):
    admin.post('/api/v1/aios/users', json={'username': 'someone', 'password': 'Someone-password-1', 'role': 'VIEWER'})
    item = admin.get('/api/v1/aios/audit').json()['items'][0]
    assert item['actor_name'] == 'admin'


def test_current_certificate(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from aios.system_state import certificate
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'aios.local')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=20))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('aios.local')]), False).sign(key, hashes.SHA256()))
    path = tmp_path / 'c.crt'
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    info = certificate(path)
    assert info['self_signed'] and info['names'] == ['aios.local'] and 18 <= info['days_left'] <= 20
    assert certificate(tmp_path / 'missing.crt') is None


def test_dashboard_history_and_active_models(admin, monkeypatch):
    from aios import routes_system as module
    monkeypatch.setattr(module, 'cached_profile', lambda: {'cpu_percent': 1.0})
    module.CPU_HISTORY.extend([10.0, 20.0])
    body = admin.get('/api/v1/aios/system/dashboard').json()
    assert body['cpu_history'][-2:] == [10.0, 20.0] and body['active_models'] == []


def test_portal_uses_its_own_confirmation():
    source = APP.read_text()
    assert source.count('confirm(') == 1  # only the fallback before the app mounts
    assert "'Operation completed.'" not in source


def test_navigation_is_grouped_and_empty_pages_point_somewhere():
    source = APP.read_text()
    assert "['MODELS', [" in source and 'menu-toggle' in source
    assert "next={['Open the catalogue','Catalogue']}" in source


def test_hostname_rule():
    from pydantic import ValidationError
    from aios.platform import SystemConfig
    SystemConfig(hostname='aios-lab', timezone='UTC')
    for bad in ('-lab', 'lab-', 'a' * 64, 'bad;name'):
        with pytest.raises(ValidationError):
            SystemConfig(hostname=bad, timezone='UTC')


def test_client_api_keys(admin, environment):
    created = admin.post('/api/v1/aios/apikeys', json={'name': 'n8n', 'expires_days': 30}).json()
    assert created['key'].startswith('aios_')
    listed = admin.get('/api/v1/aios/apikeys').json()['items']
    assert listed[0]['name'] == 'n8n' and 'digest' not in listed[0] and created['key'] not in json.dumps(listed)
    from aios.app import inference_authorised
    assert inference_authorised('Bearer ' + created['key'])
    assert not inference_authorised('Bearer aios_wrong')
    assert admin.delete(f"/api/v1/aios/apikeys/{created['id']}").status_code == 200
    assert not inference_authorised('Bearer ' + created['key'])
    expired = admin.post('/api/v1/aios/apikeys', json={'name': 'old', 'expires_days': 1}).json()
    environment.execute('UPDATE api_keys SET expires_at=1 WHERE id=?', (expired['id'],))
    assert not inference_authorised('Bearer ' + expired['key'])
    assert created['key'] not in json.dumps(admin.get('/api/v1/aios/audit').json())
