"""Administrative operations exercised on the real VM, including backup restore."""
import time
import httpx


def administrative_checks(client, password, model_id):
    prefix='/api/v1/aios/'
    checks=[]
    def call(path, method='GET', data=None):
        result=client.request(method,prefix+path,json=data)
        result.raise_for_status()
        return result.json()
    def job_done(key, timeout=180):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            try:
                jobs=call('system/jobs')['items']
                item=next((j for j in jobs if j['id']==key),None)
                if item and item['state'] in ('COMPLETED','FAILED'):
                    assert item['state']=='COMPLETED',item
                    return item['result']
            except httpx.HTTPError:
                pass
            time.sleep(2)
        raise AssertionError('System job timeout: '+key)
    call('system/policy','PUT',{'download_concurrency':3,'approved_licenses':[]})
    backup=call('backups','POST',{'include_models':False})
    archive=job_done(backup['id'])
    response=client.get(prefix+'backups/'+archive['file']);response.raise_for_status()
    assert response.content[:2]==b'\x1f\x8b'
    checks.append('Consistent DB/config/Open WebUI backup and authenticated download')
    call('system/policy','PUT',{'download_concurrency':1,'approved_licenses':['MIT']})
    restored=call('backups/restore','POST',{'file':archive['file'],'confirm':'RESTORE'})
    # Restore replaces the DB and revokes sessions; log in again once it is ready.
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        try:
            response=client.get(prefix+'auth/me')
            if response.status_code==401:
                login=client.post(prefix+'auth/login',json={'username':'qa-admin','password':password})
                if login.status_code==200:
                    client.headers['x-csrf-token']=login.json()['csrf']
                    if call('system/settings')['download_concurrency']==3:
                        break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    else:
        raise AssertionError('Restore did not recover database/settings/login')
    job_done(restored['id'])
    assert call('system/settings')['approved_licenses']==[]
    assert model_id in [m['id'] for m in call('models')['items']]
    checks.append('Validated restore, session revocation, restored settings and retained GGUF')
    # Use current DHCP values, verify timer behavior without disconnecting the VM.
    interface=next(k for k in call('hardware/profile')['network'] if k!='lo')
    network=call('system/network','POST',{'interface':interface,'dhcp':True,'dns':[]})
    result=job_done(network['id'])
    assert result['confirmation_required']==network['id']
    confirmed=call('system/network/'+network['id']+'/confirm','POST')
    job_done(confirmed['id'])
    checks.append('Validated netplan apply and timed network confirmation')
    # No model should auto-load unless explicitly requested by its configuration.
    models=call('models')['items']
    model=next(m for m in models if m['id']==model_id)
    config={**model['config'],'autostart':True}
    call('models/'+model_id,'PATCH',{'config':config,'default':True})
    return checks
