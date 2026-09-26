"""What every route module shares: logging, metrics and the role checks.

The control plane used to be one 1200-line module; it is split by subject now,
and these few objects are the only state the parts have in common.
"""
import asyncio
import logging
from collections import deque

import httpx
import psutil
from prometheus_client import Counter, Gauge, Histogram

from . import accelerators, alerts, auth, notify

logging.basicConfig(level=logging.INFO, format='%(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
LOG = logging.getLogger('aios')

REQUESTS = Counter('aios_inference_requests', 'Inference requests', ['endpoint', 'status'])
ACTIVE = Gauge('aios_inference_active', 'Active inference requests')
LATENCY = Histogram('aios_inference_seconds', 'Inference request latency')
TOKENS = Counter('aios_inference_tokens', 'Tokens reported by runtime', ['direction'])
SYSTEM = Gauge('aios_system', 'Hardware measurements', ['measurement'])
ERRORS = Counter('aios_errors', 'Control plane errors', ['status'])

viewer, operator, admin, superadmin = [auth.require(role) for role in ('VIEWER', 'OPERATOR', 'ADMIN', 'SUPERADMIN')]

# The dashboard charts start with the last five minutes instead of an empty
# frame that fills one point every five seconds while the page is open.
CPU_HISTORY: deque = deque(maxlen=60)
HISTORY: dict[str, deque] = {'cpu': CPU_HISTORY, 'memory': deque(maxlen=60), 'gpu_memory': deque(maxlen=60),
                             'temperature': deque(maxlen=60), 'tokens_per_second': deque(maxlen=60)}


def hottest():
    """The highest temperature any sensor reports, or None where there is none."""
    try:
        readings = [r.current for group in psutil.sensors_temperatures().values() for r in group if r.current]
    except (AttributeError, OSError):
        return None
    return round(max(readings), 1) if readings else None


async def generation_speed():
    """Tokens per second of the loaded language model, from its own metrics."""
    try:
        async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
            response = await client.get('http://127.0.0.1:8090/metrics')
        for line in response.text.splitlines():
            if not line.startswith('#') and 'predicted_tokens_seconds ' in line:
                value = float(line.split()[-1])
                return round(value, 1) if value == value else None
    except (httpx.HTTPError, ValueError):
        return None
    return None


async def sample_cpu():
    psutil.cpu_percent(interval=None)
    ticks = 0
    while True:
        await asyncio.sleep(5)
        CPU_HISTORY.append(round(psutil.cpu_percent(interval=None), 1))
        HISTORY['memory'].append(round(psutil.virtual_memory().percent, 1))
        HISTORY['gpu_memory'].append(accelerators.recorded_vram())
        HISTORY['temperature'].append(hottest())
        HISTORY['tokens_per_second'].append(await generation_speed())
        ticks += 1
        if ticks % 12 == 1:
            # Once a minute: conditions nobody triggers by an action.
            try:
                alerts.check_disk()
                alerts.check_updates()
                await asyncio.to_thread(notify.deliver_pending)
            except Exception as exc:  # a check must never stop the sampler
                LOG.warning('alert checks failed: %s', exc)
