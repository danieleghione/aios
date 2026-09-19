"""Optical installation acceptance on both the UEFI and the legacy BIOS firmware paths."""
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
WORK=ROOT/'build/iso-test'
WORK.mkdir(parents=True,exist_ok=True)
os.chmod(WORK,0o700)
ISO=ROOT/'dist/aios-installer-x86_64.iso'
TARGET=WORK/'target.qcow2'
log=WORK/'serial.log'
# The BIOS profile reproduces what Proxmox configures by default for a new VM:
# SeaBIOS, i440fx, one core and 2 GiB, with the VirtIO SCSI single controller.
PROFILES=[{'name':'uefi','machine':'q35','memory':4096,'cpus':2,'uefi':True,
           'label':'OVMF/UEFI on q35 with 4 GiB and 2 cores'},
          {'name':'bios','machine':'pc','memory':2048,'cpus':1,'uefi':False,
           'label':"SeaBIOS on i440fx with Proxmox' default 2 GiB and 1 core"}]
KEY=WORK/'recovery-key'
if not KEY.exists():
    subprocess.run(['ssh-keygen','-t','ed25519','-N','','-C','iso-test','-f',str(KEY)],check=True,stdout=subprocess.DEVNULL)
PUBKEY=(WORK/'recovery-key.pub').read_text().strip()
report={'checks':[],'profiles':{},'started_at':time.time()}

def ssh_recovery(*command):
    """Log in as the restricted recovery account; never allocates a terminal."""
    result=subprocess.run(['ssh','-i',str(KEY),'-p','20022','-T','-o','StrictHostKeyChecking=no',
        '-o','UserKnownHostsFile=/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=20',
        'aios-recovery@127.0.0.1',*command],capture_output=True,text=True,timeout=60)
    return result.stdout+result.stderr
process=None
console=None

def start(profile,installed=False):
    global process,console
    log.write_text('')
    os.chmod(log,0o600)
    for name in ('console.sock','monitor.sock'):
        (WORK/name).unlink(missing_ok=True)
    firmware=['-drive','if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd',
              '-drive',f'if=pflash,format=raw,file={WORK/"vars.fd"}'] if profile['uefi'] else []
    drives=['-device','virtio-scsi-pci,id=scsi0','-drive',f'file={TARGET},format=qcow2,if=none,id=target','-device','scsi-hd,drive=target,bus=scsi0.0']
    if not installed:
        drives+=['-cdrom',str(ISO),'-boot','d']
    accel=['-enable-kvm','-cpu','host'] if os.access('/dev/kvm',os.R_OK|os.W_OK) else ['-accel','tcg','-cpu','max']
    cmd=['qemu-system-x86_64',*accel,'-machine',profile['machine'],'-m',str(profile['memory']),'-smp',str(profile['cpus']),*firmware,*drives,'-netdev','user,id=n1,hostfwd=tcp:127.0.0.1:20443-:443,hostfwd=tcp:127.0.0.1:20022-:22','-device','virtio-net-pci,netdev=n1','-display','none','-chardev',f'socket,id=serial0,path={WORK/"console.sock"},server=on,wait=off,logfile={log}','-serial','chardev:serial0','-monitor',f'unix:{WORK/"monitor.sock"},server=on,wait=off']
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
    if not installed:
        wait('serial console',30)
        console.sendall(b'\x1b[B\r')
        # The guided wizard starts on its own; there is no menu step to select.
        wait('Target disk',600)
    else:
        wait('Selection:',420)

def wait(text,timeout=120,after=0):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        content=log.read_text(errors='replace')[after:]
        if text in content:return content
        if text=='Installation completed.' and 'Installation cancelled or failed.' in content:
            raise AssertionError('Installer failed; see protected serial.log')
        if process.poll() is not None:raise AssertionError('QEMU exited: '+(WORK/'stderr.log').read_text())
        time.sleep(1)
    raise AssertionError('Console timeout: '+text)

def shutdown():
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as monitor:
        monitor.connect(str(WORK/'monitor.sock'));monitor.sendall(b'system_powerdown\n')
    process.wait(timeout=120)
    console.close()

