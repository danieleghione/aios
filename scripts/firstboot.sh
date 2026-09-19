#!/usr/bin/env bash
set -Eeuo pipefail
mkdir -p /etc/aios/secrets /var/lib/aios /var/log/aios
chmod 700 /etc/aios/secrets
# Expand only the data partition on the appliance's own root disk.
DATADEV=$(findmnt -n -o SOURCE /var/lib/aios)
PARENT=$(lsblk -ndo PKNAME "$DATADEV")
PARTNUM=$(cat "/sys/class/block/$(basename "$DATADEV")/partition")
if [[ -n "$PARENT" && "$PARTNUM" == 3 ]]; then
  growpart "/dev/$PARENT" 3 || [[ $? == 1 ]]
  resize2fs "$DATADEV"
fi
chown -R aios:aios /etc/aios /var/lib/aios /var/log/aios
runuser -u aios -- /opt/aios/venv/bin/python -m aios init
if [[ ! -f /etc/nginx/aios.key ]]; then
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 825 -subj "/CN=$(hostname)" -addext "subjectAltName=DNS:$(hostname),DNS:localhost,IP:127.0.0.1" -keyout /etc/nginx/aios.key -out /etc/nginx/aios.crt
  chmod 600 /etc/nginx/aios.key
fi
if [[ ! -f /etc/aios/secrets/inference-key ]]; then
  openssl rand -hex 32 > /etc/aios/secrets/inference-key
  chmod 600 /etc/aios/secrets/inference-key
  chown aios:aios /etc/aios/secrets/inference-key
fi
if [[ ! -f /etc/aios/webui.env ]]; then
  WEBUI_KEY=$(openssl rand -hex 32)
  INFERENCE_KEY=$(cat /etc/aios/secrets/inference-key)
  cat > /etc/aios/webui.env <<ENV
DATA_DIR=/var/lib/aios/webui
WEBUI_SECRET_KEY=$WEBUI_KEY
OPENAI_API_BASE_URL=http://127.0.0.1:8081/v1
OPENAI_API_BASE_URLS=http://127.0.0.1:8081/v1
OPENAI_API_KEY=$INFERENCE_KEY
OPENAI_API_KEYS=$INFERENCE_KEY
ENABLE_OLLAMA_API=false
ENABLE_OPENAI_API=true
ENABLE_SIGNUP=false
ENABLE_PERSISTENT_CONFIG=true
OFFLINE_MODE=true
HF_HUB_OFFLINE=1
RAG_EMBEDDING_MODEL_TRUST_REMOTE_CODE=false
RAG_RERANKING_MODEL_TRUST_REMOTE_CODE=false
ENABLE_CODE_EXECUTION=false
ENABLE_CODE_INTERPRETER=false
RAG_EMBEDDING_MODEL_AUTO_UPDATE=false
RAG_RERANKING_MODEL_AUTO_UPDATE=false
WHISPER_MODEL_AUTO_UPDATE=false
ANONYMIZED_TELEMETRY=false
DO_NOT_TRACK=true
SCARF_NO_ANALYTICS=true
ENABLE_VERSION_UPDATE_CHECK=false
WEBUI_AUTH=true
ENV
  chmod 600 /etc/aios/webui.env
fi
# Also reconcile restored installations: AIOS is the login authority.
sed -i -E '/^(WEBUI_ADMIN_(EMAIL|PASSWORD|NAME)|WEBUI_AUTH_TRUSTED_(EMAIL|NAME|ROLE)_HEADER|WEBUI_AUTH_SIGNOUT_REDIRECT_URL|ENABLE_PASSWORD_AUTH|ENABLE_(TITLE|TAGS|FOLLOW_UP|AUTOCOMPLETE|SEARCH_QUERY|RETRIEVAL_QUERY)_GENERATION|TASK_MODEL_PARAMS|DEFAULT_MODEL_METADATA|ENABLE_IMAGE_GENERATION|IMAGE_GENERATION_ENGINE|IMAGE_GENERATION_MODEL|IMAGES_OPENAI_API_(BASE_URL|KEY)|IMAGE_SIZE|IMAGE_STEPS)=/d' /etc/aios/webui.env
cat >> /etc/aios/webui.env <<'ENV'
WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-AIOS-Email
WEBUI_AUTH_TRUSTED_NAME_HEADER=X-AIOS-Name
WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-AIOS-Role
WEBUI_AUTH_SIGNOUT_REDIRECT_URL=/admin/?signout=chat
ENABLE_PASSWORD_AUTH=true
ENV
# Every automatic chat task runs on the same local CPU model as the answer, and
# a reasoning model spends up to a thousand tokens on each: tags, follow-up
# suggestions and query rewriting kept the CPU at 100% for minutes around every
# message. Titles stay, generated briefly with reasoning switched off. The
# built-in tools add about 6000 tokens of schemas to every request, more than a
# default 4096 context, and small local models cannot use them. These are
# defaults: settings an administrator saves in Open WebUI take precedence.
cat >> /etc/aios/webui.env <<'ENV'
ENABLE_TITLE_GENERATION=true
TASK_MODEL_PARAMS='{"max_tokens": 128, "reasoning_budget_tokens": 0, "chat_template_kwargs": {"enable_thinking": false}}'
ENABLE_TAGS_GENERATION=false
ENABLE_FOLLOW_UP_GENERATION=false
ENABLE_AUTOCOMPLETE_GENERATION=false
ENABLE_SEARCH_QUERY_GENERATION=false
ENABLE_RETRIEVAL_QUERY_GENERATION=false
DEFAULT_MODEL_METADATA='{"capabilities": {"builtin_tools": false}}'
ENV
# Image generation goes through the same gateway and the same key: the appliance
# routes it to the published diffusion model, and answers with a clear error when
# there is none. Sizes and steps stay small because most machines have no GPU.
INFERENCE_KEY=$(cat /etc/aios/secrets/inference-key)
cat >> /etc/aios/webui.env <<ENV
ENABLE_IMAGE_GENERATION=true
IMAGE_GENERATION_ENGINE=openai
IMAGE_GENERATION_MODEL=aios-image
IMAGES_OPENAI_API_BASE_URL=http://127.0.0.1:8081/v1
IMAGES_OPENAI_API_KEY=$INFERENCE_KEY
IMAGE_SIZE=512x512
IMAGE_STEPS=20
ENV
rm -f /etc/aios/secrets/webui-bootstrap
chown -R aios-webui:aios-webui /var/lib/aios/webui
/opt/aios/venv/bin/python - <<'PYTHON'
from aios.core import setting, execute, uid, now
from aios.platform import apply_system
try:
    configuration = setting('system_config')
    if configuration:
        apply_system(configuration)
except Exception as error:
    execute('INSERT INTO alerts(id,severity,message,created_at) VALUES (?,?,?,?)', (uid(), 'WARNING', 'Saved tuning could not be fully applied: ' + type(error).__name__, now()))
PYTHON
runuser -u aios -- /opt/aios/venv/bin/python -m aios hardware
if [[ ! -f /var/lib/aios/system/firstboot-complete ]]; then
  runuser -u aios -- /opt/aios/venv/bin/python -m aios benchmark
  touch /var/lib/aios/system/firstboot-complete
fi
# Host keys are never cloned from the image: the installer creates this machine's
# own pair and ssh.service replaces any that go missing. With SSH switched off,
# leave no identity behind at all.
if [[ -f /etc/ssh/sshd_not_to_be_run ]]; then
  rm -f /etc/ssh/ssh_host_*
fi
