import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
DIST=ROOT/'dist'
BUILD=ROOT/'build/qemu-test'
BUILD.mkdir(parents=True,exist_ok=True)
os.chmod(BUILD,0o700)
IMAGE=DIST/'aios-x86_64.img'
code=Path('/usr/share/OVMF/OVMF_CODE_4M.fd')
var=Path('/usr/share/OVMF/OVMF_VARS_4M.fd')
if not code.exists():
    code=Path('/usr/share/OVMF/OVMF_CODE.fd');var=Path('/usr/share/OVMF/OVMF_VARS.fd')
shutil.copyfile(var,BUILD/'vars.fd')
overlay=BUILD/'overlay.qcow2'
overlay.unlink(missing_ok=True)
subprocess.run(['qemu-img','create','-f','qcow2','-F','raw','-b',str(IMAGE),str(overlay)],check=True,stdout=subprocess.DEVNULL)
logpath=BUILD/'serial.log'
logpath.write_text('')
os.chmod(logpath,0o600)
port=int(os.environ.get('AIOS_TEST_PORT','18443'))
monitor=BUILD/'monitor.sock'
monitor.unlink(missing_ok=True)
(BUILD/'console.sock').unlink(missing_ok=True)
accel=['-enable-kvm','-cpu','host'] if os.access('/dev/kvm',os.R_OK|os.W_OK) else ['-accel','tcg','-cpu','max']
cmd=['qemu-system-x86_64',*accel,'-m',os.environ.get('AIOS_TEST_RAM','3072'),'-smp','2','-drive',f'if=pflash,format=raw,readonly=on,file={code}','-drive',f'if=pflash,format=raw,file={BUILD/"vars.fd"}','-drive',f'file={overlay},format=qcow2,if=virtio','-netdev',f'user,id=n1,hostfwd=tcp:127.0.0.1:{port}-:443','-device','virtio-net-pci,netdev=n1','-display','none','-chardev',f'socket,id=serial0,path={BUILD/"console.sock"},server=on,wait=off,logfile={logpath}', '-serial','chardev:serial0','-monitor',f'unix:{monitor},server=on,wait=off','-no-reboot']
console=None
results={'started_at':time.time(),'acceleration':'KVM' if '-enable-kvm' in accel else 'TCG','checks':[]}
process=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(BUILD/'qemu.stderr').open('w'))
try:
    console=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    for _ in range(100):
        try:
            console.connect(str(BUILD/'console.sock'))
            break
        except (FileNotFoundError,ConnectionRefusedError):
            time.sleep(.1)
    # Drain output so the guest console cannot block on a full Unix socket.
    import threading
    def drain(sock):
        try:
            while sock.recv(65536):
                pass
        except OSError:
            return
    threading.Thread(target=drain,args=(console,),daemon=True).start()
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        if process.poll() is not None:
            raise AssertionError('QEMU exited before ready: '+(BUILD/'qemu.stderr').read_text())
        text=logpath.read_text(errors='replace')
        if 'Kernel panic' in text:
            raise AssertionError('Kernel panic during UEFI boot; see protected serial.log')
        if 'AIOS READY' in text:
            break
        time.sleep(2)
    else:
        raise AssertionError('AIOS READY not found in serial console within 600s')
    results['checks'].append('UEFI boot and AIOS READY')
    with httpx.Client(base_url=f'https://127.0.0.1:{port}',verify=False,timeout=120) as client:
        response=client.get('/health');response.raise_for_status();assert response.json()['database']
        results['checks'].append('HTTPS health and SQLite')
        response=client.get('/admin/');response.raise_for_status();assert '<div id="root">' in response.text
        for asset in re.findall(r'(?:src|href)="(/admin/assets/[^"]+)"',response.text):
            client.get(asset).raise_for_status()
        results['checks'].append('Admin HTML and all JS/CSS assets')
        assert client.get('/api/v1/aios/models').status_code==401
        token=re.search(r'Admin bootstrap secret \(valid 24h\): ([A-Za-z0-9_-]+)',text).group(1)
        password=secrets.token_urlsafe(24)
        response=client.post('/api/v1/aios/auth/bootstrap',json={'username':'qa-admin','password':password,'secret':token});response.raise_for_status()
        response=client.post('/api/v1/aios/auth/login',json={'username':'qa-admin','password':password});response.raise_for_status()
        client.headers['x-csrf-token']=response.json()['csrf']
        client.get('/api/v1/aios/hardware/profile').raise_for_status()
        results['checks'].append('One-time bootstrap, login, CSRF, hardware profiler')
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            try:
                response=client.get('/api/config')
                if response.status_code==200 and 'features' in response.json():
                    break
            except (httpx.HTTPError,ValueError):
                pass
            time.sleep(3)
        else:
            raise AssertionError('Open WebUI did not become ready')
        client.get('/').raise_for_status()
        results['checks'].append('Open WebUI HTTP and configuration API')
        assert client.get('/v1/models').json()['data']==[]
        results['checks'].append('Public models empty before publication')
        if os.environ.get('AIOS_MODEL_SMOKE','1')=='1':
            spec=importlib.util.spec_from_file_location('model_smoke',ROOT/'integration-tests/model_smoke.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            results['model']=module.model_smoke(client)
            response=client.post('/api/v1/auths/signin',json={'email':'ignored@example.org','password':''});response.raise_for_status()
            webui_token=response.json()['token']
            response=client.get('/api/models',headers={'Authorization':'Bearer '+webui_token});response.raise_for_status()
            assert results['model']['model'] in [m['id'] for m in response.json()['data']]
            response=client.post('/api/chat/completions',headers={'Authorization':'Bearer '+webui_token},json={'model':results['model']['model'],'messages':[{'role':'user','content':'Once upon a time'}],'stream':False,'max_tokens':8});response.raise_for_status()
            body=response.json();assert body.get('choices'),body
            results['model']['chat_response']=body['choices'][0]['message']['content']
            results['checks'].append('Real HTTPS GGUF download, checksum, install, publish, llama.cpp load and chat through Open WebUI')
        if os.environ.get('AIOS_BROWSER_TEST','0')=='1':
            credentials=BUILD/'browser-credentials.json'
            credentials.write_text(json.dumps({'base':str(client.base_url).rstrip('/'),'username':'qa-admin','password':password}))
            os.chmod(credentials,0o600)
            import pwd
            browser_env={**os.environ,'AIOS_BROWSER_CREDENTIALS':str(credentials),'AIOS_BROWSER_OUTPUT':str(DIST/'screenshots'),'PLAYWRIGHT_BROWSERS_PATH':str(Path(pwd.getpwnam(os.environ.get('SUDO_USER','root')).pw_dir)/'.cache/ms-playwright')}
            try:
                subprocess.run(['node','admin-smoke.mjs'],cwd=ROOT/'frontend-admin',env=browser_env,check=True,timeout=180)
            finally:
                credentials.unlink(missing_ok=True)
            results['checks'].append('Chromium: all admin pages, desktop/mobile layout, no JavaScript errors')
        if os.environ.get('AIOS_ADMIN_TEST','0')=='1' and results.get('model'):
            spec=importlib.util.spec_from_file_location('appliance_checks',ROOT/'integration-tests/appliance_checks.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            results['checks'].extend(module.administrative_checks(client,password,results['model']['model']))
        original_data_size=client.get('/api/v1/aios/hardware/profile').json()['model_storage']['total']
        results['checks'].append('Audit API')
        client.get('/api/v1/aios/audit').raise_for_status()
    # ACPI shutdown preserves the overlay for persistence investigation.
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as control:
        control.connect(str(monitor));control.sendall(b'system_powerdown\n')
    process.wait(timeout=90)
    results['checks'].append('Graceful ACPI shutdown')
    console.close()
    subprocess.run(['qemu-img','resize',str(overlay),'16G'],check=True,stdout=subprocess.DEVNULL)
    logpath.write_text('')
    process=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(BUILD/'qemu-reboot.stderr').open('w'))
    console=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    for _ in range(100):
        try:
            console.connect(str(BUILD/'console.sock'))
            break
        except (FileNotFoundError,ConnectionRefusedError):
            time.sleep(.1)
    threading.Thread(target=drain,args=(console,),daemon=True).start()
    deadline=time.monotonic()+300
    while time.monotonic()<deadline:
        if 'AIOS READY' in logpath.read_text(errors='replace'):
            break
        if process.poll() is not None:
            raise AssertionError('Persistence boot exited')
        time.sleep(2)
    else:
        raise AssertionError('Persistence boot did not become ready')
    with httpx.Client(base_url=f'https://127.0.0.1:{port}',verify=False,timeout=60) as client:
        assert client.get('/api/v1/aios/auth/status').json()['initialized']
        response=client.post('/api/v1/aios/auth/login',json={'username':'qa-admin','password':password});response.raise_for_status()
        client.headers['x-csrf-token']=response.json()['csrf']
        hardware=client.get('/api/v1/aios/hardware/profile').json()
        assert hardware['model_storage']['total']>original_data_size+2*1024**3
        if results.get('model'):
            models=client.get('/api/v1/aios/models').json()['items']
            model=next(m for m in models if m['id']==results['model']['model'])
            assert model['published'] and model['sha256']==results['model']['sha256']
            if model['config'].get('autostart'):
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    runtime=client.get('/api/v1/aios/runtime').json()['items']
                    if any(r['model_id']==model['id'] and r['state']=='RUNNING' for r in runtime):
                        break
                    time.sleep(2)
                else:
                    raise AssertionError('Autostart failed after reboot')
        results['checks'].append('Second boot: users, published models, checksums, configuration and data-partition expansion persist')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as control:
        control.connect(str(monitor));control.sendall(b'system_powerdown\n')
    process.wait(timeout=90)
    results['status']='PASSED'
except BaseException as exc:
    results['status']='FAILED';results['error']=str(exc)
    if console and process.poll() is None:
        try:
            console.sendall(b'4\n')
            time.sleep(.5)
            console.sendall(b'journalctl -u aios-firstboot -u aios-control-plane -u aios-open-webui -u aios-runtime-manager -u aios-platform -n 80 --no-pager\n')
            time.sleep(2)
        except OSError:
            pass
    raise
finally:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.kill();process.wait()
    if console:
        console.close()
    results['finished_at']=time.time()
    (DIST/'qemu-test-report.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