def run(profile):
    name=profile['name']
    checks=report['checks']
    TARGET.unlink(missing_ok=True)
    subprocess.run(['qemu-img','create','-f','qcow2',str(TARGET),'16G'],check=True,stdout=subprocess.DEVNULL)
    if profile['uefi']:
        shutil.copyfile('/usr/share/OVMF/OVMF_VARS_4M.fd',WORK/'vars.fd')
    start(profile)
    text=log.read_text(errors='replace')
    assert 'AIOS INSTALLER ISO READY' in text
    checks.append(f'{name}: CD/DVD ISO boots on {profile["label"]} with a blank VirtIO-SCSI target')
    assert 'Selection:' not in text,'the guided wizard must run without a menu step'
    checks.append(f'{name}: guided configuration starts by itself and waits for input, with no menu to find')
    # QEMU user networking exposes gateway/DNS at these fixed test addresses.
    # No erase confirmation: the chosen disk is installed straight after the summary.
    answers=['/dev/sda','it_IT.UTF-8','it','aios-proxmox-test','','static',
             '10.0.2.15/24','10.0.2.2','10.0.2.3','Europe/Rome','ntp','ntp.ubuntu.com',
             '','',PUBKEY,'si']
    console.sendall(('\n'.join(answers)+'\n').encode())
    wait('Installation completed.',2400)
    checks.append(f'{name}: wizard installs hostname, static IPv4, gateway, DNS, timezone/NTP, Italian locale and keyboard')
    # The installer reboots on its own after ten seconds; stop it first.
    # Stop the live system the way a hypervisor does: Proxmox' Shutdown button
    # and virsh shutdown both send the ACPI power button, nothing else.
    shutdown()
    checks.append(f'{name}: live installer environment shuts down on an ACPI power button request')
    # Detach the source entirely and boot the installed target as the only disk.
    start(profile,installed=True)
    text=log.read_text(errors='replace')
    assert 'AIOS READY' in text
    new_secret=re.search(r'Admin bootstrap secret \(valid 24h\): (\S+)',text).group(1)
    assert len(new_secret)>20
    with httpx.Client(base_url='https://127.0.0.1:20443',verify=False,timeout=60) as client:
        client.get('/admin/').raise_for_status()
        assert not client.get('/api/v1/aios/auth/status').json()['initialized']
        assert client.get('/health').json()['database']
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'\n')
    menu=wait('Selection:',60,offset)
    assert 'shell' not in menu.lower(),'the installed console must not offer a shell'
    checks.append(f'{name}: installed console is a closed menu with no shell entry')
    with httpx.Client(base_url='https://127.0.0.1:20443',verify=False,timeout=60) as client:
        assert client.get('/health').json()['database']
    checks.append(f'{name}: installed disk boots without ISO and serves HTTPS on the static address')
    # The optional recovery account exists only to reach the bootstrap secret.
    output=ssh_recovery()
    assert 'Bootstrap secret:' in output,output
    checks.append(f'{name}: SSH recovery account enabled by the wizard returns the bootstrap secret')
    hostile=ssh_recovery('cat /etc/shadow')
    assert 'root:' not in hostile and 'Bootstrap secret:' in hostile,hostile
    checks.append(f'{name}: recovery account ignores any command sent over SSH and gives no shell')
    secret=re.search(r'Bootstrap secret: (\S+)',output).group(1)
    with httpx.Client(base_url='https://127.0.0.1:20443',verify=False,timeout=60) as client:
        created=client.post('/api/v1/aios/auth/bootstrap',json={'username':'admin@example.com',
            'password':'Testing-password-937!','secret':secret})
        assert created.status_code==200,created.text
    checks.append(f'{name}: the bootstrap secret creates the first administrator, with an email as username')
    refused=ssh_recovery()
    assert 'Bootstrap secret:' not in refused and 'An administrator already exists' in refused,refused
    checks.append(f'{name}: once an administrator exists the recovery account stops disclosing the secret')
    # With an administrator in place the console must demand that password.
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'\n')
    wait('Selection:',60,offset)
    console.sendall(b'2\n')
    wait('AIOS user',30,offset)
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'admin@example.com\nwrong-password-here\n')
    wait('Action denied.',60,offset)
    checks.append(f'{name}: console refuses an action when the administrator password is wrong')
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'\n')
    wait('Selection:',60,offset)
    console.sendall(b'2\n')
    wait('AIOS user',30,offset)
    offset=len(log.read_text(errors='replace'))
    console.sendall(b'admin@example.com\nTesting-password-937!\n')
    wait('Authorised: admin@example.com',60,offset)
    checks.append(f'{name}: the administrator password authorises the action and is recorded in the audit trail')
    shutdown()

try:
    for profile in PROFILES:
        started=time.time()
        run(profile)
        report['profiles'][profile['name']]={'status':'PASSED','label':profile['label'],
                                             'machine':profile['machine'],'memory_mib':profile['memory'],
                                             'cpus':profile['cpus'],'seconds':round(time.time()-started)}
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
    (ROOT/'dist/iso-test-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
