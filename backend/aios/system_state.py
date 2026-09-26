"""What the operating system is actually configured with, read without privileges.

The portal used to show the values stored the last time someone pressed Apply,
and a hard-coded "UTC" before that: an installation made in Europe/Rome showed
UTC, and applying the form moved the clock to UTC without anyone asking.
"""
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

import psutil

ZONEINFO = Path('/usr/share/zoneinfo')
TIMESYNCD = [Path('/etc/systemd/timesyncd.conf'), *sorted(Path('/etc/systemd/timesyncd.conf.d').glob('*.conf'))]
RESOLVED = Path('/run/systemd/resolve/resolv.conf')
LEASES = Path('/run/systemd/netif/leases')


def timezone_name():
    """The zone /etc/localtime points to, as the installer and timedatectl set it."""
    try:
        target = os.path.realpath('/etc/localtime')
    except OSError:
        return 'UTC'
    marker = '/zoneinfo/'
    return target.split(marker, 1)[1] if marker in target else 'UTC'


def ntp_servers():
    servers = []
    for path in TIMESYNCD:
        try:
            for line in path.read_text().splitlines():
                if line.strip().startswith('NTP='):
                    servers = line.split('=', 1)[1].split()
        except OSError:
            continue
    return servers or ['ntp.ubuntu.com']


def governor():
    try:
        return Path('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor').read_text().strip()
    except OSError:
        return 'unchanged'


def hugepages():
    try:
        return int(Path('/proc/sys/vm/nr_hugepages').read_text())
    except (OSError, ValueError):
        return 0


def observed_system(stored):
    """Live values where the system can tell them; stored ones for the rest."""
    current = governor()
    return {**stored, 'hostname': socket.gethostname(), 'timezone': timezone_name(), 'ntp': ntp_servers(),
            'governor': current if current in ('performance', 'powersave') else 'unchanged', 'hugepages_2m': hugepages()}


def timezones():
    """Every zone the system knows, for the portal's picker and for validation."""
    zones = []
    for root, _, files in os.walk(ZONEINFO):
        for name in files:
            relative = os.path.relpath(os.path.join(root, name), ZONEINFO)
            if re.fullmatch(r'[A-Z][A-Za-z_+-]*(/[A-Za-z0-9_+-]+)*', relative) and not relative.startswith(('posix', 'right', 'Etc/')):
                zones.append(relative)
    return sorted(set(zones + ['UTC']))


def default_route():
    """Interface and gateway of the IPv4 default route, from the kernel table."""
    try:
        for line in Path('/proc/net/route').read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == '00000000':
                gateway = socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
                return fields[0], gateway
    except (OSError, ValueError):
        pass
    return None, None


def dns_servers():
    try:
        return [line.split()[1] for line in RESOLVED.read_text().splitlines() if line.startswith('nameserver ')]
    except OSError:
        return []


def network():
    """The connection the appliance is reachable on, as it is now."""
    interface, gateway = default_route()
    addresses = []
    if interface:
        for address in psutil.net_if_addrs().get(interface, []):
            if address.family == socket.AF_INET and address.netmask:
                prefix = sum(bin(int(part)).count('1') for part in address.netmask.split('.'))
                addresses.append(f'{address.address}/{prefix}')
    dhcp = False
    try:
        index = socket.if_nametoindex(interface) if interface else None
        dhcp = bool(index and (LEASES / str(index)).exists())
    except OSError:
        pass
    return {'interface': interface, 'addresses': addresses, 'gateway': gateway, 'dns': dns_servers(), 'dhcp': dhcp,
            'interfaces': sorted(name for name in psutil.net_if_addrs() if name != 'lo')}


def pending_network(jobs):
    """A network change applied less than two minutes ago and not yet confirmed.
    The operator no longer copies an identifier: the page offers the change."""
    now = datetime.now(timezone.utc).timestamp()
    for job in jobs:
        if job['action'] == 'network-confirm':
            return None
        if job['action'] == 'network' and job['state'] == 'COMPLETED' and now - job['created_at'] < 120:
            return {'id': job['id'], 'expires': job['created_at'] + 120}
    return None


def certificate(path='/etc/nginx/aios.crt'):
    """The certificate the portal is served with: who it names, who signed it and
    when it expires, so a replacement can be planned instead of discovered."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    try:
        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        alt = [str(v) for v in names.get_values_for_type(x509.DNSName)] + [str(v) for v in names.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        alt = []
    expires = cert.not_valid_after_utc
    return {'subject': cert.subject.rfc4514_string(), 'issuer': cert.issuer.rfc4514_string(), 'self_signed': cert.subject == cert.issuer,
            'not_before': cert.not_valid_before_utc.timestamp(), 'not_after': expires.timestamp(),
            'days_left': (expires - datetime.now(timezone.utc)).days, 'names': alt, 'sha256': cert.fingerprint(hashes.SHA256()).hex(':').upper()}
