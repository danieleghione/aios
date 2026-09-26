"""The engine registry: one table says what each engine runs, where it listens
and how it starts, and only language models reach the chat's model list."""
import json


def test_every_kind_has_one_engine_on_its_own_port():
    from aios.runtime import ENGINES, KIND_NAMES, PORTS, kind_of
    assert set(ENGINES) == {'text', 'image', 'speech', 'voice'}
    assert len(set(PORTS.values())) == len(PORTS) and PORTS['text'] == 8090
    assert KIND_NAMES == {'text': 'language', 'image': 'image', 'speech': 'speech', 'voice': 'voice'}
    assert [kind_of({'kind': k}) for k in ('image', 'speech', 'voice', None)] == ['image', 'speech', 'voice', 'text']
    assert ENGINES['speech'].ready(400) and not ENGINES['text'].ready(503) and ENGINES['image'].ready(200)


def test_only_language_models_are_offered_to_the_chat(admin, environment):
    (environment.ETC / 'secrets').mkdir(parents=True, exist_ok=True)
    (environment.ETC / 'secrets/inference-key').write_text('chat-key')
    repo = environment.uid()
    environment.execute('INSERT INTO repositories(id,name,provider,url,config,enabled) VALUES (?,?,?,?,?,1)', (repo, 'R', 'http', 'https://example.com/m.json', '{}'))
    names = {}
    for kind in ('text', 'image', 'speech', 'voice'):
        key = environment.uid()
        metadata = {'display_name': kind + ' model', 'size': 1, **({'kind': kind} if kind != 'text' else {})}
        environment.execute('INSERT INTO discovered_models VALUES (?,?,?,?,?,?,?)', (key, repo, key, 'r', json.dumps(metadata), 0, 0))
        environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,published,gguf) VALUES (?,?,?,?,?,1,?)',
                            (key, '/m', 'a' * 64, 0, 'PUBLISHED', '{}'))
        names[key] = kind
    listed = admin.get('/v1/models', headers={'Authorization': 'Bearer chat-key'}).json()['data']
    assert [names[m['id']] for m in listed] == ['text']
