"""Install AIOS from an ISO onto a fresh lab disk through the guided installer on
the serial console, answering it like an operator would (including one invalid
hostname, to see it asked again), with screenshots of the boot menu and wizard.

    python3 install.py dist/aios-installer-<version>-x86_64.iso
"""
import os, shutil, socket, subprocess, sys, threading, time
from pathlib import Path
LAB = Path(os.environ.get('AIOS_LAB_DIR', Path(__file__).resolve().parents[2] / 'build' / 'lab'))
LAB.mkdir(parents=True, exist_ok=True)
ISO = Path(sys.argv[1]).resolve()
SHOTS = LAB / 'shots'; SHOTS.mkdir(exist_ok=True)
DISK, VARS, LOG = LAB / 'aios.qcow2', LAB / 'vars.fd', LAB / 'install.log'
# The recovery SSH account is created with a key made for the lab.
if not (LAB / 'lab-key.pub').exists():
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'aios-lab', '-f', str(LAB / 'lab-key')], check=True)
KEY = (LAB / 'lab-key.pub').read_text().strip()
DISK.unlink(missing_ok=True)
subprocess.run(['qemu-img', 'create', '-f', 'qcow2', str(DISK), '20G'], check=True, stdout=subprocess.DEVNULL)
shutil.copyfile('/usr/share/OVMF/OVMF_VARS_4M.fd', VARS)
LOG.write_text('')
for name in ('c.sock', 'm.sock'):
    (LAB / name).unlink(missing_ok=True)
cmd = ['qemu-system-x86_64', '-enable-kvm', '-cpu', 'host', '-machine', 'q35', '-m', '3072', '-smp', '2',
       '-drive', 'if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd',
       '-drive', f'if=pflash,format=raw,file={VARS}',
       '-device', 'virtio-scsi-pci,id=scsi0', '-drive', f'file={DISK},format=qcow2,if=none,id=d',
       '-device', 'scsi-hd,drive=d,bus=scsi0.0', '-drive', f'file={ISO},media=cdrom,if=none,id=cd,readonly=on',
       '-device', 'ide-cd,drive=cd,bootindex=0', '-no-reboot',
       '-netdev', 'user,id=n1', '-device', 'virtio-net-pci,netdev=n1',
       '-display', 'none', '-chardev', f'socket,id=s0,path={LAB/"c.sock"},server=on,wait=off,logfile={LOG}',
       '-serial', 'chardev:s0', '-monitor', f'unix:{LAB/"m.sock"},server=on,wait=off']
p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=(LAB / 'err.log').open('w'))
def connect(name):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(200):
        try: s.connect(str(LAB / name)); return s
        except (FileNotFoundError, ConnectionRefusedError): time.sleep(.1)
    sys.exit(f'QEMU did not start: {(LAB / "err.log").read_text().strip()}')
con = connect('c.sock'); mon = connect('m.sock')
threading.Thread(target=lambda: [None for _ in iter(lambda: con.recv(65536), b'')], daemon=True).start()
threading.Thread(target=lambda: [None for _ in iter(lambda: mon.recv(65536), b'')], daemon=True).start()
def shot(name): mon.sendall(f'screendump {SHOTS / name}.ppm\n'.encode())
def wait(text, timeout=2400, after=0, fatal=True):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        content = LOG.read_text(errors='replace')[after:]
        if text in content: return content
        if p.poll() is not None: return None
        time.sleep(1)
    if fatal: sys.exit('timeout waiting for: ' + text)
    return None
prev = 0
if len(sys.argv) > 2 and sys.argv[2] == 'graphical':
    # Default entry on the screen: splash, then the wizard on tty1. Pictures only.
    for delay in (4, 10, 16, 24, 35, 50, 70):
        time.sleep(delay - prev); prev = delay; shot(f'g-{delay:03d}')
    mon.sendall(b'quit\n'); p.wait(timeout=30); sys.exit(0)
time.sleep(4); shot('1-iso-grub')
wait('serial console', 60); time.sleep(1); shot('2-iso-grub-menu')
# GRUB sometimes misses the first keystroke on the serial line and sits on the
# menu until the timeout; send the selection again rather than failing the run.
for attempt in range(6):
    con.sendall(b'\x1b[B\r')
    if wait('Target disk', 120 if attempt < 5 else 600, fatal=False) is not None:
        break
    print('GRUB did not take the selection, retrying', attempt + 1, flush=True)
else:
    sys.exit('timeout waiting for: Target disk')
shot('4-iso-wizard')
t0 = time.monotonic()
# disk, locale, keyboard, hostname (wrong then right), interface, mode, dns, zone, clock, ntp, ssh (default yes), user, key, confirm
answers = ['', '', '', 'bad;name', 'aios-lab', '', 'dhcp', '', '', 'ntp', '', '', '', KEY, 'yes']
for a in answers:
    con.sendall((a + '\n').encode()); time.sleep(1.5)
print('invalid hostname asked again:', 'Invalid value' in LOG.read_text(errors='replace'))
print('erase confirmation asked:', 'ERASE' in LOG.read_text(errors='replace'))
done = wait('Installation completed', 3600)
print('installation completed in', round(time.monotonic() - t0), 's including the wizard')
# -no-reboot makes the automatic reboot end QEMU.
p.wait(timeout=120)
print('automatic reboot: QEMU exited with code', p.returncode)
