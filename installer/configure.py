"""Validated console installation settings; collection never changes a disk."""
import argparse
import ipaddress
import json
import re
import subprocess
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

LOCALES = ('it_IT.UTF-8', 'en_US.UTF-8', 'en_GB.UTF-8', 'de_DE.UTF-8', 'fr_FR.UTF-8', 'es_ES.UTF-8', 'pt_PT.UTF-8')
KEYBOARDS = ('it', 'us', 'gb', 'de', 'fr', 'es', 'pt')


def hostname(value):
    if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', value):
        raise ValueError('hostname must be 1-63 letters, digits and hyphens')
    return value.lower()


def validate(data):
    result = dict(data)
    result['hostname'] = hostname(data['hostname'])
    if data['locale'] not in LOCALES or data['keyboard'] not in KEYBOARDS:
        raise ValueError('Unsupported language or keyboard.')
    try:
        ZoneInfo(data['timezone'])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('Invalid time zone, e.g. Europe/Rome.') from exc
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', data['interface']) or data['interface'] == 'lo':
        raise ValueError('Invalid network interface.')
    if type(data['dhcp']) is not bool or type(data['ntp']) is not bool:
        raise ValueError('DHCP/NTP must be booleans.')
    result['dns'] = [str(ipaddress.ip_address(x)) for x in data.get('dns', [])]
    if len(result['dns']) > 4:
        raise ValueError('Enter at most four DNS servers.')
    if not data['dhcp']:
        address = ipaddress.ip_interface(data['address'])
        gateway = ipaddress.ip_address(data['gateway'])
        if address.version != 4 or gateway.version != 4 or gateway not in address.network:
            raise ValueError('Give an IPv4/CIDR address and a gateway in the same subnet.')
        if not result['dns']:
            raise ValueError('A static network needs at least one DNS server.')
        result['address'], result['gateway'] = str(address), str(gateway)
    else:
        result['address'], result['gateway'] = '', ''
    servers = data.get('ntp_servers', [])
    if len(servers) > 4 or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}', s) for s in servers):
        raise ValueError('Invalid NTP servers.')
    result['ntp_servers'] = servers
    if not data['ntp']:
        value = datetime.strptime(data['manual_time'], '%Y-%m-%d %H:%M:%S')
        result['manual_utc'] = value.replace(tzinfo=ZoneInfo(data['timezone'])).astimezone(timezone.utc).isoformat()
    # The SSH password is never persisted; apply() reads it from the raw input.
    password = result.pop('ssh_password', '')
    result['ssh'] = data.get('ssh', False)
    if type(result['ssh']) is not bool:
        raise ValueError('SSH must be a boolean value.')
    result['ssh_user'] = data.get('ssh_user', '')
    result['ssh_key'] = data.get('ssh_key', '').strip()
    if not result['ssh']:
        result['ssh_user'], result['ssh_key'] = '', ''
    else:
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', result['ssh_user']):
            raise ValueError('Invalid SSH user: lower case, digits, hyphen and underscore.')
        if not password and not result['ssh_key']:
            raise ValueError('For SSH give either a password or a public key.')
        if password and len(password) < 12:
            raise ValueError('The SSH password must be at least 12 characters long.')
        if result['ssh_key'] and not re.fullmatch(
                r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]{32,1000}={0,3}( [\x21-\x7e]{1,200})?',
                result['ssh_key']):
            raise ValueError('SSH public key not recognised.')
    return result


def ask(label, default='', check=None, secret=False):
    """Ask until the answer is valid. A wrong value is explained and asked for
    again on its own: nothing typed before it is lost."""
    while True:
        prompt = f'{label}' + (f' [{default}]' if default and not secret else '') + ': '
        value = (getpass(prompt) if secret else input(prompt)).strip() or default
        if check is None:
            return value
        try:
            checked = check(value)
        except (ValueError, KeyError) as exc:
            print(f'  Invalid value: {exc or "format not recognised"}. Please enter it again.')
            continue
        return value if checked is None else checked


