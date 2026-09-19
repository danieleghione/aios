#!/usr/bin/env python3
from pathlib import Path
base = Path(__file__).resolve().parents[1]
common = '''[Unit]
Description=AIOS {description}
After=aios-firstboot.service network.target
Requires=aios-firstboot.service
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=aios
Group=aios
SupplementaryGroups=systemd-journal
WorkingDirectory=/opt/aios/app
Environment=PYTHONPATH=/opt/aios/app/backend
Environment=PYTHONUNBUFFERED=1
Environment=LD_LIBRARY_PATH=/opt/aios/runtime/lib
EnvironmentFile=-/etc/aios/proxy.env
ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/lib/aios /etc/aios /var/log/aios
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
CapabilityBoundingSet=
LimitMEMLOCK=infinity
UMask=0027

[Install]
WantedBy=multi-user.target
'''
commands = {
'control-plane': ('control plane', '/opt/aios/venv/bin/uvicorn aios.app:app --host 127.0.0.1 --port 8081 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log'),
'runtime-manager': ('runtime manager', '/opt/aios/venv/bin/python -m aios runtime'),
'download-worker': ('download worker', '/opt/aios/venv/bin/python -m aios downloads'),
'hardware-profiler': ('hardware profiler', '/opt/aios/venv/bin/python -m aios hardware'),
'repository-sync': ('repository sync', '/opt/aios/venv/bin/python -m aios sync')}
for name, (description, command) in commands.items():
    text = common.format(description=description, command=command)
    if name in ('hardware-profiler', 'repository-sync'):
        text = text.replace('Type=simple', 'Type=oneshot').replace('Restart=on-failure', 'Restart=no')
    (base/'systemd'/f'aios-{name}.service').write_text(text)
