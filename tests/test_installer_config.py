import json
import importlib.util
from pathlib import Path
import pytest
import yaml

spec = importlib.util.spec_from_file_location('installer_config', Path(__file__).parents[1]/'installer/configure.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)

@pytest.fixture
def settings():
    return dict(hostname='aios-lab', locale='it_IT.UTF-8', keyboard='it', interface='ens18', dhcp=False,
                address='192.168.50.20/24', gateway='192.168.50.1', dns=['192.168.50.1','1.1.1.1'],
                timezone='Europe/Rome', ntp=True, ntp_servers=['ntp.ubuntu.com'], manual_time='')

KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ9k3mKQdpWJ0Fv8dYrVqTt6uLmXcE2Rb1oPzN4hSgYw lab@example'

@pytest.mark.parametrize('key,value', [('hostname','bad;reboot'),('interface','../ens18'),('gateway','10.0.0.1'),
    ('address','bad'),('dns',['bad']),('timezone','../../etc/passwd'),('locale','xx_XX'),('keyboard','it;reboot'),
    ('ntp_servers',['pool.ntp.org\nBad=1'])])
def test_reject_bad_settings(settings,key,value):
    with pytest.raises(ValueError):
        config.validate({**settings,key:value})


def test_static_target_files(settings,tmp_path,monkeypatch):
    commands=[]
    monkeypatch.setattr(config.subprocess,'run',lambda args,**kw:commands.append(args))
    config.apply(tmp_path,settings)
    network=yaml.safe_load((tmp_path/'etc/netplan/10-aios.yaml').read_text())['network']['ethernets']['ens18']
    assert network['addresses']==['192.168.50.20/24']
    assert network['routes']==[{'to':'default','via':'192.168.50.1'}]
    assert network['nameservers']['addresses']==settings['dns']
    assert (tmp_path/'etc/hostname').read_text()=='aios-lab\n'
    assert (tmp_path/'etc/localtime').readlink()==Path('../usr/share/zoneinfo/Europe/Rome')
    assert 'XKBLAYOUT="it"' in (tmp_path/'etc/default/keyboard').read_text()
    # tmpfiles.d relinks these two paths on every boot, so the settings have to
    # live in the files the links point at or the first boot discards them.
    assert (tmp_path/'etc/locale.conf').read_text()=='LANG=it_IT.UTF-8\n'
    assert (tmp_path/'etc/default/locale').readlink()==Path('../locale.conf')
    assert 'LANG=it_IT.UTF-8' in (tmp_path/'etc/default/locale').read_text()
    assert (tmp_path/'etc/vconsole.conf').readlink()==Path('default/keyboard')
    assert 'XKBLAYOUT="it"' in (tmp_path/'etc/vconsole.conf').read_text()
    assert (tmp_path/'etc/netplan/10-aios.yaml').stat().st_mode & 0o777 == 0o600
    assert any(args[-2:]==['netplan','generate'] for args in commands)


def test_dhcp_dns_override_and_manual_clock(settings,tmp_path,monkeypatch):
    monkeypatch.setattr(config.subprocess,'run',lambda *args,**kw:None)
    data={**settings,'dhcp':True,'ntp':False,'manual_time':'2026-09-11 12:00:00'}
    assert config.validate(data)['manual_utc']=='2026-09-11T10:00:00+00:00'
    config.apply(tmp_path,data)
    network=yaml.safe_load((tmp_path/'etc/netplan/10-aios.yaml').read_text())['network']['ethernets']['ens18']
    assert network['dhcp4'] and network['dhcp4-overrides']=={'use-dns':False}
    assert 'routes' not in network


def test_ssh_disabled_by_default(settings,tmp_path,monkeypatch):
    commands=[]
    monkeypatch.setattr(config.subprocess,'run',lambda args,**kw:commands.append(args))
    config.apply(tmp_path,settings)
    assert json.loads((tmp_path/'etc/aios/install-settings.json').read_text())['ssh'] is False
    assert not (tmp_path/'etc/ssh/sshd_config.d/aios.conf').exists()
    assert not (tmp_path/'etc/sudoers.d/aios-recovery').exists()
    assert any(args[-4:]==['systemctl','disable','ssh.service','ssh.socket'] for args in commands)
    # Socket activation would otherwise keep port 22 listening anyway.
    assert (tmp_path/'etc/ssh/sshd_not_to_be_run').exists()
    assert 'dport 22' not in (tmp_path/'etc/aios/nftables-ssh.nft').read_text()


def test_ssh_key_account_is_restricted(settings,tmp_path,monkeypatch):
    commands=[]
    monkeypatch.setattr(config.subprocess,'run',lambda args,**kw:commands.append(args))
    config.apply(tmp_path,{**settings,'ssh':True,'ssh_user':'aios-recovery','ssh_key':KEY})
    sshd=(tmp_path/'etc/ssh/sshd_config.d/aios.conf').read_text()
    assert 'PermitRootLogin no' in sshd and 'AllowUsers aios-recovery' in sshd
    # A key was given, so password logins stay closed.
    assert 'PasswordAuthentication no' in sshd
    rule=(tmp_path/'etc/sudoers.d/aios-recovery').read_text()
    assert rule.startswith('aios-recovery ALL=(root) NOPASSWD: /usr/local/sbin/aios-bootstrap-recovery show,')
    assert 'ALL=(ALL)' not in rule and 'NOPASSWD: ALL' not in rule
    assert (tmp_path/'etc/sudoers.d/aios-recovery').stat().st_mode & 0o777 == 0o440
    assert (tmp_path/'home/aios-recovery/.ssh/authorized_keys').read_text()==KEY+'\n'
    assert (tmp_path/'home/aios-recovery/.ssh/authorized_keys').stat().st_mode & 0o777 == 0o600
    assert ['chroot',str(tmp_path),'useradd','--create-home','--shell','/usr/local/bin/aios-recovery-shell','aios-recovery'] in commands
    # Without a password the account must not be able to authenticate with one.
    assert ['chroot',str(tmp_path),'passwd','-l','aios-recovery'] in commands
    assert any(args[-3:]==['systemctl','enable','ssh.service'] for args in commands)
    assert any(args[-3:]==['systemctl','enable','ssh.socket'] for args in commands)
    assert not (tmp_path/'etc/ssh/sshd_not_to_be_run').exists()
    # Without host keys sshd never starts, so they cannot depend on unit ordering.
    assert ['chroot',str(tmp_path),'ssh-keygen','-A'] in commands
    assert 'ExecStartPre=/usr/bin/ssh-keygen -A' in (tmp_path/'etc/systemd/system/ssh.service.d/aios.conf').read_text()
    # Without this the firewall would silently drop every SSH connection.
    assert (tmp_path/'etc/aios/nftables-ssh.nft').read_text().strip()=='tcp dport 22 accept'


def test_ssh_password_is_never_persisted(settings,tmp_path,monkeypatch):
    monkeypatch.setattr(config.subprocess,'run',lambda *a,**kw:None)
    config.apply(tmp_path,{**settings,'ssh':True,'ssh_user':'aios-recovery','ssh_password':'a-long-password-1'})
    saved=(tmp_path/'etc/aios/install-settings.json').read_text()
    assert 'a-long-password-1' not in saved and 'ssh_password' not in saved
    assert 'PasswordAuthentication yes' in (tmp_path/'etc/ssh/sshd_config.d/aios.conf').read_text()


@pytest.mark.parametrize('data',[
    {'ssh':True,'ssh_user':'aios-recovery'},
    {'ssh':True,'ssh_user':'root;reboot','ssh_key':KEY},
    {'ssh':True,'ssh_user':'Recovery','ssh_key':KEY},
    {'ssh':True,'ssh_user':'aios-recovery','ssh_password':'short'},
    {'ssh':True,'ssh_user':'aios-recovery','ssh_key':'not-a-key'},
    {'ssh':True,'ssh_user':'aios-recovery','ssh_key':'ssh-ed25519 AAAA$(reboot)'},
    {'ssh':'yes','ssh_user':'aios-recovery','ssh_key':KEY}])
def test_reject_bad_ssh_settings(settings,data):
    with pytest.raises(ValueError):
        config.validate({**settings,**data})


def scripted(monkeypatch, answers, secrets=()):
    """Feed the wizard, recording every prompt it shows."""
    answers, secrets, prompts, printed = list(answers), list(secrets), [], []
    def reply(queue):
        def read(prompt=''):
            prompts.append(prompt)
            assert queue, 'wizard asked more than scripted: ' + prompt
            return queue.pop(0)
        return read
    monkeypatch.setattr('builtins.input', reply(answers))
    monkeypatch.setattr(config, 'getpass', reply(secrets))
    monkeypatch.setattr('builtins.print', lambda *a, **k: printed.append(' '.join(map(str, a))))
    return prompts, printed, answers


def test_a_wrong_value_is_asked_again_without_restarting(monkeypatch):
    prompts, printed, left = scripted(monkeypatch, [
        '', '', 'bad;host', 'aios-lab',          # locale, keyboard, hostname wrong then right
        'eth9', 'ens18',                         # interface not present, then present
        'fixed', 'static',                       # network mode wrong, then right
        '192.168.1.50', '192.168.1.50/24',       # address without prefix, then with
        '10.0.0.1', '192.168.1.1',               # gateway outside the network, then inside
        '', '1.1.1.1',                           # static without DNS, then one
        'Europe/Nowhere', 'Europe/Rome',         # unknown zone, then known
        'manual', '2026-13-40 25:00:00', '2026-09-15 10:00:00',
        'maybe', 'no',                           # SSH answer wrong, then right
        'yes'])
    result = config.collect(['ens18'])
    assert left == []
    assert result['hostname'] == 'aios-lab' and result['address'] == '192.168.1.50/24' and result['gateway'] == '192.168.1.1'
    assert result['dns'] == ['1.1.1.1'] and result['timezone'] == 'Europe/Rome' and not result['ntp']
    # The wizard never went back to the first question.
    assert sum('Language' in p for p in prompts) == 1
    assert sum('Invalid value' in line for line in printed) == 9


def test_mismatched_ssh_password_is_asked_again(monkeypatch):
    _, printed, left = scripted(monkeypatch,
        ['', '', 'aios', 'ens18', 'dhcp', '', 'Europe/Rome', 'ntp', '', 'yes', 'root', 'recovery', '', 'yes'],
        ['short', 'Long-enough-password-1', 'different-password-12', 'Long-enough-password-1', 'Long-enough-password-1'])
    result = config.collect(['ens18'])
    assert left == [] and result['ssh_user'] == 'recovery' and result['ssh_password'] == 'Long-enough-password-1'
    assert any('do not match' in line for line in printed)
