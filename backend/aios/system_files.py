"""System files an application release carries: systemd units, the NGINX site,
the firewall rules and the chat's stylesheet.

The image installs them once; a signed application release replaces only
/opt/aios/app. Without this, an appliance updated in place would run the new
code with the old units and configuration: no embedding service, the old upload
limit. At every boot and after every application release, each file that
differs is installed, checked by the tool that reads it, and put back as it was
if the check fails. Runs as root (firstboot and the platform broker).
"""
import filecmp
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_SUFFIXES = ('.service', '.socket', '.timer')


def default_run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)


def targets(app, root='/'):
    """(source, destination, check, apply) for every file the release carries."""
    app, root = Path(app), Path(root)
    found = []
    for unit in sorted((app / 'systemd').glob('*')):
        if unit.suffix in UNIT_SUFFIXES:
            found.append((unit, root / 'etc/systemd/system' / unit.name, None, None))
    found.append((app / 'config/nginx.conf', root / 'etc/nginx/sites-available/aios', ['nginx', '-t'],
                  ['systemctl', 'try-reload-or-restart', 'nginx']))
    found.append((app / 'config/nftables.conf', root / 'etc/nftables.conf', ['nft', '-c', '-f', '{path}'],
                  ['nft', '-f', '{path}']))
    for css in glob.glob(str(root / 'opt/aios/webui/lib/python3*/site-packages/open_webui/static/custom.css')):
        found.append((app / 'config/webui-custom.css', Path(css), None, None))
    return [entry for entry in found if entry[0].is_file()]


def enabled_units(app):
    try:
        lines = (Path(app) / 'config/enabled-units').read_text().splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith('#')]


def sync(app='/opt/aios/app', root='/', run=default_run):
    """Install what changed. Returns the destinations written; raises ValueError,
    with everything put back, when a file does not pass its check."""
    changed, units = [], False
    for source, destination, check, apply in targets(app, root):
        if destination.exists() and filecmp.cmp(source, destination, shallow=False):
            continue
        backup = destination.with_name(destination.name + '.aios-previous')
        existed = destination.exists()
        if existed:
            shutil.copy2(destination, backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o644)
        try:
            if check:
                run([part.replace('{path}', str(destination)) for part in check])
        except (subprocess.SubprocessError, OSError) as exc:
            if existed:
                shutil.copy2(backup, destination)
            else:
                destination.unlink()
            detail = getattr(exc, 'stderr', '') or str(exc)
            raise ValueError(f'{destination.name} from this release does not pass its check, kept the previous one: {detail.strip()[:300]}') from None
        finally:
            backup.unlink(missing_ok=True)
        if apply:
            run([part.replace('{path}', str(destination)) for part in apply])
        units = units or destination.suffix in UNIT_SUFFIXES
        changed.append(str(destination))
    if units:
        run(['systemctl', 'daemon-reload'])
    wanted = enabled_units(app)
    if wanted:
        newly = [unit for unit in wanted if _disabled(unit, run)]
        if newly:
            run(['systemctl', 'enable', *newly])
            # --no-block: at boot this runs inside the boot transaction.
            run(['systemctl', 'start', '--no-block', *newly])
            changed += [f'enabled {unit}' for unit in newly]
    return changed


def _disabled(unit, run):
    try:
        run(['systemctl', 'is-enabled', '--quiet', unit])
        return False
    except subprocess.CalledProcessError:
        return True
    except (subprocess.SubprocessError, OSError):
        return False


if __name__ == '__main__':
    try:
        for item in sync(sys.argv[1] if len(sys.argv) > 1 else '/opt/aios/app'):
            print('System file updated:', item)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