def one_of(choices, message):
    def check(value):
        if value not in choices:
            raise ValueError(message + ' (' + ', '.join(choices) + ')')
        return value
    return check


def yes_no(value):
    answer = value.lower()
    if answer in ('si', 'sì', 's', 'yes', 'y'):
        return True
    if answer in ('no', 'n'):
        return False
    raise ValueError('answer yes or no')


def ipv4_interface(value):
    address = ipaddress.ip_interface(value)
    if address.version != 4 or address.network.prefixlen == 32:
        raise ValueError('give an IPv4 address with prefix, e.g. 192.168.1.50/24')
    return str(address)


def dns_list(required):
    def check(value):
        servers = [str(ipaddress.ip_address(x)) for x in value.split()]
        if len(servers) > 4:
            raise ValueError('at most four DNS servers')
        if required and not servers:
            raise ValueError('a static network needs at least one DNS server')
        return servers
    return check


def timezone_name(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('unknown time zone, e.g. Europe/Rome') from exc
    return value


def ntp_servers(value):
    servers = value.split()
    if not servers or len(servers) > 4 or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.:-]{0,252}', s) for s in servers):
        raise ValueError('one to four server names separated by spaces')
    return servers


def long_password(value):
    if len(value) < 12:
        raise ValueError('the password must be at least 12 characters long')
    return value


def local_time(value):
    datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    return value


def collect(interfaces=None):
    if interfaces is None:
        interfaces = sorted(p.name for p in Path('/sys/class/net').iterdir() if p.name != 'lo')
    while True:
        print('\nAIOS — installation settings (no disk is changed yet)')
        print('The language sets the system locale; the keyboard applies to the installed system.')
        print('Available languages: ' + ', '.join(LOCALES))
        locale = ask('Language / locale', 'it_IT.UTF-8', one_of(LOCALES, 'language not available'))
        print('Available keyboards: ' + ', '.join(KEYBOARDS))
        keyboard = ask('Keyboard', 'it', one_of(KEYBOARDS, 'keyboard not available'))
        name = ask('Hostname', 'aios', hostname)
        print('Detected interfaces: ' + ', '.join(interfaces))
        interface = ask('Interface', interfaces[0] if interfaces else '', one_of(interfaces, 'interface not present'))
        mode = ask('Network: dhcp or static', 'dhcp', lambda v: one_of(('dhcp', 'static'), 'invalid mode')(v.lower()))
        address = gateway = ''
        if mode == 'static':
            address = ask('IPv4/CIDR address (e.g. 192.168.1.50/24)', '', ipv4_interface)
            network = ipaddress.ip_interface(address).network

            def same_network(value):
                gateway = ipaddress.ip_address(value)
                if gateway.version != 4 or gateway not in network:
                    raise ValueError(f'the gateway must be an IPv4 address inside {network}')
                return str(gateway)
            gateway = ask('IPv4 gateway', '', same_network)
        dns = ask('DNS servers separated by spaces (empty = DHCP DNS)' if mode == 'dhcp' else 'DNS servers separated by spaces', '',
                  dns_list(mode == 'static'))
        zone = ask('Time zone', 'Europe/Rome', timezone_name)
        clock_mode = ask('Clock: ntp or manual', 'ntp', lambda v: one_of(('ntp', 'manual'), 'invalid clock mode')(v.lower()))
        ntp = ask('NTP servers separated by spaces', 'ntp.ubuntu.com', ntp_servers) if clock_mode == 'ntp' else []
        manual = ask('Local date and time YYYY-MM-DD HH:MM:SS', '', local_time) if clock_mode == 'manual' else ''
        print('\nSSH access: it only reads or regenerates the code for the first administrator,')
        print("until that account exists. It gives no shell and no other privilege.")
        ssh = ask('Enable recovery SSH? yes/no', 'yes', yes_no)
        ssh_user = ssh_key = ssh_password = ''
        if ssh:
            def user_name(value):
                if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value) or value in ('root', 'aios', 'aios-webui'):
                    raise ValueError('lower case, digits, hyphen and underscore; not a system account')
                return value

            def public_key(value):
                if value and not re.fullmatch(
                        r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]{32,1000}={0,3}( [\x21-\x7e]{1,200})?', value):
                    raise ValueError('SSH public key not recognised')
                return value
            ssh_user = ask('Recovery SSH user', 'aios-recovery', user_name)
            ssh_key = ask('SSH public key (empty to use a password)', '', public_key)
            while not ssh_key:
                ssh_password = ask('SSH password (at least 12 characters)', '', long_password, secret=True)
                if getpass('Repeat the SSH password: ') == ssh_password:
                    break
                print('  The two passwords do not match. Please enter it again.')
        try:
            result = validate(dict(hostname=name, locale=locale, keyboard=keyboard, interface=interface,
                                   dhcp=mode == 'dhcp', address=address, gateway=gateway, dns=dns,
                                   timezone=zone, ntp=clock_mode == 'ntp', ntp_servers=ntp, manual_time=manual,
                                   ssh=ssh, ssh_user=ssh_user, ssh_key=ssh_key, ssh_password=ssh_password))
        except (ValueError, KeyError) as exc:
            # Every field was checked as it was typed; this is only a backstop.
            print('Invalid configuration:', exc); continue
        print('\nConfiguration summary:')
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if ask('Confirm the configuration? yes/no (no = enter the values again)', 'yes', yes_no):
            # Returned apart from the summary and the saved file, never stored.
            return dict(result, ssh_password=ssh_password)


