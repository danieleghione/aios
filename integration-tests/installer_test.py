"""Destructive installer acceptance test confined to newly created virtual disks."""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'build/installer-test'
WORK.mkdir(parents=True,exist_ok=True)
os.chmod(WORK,0o700)
SOURCE=WORK/'source.qcow2'
TARGET=WORK/'target.qcow2'
for disk in (SOURCE,TARGET):
    disk.unlink(missing_ok=True)
subprocess.run(['qemu-img','create','-f','qcow2','-F','raw','-b',str(ROOT/'dist/aios-x86_64.img'),str(SOURCE)],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['qemu-img','create','-f','qcow2',str(TARGET),'16G'],check=True,stdout=subprocess.DEVNULL)
shutil.copyfile('/usr/share/OVMF/OVMF_VARS_4M.fd',WORK/'vars.fd')
log=WORK/'serial.log'
report={'checks':[],'started_at':time.time()}
process=None
console=None

def start(installed=False):
    global process,console
    log.write_text('')
    os.chmod(log,0o600)
    for name in ('console.sock','monitor.sock'):
        (WORK/name).unlink(missing_ok=True)
    drives=['-drive',f'file={TARGET if installed else SOURCE},format=qcow2,if=virtio']
    if not installed:
        drives+=['-drive',f'file={TARGET},format=qcow2,if=virtio']
    accel=['-enable-kvm','-cpu','host'] if os.access('/dev/kvm',os.R_OK|os.W_OK) else ['-accel','tcg','-cpu','max']
    cmd=['qemu-system-x86_64',*accel,'-m','3072','-smp','2','-drive','if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd','-drive',f'if=pflash,format=raw,file={WORK/"vars.fd"}',*drives,'-netdev','user,id=n1,hostfwd=tcp:127.0.0.1:19443-:443','-device','virtio-net-pci,netdev=n1','-display','none','-chardev',f'socket,id=serial0,path={WORK/"console.sock"},server=on,wait=off,logfile={log}','-serial','chardev:serial0','-monitor',f'unix:{WORK/"monitor.sock"},server=on,wait=off']
    process=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=(WORK/'stderr.log').open('w'))
    console=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    for _ in range(100):
        try:
            console.connect(str(WORK/'console.sock'));break
        except (FileNotFoundError,ConnectionRefusedError):time.sleep(.1)
    def drain(sock):
        try:
            while sock.recv(65536):pass
        except OSError:return
    threading.Thread(target=drain,args=(console,),daemon=True).start()
    wait('Selection:',180)

def wait(text,timeout=120,after=0):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        content=log.read_text(errors='replace')[after:]
        if text in content:return content
        if process.poll() is not None:raise AssertionError('QEMU exited: '+(WORK/'stderr.log').read_text())
        time.sleep(1)
    raise AssertionError('Console timeout: '+text)

def shutdown():
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as monitor:
        monitor.connect(str(WORK/'monitor.sock'));monitor.sendall(b'system_powerdown\n')
    process.wait(timeout=90)
    console.close()

try:
    start()
    with httpx.Client(base_url='https://127.0.0.1:19443',verify=False,timeout=30) as client:
        assert not client.get('/api/v1/aios/auth/status').json()['initialized']
    original_secret=re.search(r'Admin bootstrap secret \(valid 24h\): (\S+)',log.read_text()).group(1)
    console.sendall(b'4\n');time.sleep(.3)
    # Verify the installer refuses its live source disk before any erase prompt.
    offset=len(log.read_text(errors='replace'))
    console.sendall(b"printf '/dev/vda\\n' | /opt/aios/app/installer/install.sh\n")
    wait('is not one of the usable disks',30,offset)
    report['checks'].append('Installer rejects the currently booted source disk')
    # Only /dev/vdb, the blank qcow2 created above, is explicitly confirmed.
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'/opt/aios/app/installer/install.sh\n')
    wait('Target disk',30,offset)
    # 'no' to SSH keeps this path at the shipped default; iso_test covers SSH.
    answers=['/dev/vdb','it_IT.UTF-8','it','aios-installed','','dhcp','','Europe/Rome','ntp','ntp.ubuntu.com','no','si']
    console.sendall(('\n'.join(answers)+'\n').encode())
    wait('Installation completed.',1200,offset)
    report['checks'].append('Explicitly confirmed GPT/ESP/root/data installation onto blank virtual disk')
    shutdown()
    # Detach the source entirely and boot the installed target as the only disk.
    start(installed=True)
    text=log.read_text(errors='replace')
    assert 'AIOS READY' in text
    new_secret=re.search(r'Admin bootstrap secret \(valid 24h\): (\S+)',text).group(1)
    assert new_secret!=original_secret
    with httpx.Client(base_url='https://127.0.0.1:19443',verify=False,timeout=30) as client:
        client.get('/admin/').raise_for_status()
        assert not client.get('/api/v1/aios/auth/status').json()['initialized']
        assert client.get('/health').json()['database']
    report['checks'].append('Installed disk boots independently via UEFI with DHCP, HTTPS and new bootstrap identity')
    # The first boot relinks /etc/default/locale, so prove the chosen locale and
    # keyboard survive that boot instead of only being written by the installer.
    console.sendall(b'4\n');time.sleep(.3)
    offset=len(log.read_text(errors='replace'))
    console.sendall(("python3 -c \"import pathlib,subprocess; "
        "assert 'LANG=it_IT.UTF-8' in pathlib.Path('/etc/default/locale').read_text(); "
        "assert 'it_IT.utf8' in subprocess.check_output(['locale','-a'],text=True); "
        "assert 'XKBLAYOUT='+chr(34)+'it'+chr(34) in pathlib.Path('/etc/default/keyboard').read_text(); "
        "print('RAW_LOCALE_'+'VERIFIED')\"\n").encode())
    wait('RAW_LOCALE_VERIFIED',60,offset)
    report['checks'].append('Chosen locale and keyboard survive the first boot of the installed disk')
    shutdown()
    report['status']='PASSED'
except BaseException as exc:
    report['status']='FAILED';report['error']=str(exc)
    raise
finally:
    if process and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:process.kill();process.wait()
    if console:console.close()
    report['finished_at']=time.time()
    (ROOT/'dist/installer-test-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
