import hashlib
import httpx
import pytest
from aios.hardware import compatibility
from aios.providers import artifact, matches


def test_compatibility_ram_and_disk():
    hw={'ram':{'total':16*1024**3,'available':12*1024**3},'model_storage':{'free':100*1024**3},'physical_cores':8,'isa':['avx2'],'numa_nodes':{'node0':'0-7'}}
    model={'size':2*1024**3,'format':'GGUF'}
    assert compatibility(model,hw=hw)['classification']=='OPTIMAL'
    model['size']=20*1024**3
    assert compatibility(model,hw=hw)['classification']=='NOT_RECOMMENDED'
    model['size']=200*1024**3
    assert compatibility(model,hw=hw)['classification']=='INCOMPATIBLE'

def test_provider_filters():
    item=artifact('author/model','model-Q4_K_M.gguf','https://example.com/m',1024,revision='abc',license='MIT')
    assert item['quantization']=='Q4_K_M'
    assert matches(item,{'license':'MIT','author':'author','max_size':2048})
    assert not matches(item,{'max_size':100})

@pytest.mark.asyncio
async def test_download_verify_install_publish(admin,environment,discovered,tiny_gguf,monkeypatch):
    from aios import downloads
    key=discovered(size=len(tiny_gguf),sha=hashlib.sha256(tiny_gguf).hexdigest())
    response=admin.post(f'/api/v1/aios/models/{key}/install',json={'accept_license':True})
    assert response.status_code==200,response.text
    job=environment.one('SELECT * FROM downloads WHERE id=?',(response.json()['id'],))
    environment.execute("UPDATE downloads SET state='DOWNLOADING' WHERE id=?",(job['id'],))
    transport=httpx.MockTransport(lambda req:httpx.Response(200,content=tiny_gguf))
    original=httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx,'AsyncClient',lambda **kwargs:original(transport=transport,**kwargs))
    monkeypatch.setattr(downloads,'request_target',lambda url,headers,*args:(url,headers,{}))
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?',(job['id'],))['state']=='INSTALLED'
    assert admin.get('/v1/models').json()['data']==[]
    assert admin.patch(f'/api/v1/aios/models/{key}',json={'published':True}).status_code==200
    assert admin.get('/v1/models').json()['data'][0]['id']==key
    assert admin.post(f'/api/v1/aios/runtime/{key}/start').status_code==200
    assert admin.delete(f'/api/v1/aios/models/{key}').status_code==409
    environment.execute("UPDATE runtime_instances SET desired='STOPPED' WHERE model_id=?",(key,))
    assert admin.delete(f'/api/v1/aios/models/{key}').status_code==200
    assert not (environment.DATA/'models'/f'{key}.gguf').exists()

@pytest.mark.asyncio
async def test_checksum_failure_cleaned(environment,discovered,tiny_gguf,monkeypatch):
    from aios import downloads
    key=discovered(size=len(tiny_gguf),sha='0'*64)
    job={'id':environment.uid(),'model_id':key,'attempts':0}
    environment.execute('INSERT INTO downloads(id,model_id,state,total,created_at,updated_at) VALUES (?,?,?,?,?,?)',(job['id'],key,'DOWNLOADING',len(tiny_gguf),environment.now(),environment.now()))
    original=httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=tiny_gguf)),**kwargs))
    monkeypatch.setattr(downloads,'request_target',lambda url,headers,*args:(url,headers,{}))
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?',(job['id'],))['state']=='FAILED'
    assert not (environment.DATA/'downloads'/(job['id']+'.part')).exists()
    assert not environment.one('SELECT id FROM installed_models WHERE id=?',(key,))