def apply(destination, data):
    """Write target files only. Commands to generate locales run in target chroot."""
    password = data.get('ssh_password', '')
    data = validate(data)
    root = Path(destination)
    def write(name, content, mode=0o644):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # Do not follow symlinks copied from a live filesystem into the host.
        path.unlink(missing_ok=True)
        path.write_text(content)
        path.chmod(mode)
    def link(name, target):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to(target)
    write('etc/hostname', data['hostname'] + '\n')
    write('etc/hosts', f"127.0.0.1 localhost\n127.0.1.1 {data['hostname']}\n::1 localhost ip6-localhost ip6-loopback\n")
    net = {'dhcp4': data['dhcp'], 'dhcp6': data['dhcp'], 'optional': True}
    if data['dhcp'] and data['dns']:
        net['dhcp4-overrides'] = {'use-dns': False}
        net['dhcp6-overrides'] = {'use-dns': False}
    if not data['dhcp']:
        net.update(addresses=[data['address']], routes=[{'to': 'default', 'via': data['gateway']}])
    if data['dns']:
        net['nameservers'] = {'addresses': data['dns']}
    write('etc/netplan/10-aios.yaml', yaml.safe_dump({'network': {'version': 2, 'renderer': 'networkd', 'ethernets': {data['interface']: net}}}), 0o600)
    write('etc/timezone', data['timezone'] + '\n')
    zone = root / 'etc/localtime'
    zone.unlink(missing_ok=True)
    zone.symlink_to('../usr/share/zoneinfo/' + data['timezone'])
    # tmpfiles.d recreates /etc/default/locale and /etc/vconsole.conf as symlinks on
    # every boot, so write the files they point at and lay the links down here too.
    write('etc/locale.conf', f"LANG={data['locale']}\n")
    link('etc/default/locale', '../locale.conf')
    write('etc/locale.gen', f"{data['locale']} UTF-8\n")
    write('etc/default/keyboard', f'XKBMODEL="pc105"\nXKBLAYOUT="{data["keyboard"]}"\nXKBVARIANT=""\nXKBOPTIONS=""\nBACKSPACE="guess"\n')
    link('etc/vconsole.conf', 'default/keyboard')
    write('etc/systemd/timesyncd.conf.d/aios.conf', '[Time]\nNTP=' + ' '.join(data['ntp_servers']) + '\n')
    write('etc/aios/install-settings.json', json.dumps(data, indent=2) + '\n', 0o600)
    subprocess.run(['chroot', str(root), 'locale-gen'], check=True)
    subprocess.run(['chroot', str(root), 'systemctl', 'enable' if data['ntp'] else 'disable', 'systemd-timesyncd.service'], check=True)
    subprocess.run(['chroot', str(root), 'netplan', 'generate'], check=True)
    subprocess.run(['chroot', str(root), 'setupcon', '--save-only'], check=True)
    apply_ssh(root, write, data, password)


