"""Real HTTPS discovery/download and llama.cpp inference against a running appliance."""
import json
import time


def wait_for(fetch, predicate, timeout=180):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=fetch()
        if predicate(result):
            return result
        time.sleep(2)
    raise AssertionError('Timed out waiting for appliance transition: '+json.dumps(result)[:1000])


def model_smoke(client):
    prefix='/api/v1/aios/'
    def call(path, method='GET', body=None):
        response=client.request(method,prefix+path,json=body)
        response.raise_for_status()
        return response.json()
    repo=next(r for r in call('repositories')['items'] if r['provider']=='huggingface')
    call('repositories/'+repo['id'],'PUT',{'name':'Hugging Face smoke','provider':'huggingface','url':'https://huggingface.co','enabled':False,'config':{'models':['ggml-org/models'],'filters':{'max_size':2000000}}})
    call('repositories/'+repo['id']+'/sync','POST')
    wait_for(lambda:call('repositories'),lambda r:next(x for x in r['items'] if x['id']==repo['id'])['status']!='SYNCING')
    catalog=call('catalog?q=stories260K.gguf')['items']
    model=next(m for m in catalog if m['filename']=='tinyllamas/stories260K.gguf')
    job=call('models/'+model['id']+'/install','POST',{'accept_license':True,'override_compatibility':True})
    state=wait_for(lambda:call('downloads'),lambda r:next(x for x in r['items'] if x['id']==job['id'])['state'] in ('INSTALLED','FAILED'))
    download=next(x for x in state['items'] if x['id']==job['id'])
    assert download['state']=='INSTALLED',download
    assert model['id'] not in [m['id'] for m in client.get('/v1/models').json()['data']]
    call('models/'+model['id'],'PATCH',{'published':True,'config':{'profile':'CUSTOM','context':256,'threads':2,'batch':64,'parallel':1,'timeout':60}})
    assert model['id'] in [m['id'] for m in client.get('/v1/models').json()['data']]
    call('runtime/'+model['id']+'/start','POST')
    runtime=wait_for(lambda:call('runtime'),lambda r:any(x['model_id']==model['id'] and x['state'] in ('RUNNING','FAILED') for x in r['items']))
    instance=next(x for x in runtime['items'] if x['model_id']==model['id'])
    if instance['state']!='RUNNING':
        raise AssertionError(call('runtime/'+instance['id']+'/log'))
    return {'model':model['id'],'sha256':next(m for m in call('models')['items'] if m['id']==model['id'])['sha256'],'runtime':instance['state']}