@pytest.mark.asyncio
async def test_resume_uses_range(environment,discovered,tiny_gguf,monkeypatch):
    from aios import downloads
    key=discovered(size=len(tiny_gguf),sha=hashlib.sha256(tiny_gguf).hexdigest())
    job={'id':environment.uid(),'model_id':key,'attempts':0}
    environment.execute('INSERT INTO downloads(id,model_id,state,total,created_at,updated_at) VALUES (?,?,?,?,?,?)',(job['id'],key,'DOWNLOADING',len(tiny_gguf),environment.now(),environment.now()))
    (environment.DATA/'downloads'/(job['id']+'.part')).write_bytes(tiny_gguf[:32])
    def respond(req):
        assert req.headers['range']=='bytes=32-'
        return httpx.Response(206,headers={'content-range':f'bytes 32-{len(tiny_gguf)-1}/{len(tiny_gguf)}'},content=tiny_gguf[32:])
    original=httpx.AsyncClient
    monkeypatch.setattr(downloads.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    monkeypatch.setattr(downloads,'request_target',lambda url,headers,*args:(url,headers,{}))
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?',(job['id'],))['state']=='INSTALLED'

def test_license_policy(admin,environment,discovered):
    key=discovered()
    assert admin.post(f'/api/v1/aios/models/{key}/install',json={'accept_license':False}).status_code==422
    environment.set_setting('approved_licenses',['Apache-2.0'])
    assert admin.post(f'/api/v1/aios/models/{key}/install',json={'accept_license':True}).status_code==403

def test_database_restart(environment):
    environment.set_setting('persist',{'value':123})
    environment.initialize()
    assert environment.setting('persist')=={'value':123}

def test_runtime_configuration(environment,discovered,tiny_gguf):
    from aios.runtime import RuntimeConfig, arguments
    key=discovered()
    environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf) VALUES (?,?,?,?,?,?)',(key,str(environment.DATA/'models'/f'{key}.gguf'),'a'*64,environment.now(),'INSTALLED','{}'))
    config=RuntimeConfig(context=512,threads=2,batch=128)
    cmd=arguments({'model_id':key,'port':8090},config)
    assert '--ctx-size' in cmd and '512' in cmd
    with pytest.raises(ValueError,match='HugeTLB'):
        arguments({'model_id':key,'port':8090},RuntimeConfig(hugepages=True))

def test_openapi(admin):
    schema=admin.get('/api/aios/openapi.json').json()
    assert '/api/v1/aios/models/{key}/install' in schema['paths']
    assert schema['components']['schemas']['RuntimeConfig']['properties']['context']['minimum']==128

@pytest.mark.asyncio
async def test_completed_part_recovers_without_network(environment,discovered,tiny_gguf,monkeypatch):
    from aios import downloads
    key=discovered(size=len(tiny_gguf),sha=hashlib.sha256(tiny_gguf).hexdigest())
    job={'id':environment.uid(),'model_id':key,'attempts':0}
    environment.execute('INSERT INTO downloads(id,model_id,state,total,created_at,updated_at) VALUES (?,?,?,?,?,?)',(job['id'],key,'DOWNLOADING',len(tiny_gguf),environment.now(),environment.now()))
    (environment.DATA/'downloads'/(job['id']+'.part')).write_bytes(tiny_gguf)
    def no_network(*args, **kwargs):
        raise AssertionError('Complete .part must verify without issuing HTTP Range at EOF')
    monkeypatch.setattr(downloads.httpx,'AsyncClient',no_network)
    await downloads.download(job)
    assert environment.one('SELECT state FROM downloads WHERE id=?',(job['id'],))['state']=='INSTALLED'

@pytest.mark.asyncio
async def test_huggingface_repository_rename(environment,monkeypatch):
    from aios import providers
    repo=environment.one("SELECT * FROM repositories WHERE provider='huggingface'")
    repo['config']=environment.encode({'models':['old/model']})
    def respond(request):
        if request.url.path.endswith('/old/model'):
            return httpx.Response(307,headers={'location':'/api/models/new/model?blobs=true'})
        return httpx.Response(200,json={'id':'new/model','sha':'immutable','siblings':[{'rfilename':'m.gguf','lfs':{'size':100,'sha256':'a'*64}}],'cardData':{'license':'MIT'}})
    original=httpx.AsyncClient
    monkeypatch.setattr(providers.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    monkeypatch.setattr(providers,'request_target',lambda url,headers,*args:(url,headers,{}))
    items=await providers.huggingface(repo)
    assert items[0]['model_id']=='new/model'
    assert '/new/model/resolve/immutable/' in items[0]['url']

def test_runtime_autostart_is_explicit(environment,discovered):
    from aios.runtime import initialize_runtime_state
    for enabled in (False,True):
        key=discovered()
        environment.execute('INSERT INTO installed_models(id,path,sha256,installed_at,state,gguf,config) VALUES (?,?,?,?,?,?,?)',(key,str(environment.DATA/'models'/f'{key}.gguf'),'a'*64,environment.now(),'INSTALLED','{}',environment.encode({'autostart':enabled})))
        environment.execute("INSERT INTO runtime_instances(id,model_id,state,port,config,desired) VALUES (?,?,'RUNNING',8090,'{}','RUNNING')",(environment.uid(),key))
        initialize_runtime_state()
        instance=environment.one('SELECT * FROM runtime_instances WHERE model_id=?',(key,))
        assert instance['state']=='STOPPED'
        assert instance['desired']==('RUNNING' if enabled else 'STOPPED')