def apply_ssh(root, write, data, password):
    """Optional recovery account: no shell, no sudo beyond one bootstrap command."""
    if not data['ssh']:
        write('etc/aios/nftables-ssh.nft', '# SSH disabled: no rule.\n')
        write('etc/ssh/sshd_not_to_be_run', '')
        subprocess.run(['chroot', str(root), 'systemctl', 'disable', 'ssh.service', 'ssh.socket'], check=False)
        return
    # Both units honour this file; removing it is what actually allows sshd to run.
    (root / 'etc/ssh/sshd_not_to_be_run').unlink(missing_ok=True)
    # nftables drops everything but HTTP/HTTPS by default, so open 22 as well.
    write('etc/aios/nftables-ssh.nft', '  tcp dport 22 accept\n')
    # sshd refuses to start without host keys, and unit ordering must not decide
    # whether it works, so make this machine's keys now and regenerate any that
    # go missing later. They are created here, never copied out of the image.
    write('etc/systemd/system/ssh.service.d/aios.conf',
          '[Service]\nExecStartPre=/usr/bin/ssh-keygen -A\n')
    user = data['ssh_user']
    write('etc/ssh/sshd_config.d/aios.conf', '\n'.join([
        'PermitRootLogin no',
        'AllowUsers ' + user,
        'PubkeyAuthentication yes',
        # A key alone is stronger, so passwords stay off unless one was chosen.
        'PasswordAuthentication ' + ('yes' if password else 'no'),
        'KbdInteractiveAuthentication no',
        'PermitEmptyPasswords no',
        'MaxAuthTries 3',
        'X11Forwarding no',
        'AllowTcpForwarding no',
        'AllowAgentForwarding no',
        'PermitTunnel no',
        'ClientAliveInterval 300',
    ]) + '\n')
    write('etc/sudoers.d/aios-recovery',
          f'{user} ALL=(root) NOPASSWD: /usr/local/sbin/aios-bootstrap-recovery show, '
          f'/usr/local/sbin/aios-bootstrap-recovery reset, '
          f'/usr/local/sbin/aios-bootstrap-recovery apikey\n', 0o440)
    subprocess.run(['chroot', str(root), 'useradd', '--create-home', '--shell',
                    '/usr/local/bin/aios-recovery-shell', user], check=True)
    if password:
        subprocess.run(['chroot', str(root), 'chpasswd'], input=f'{user}:{password}', text=True, check=True)
    else:
        subprocess.run(['chroot', str(root), 'passwd', '-l', user], check=True)
    if data['ssh_key']:
        write(f'home/{user}/.ssh/authorized_keys', data['ssh_key'] + '\n', 0o600)
        subprocess.run(['chroot', str(root), 'chown', '-R', f'{user}:{user}', f'/home/{user}/.ssh'], check=True)
    subprocess.run(['chroot', str(root), 'ssh-keygen', '-A'], check=True)
    # sshd is socket activated on this release: the socket has to be enabled too.
    subprocess.run(['chroot', str(root), 'systemctl', 'enable', 'ssh.socket'], check=True)
    subprocess.run(['chroot', str(root), 'systemctl', 'enable', 'ssh.service'], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collect', type=Path)
    parser.add_argument('--apply', type=Path)
    parser.add_argument('--settings', type=Path)
    args = parser.parse_args()
    if args.collect:
        args.collect.write_text(json.dumps(collect()))
        args.collect.chmod(0o600)
    elif args.apply and args.settings:
        apply(args.apply, json.loads(args.settings.read_text()))
    else:
        parser.error('Use --collect FILE or --apply ROOT --settings FILE')


if __name__ == '__main__':
    main()
