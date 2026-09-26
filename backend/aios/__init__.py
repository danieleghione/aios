"""AIOS control plane."""
from pathlib import Path


def _version():
    """The release this code belongs to, from the VERSION file at the root of the
    application: one number for the portal, the API and the build."""
    for root in (Path(__file__).resolve().parents[2], Path('/opt/aios/app')):
        try:
            return (root / 'VERSION').read_text().strip()
        except OSError:
            continue
    return 'unknown'


__version__ = _version()
