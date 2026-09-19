import pytest
from aios import providers

@pytest.mark.asyncio
async def test_modelscope_metadata(environment,monkeypatch):
    repo=environment.one("SELECT * FROM repositories WHERE provider='modelscope'")
    repo['config']=environment.encode({'models':['org/model'],'revision':'commit','license':'Apache-2.0'})
    async def metadata(*args,**kwargs):
        return {'Code':200,'Data':{'Files':[{'Path':'weights-Q8_0.gguf','Size':1024,'Sha256':'a'*64,'Revision':'commit'},{'Path':'config.json','Size':10}]}}
    monkeypatch.setattr(providers,'request_json',metadata)
    items=await providers.modelscope(repo)
    assert len(items)==1 and items[0]['sha256']=='a'*64
    assert 'Revision=commit' in items[0]['url']

@pytest.mark.asyncio
async def test_github_release_assets(environment,monkeypatch):
    repo=environment.one("SELECT * FROM repositories WHERE provider='github'")
    repo['config']=environment.encode({'models':['org/model'],'license':'MIT'})
    async def metadata(*args,**kwargs):
        return [{'tag_name':'v1','published_at':'2026-01-01','assets':[{'name':'model.gguf','size':2000,'digest':'sha256:'+'b'*64,'browser_download_url':'https://github.com/org/model/releases/download/v1/model.gguf'}]}]
    monkeypatch.setattr(providers,'request_json',metadata)
    item=(await providers.github(repo))[0]
    assert item['revision']=='v1' and item['sha256']=='b'*64

@pytest.mark.asyncio
async def test_manifest_rejects_large_collection(environment,monkeypatch):
    repo=dict(environment.one("SELECT * FROM repositories WHERE provider='internal'"))
    repo['url']='https://example.com/manifest.json'  # a configured repository, so the manifest itself is judged
    async def metadata(*args,**kwargs):
        return {'schema_version':1,'models':[{}]*10001}
    monkeypatch.setattr(providers,'request_json',metadata)
    with pytest.raises(ValueError,match='Invalid AIOS manifest'):
        await providers.generic(repo)
