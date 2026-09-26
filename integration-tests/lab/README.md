# Lab verification

Every release is installed from its own ISO on a fresh virtual machine and exercised the way an operator would: guided installer on the serial console, first administrator, repositories, catalogue, models of all three kinds, the portal in a real browser, the chat, and the operations that change the system.

```bash
make iso
make lab-test ISO=dist/aios-installer-<version>-x86_64.iso
```

`make lab-test` runs [run.sh](run.sh): it installs the ISO with [install.py](install.py), boots the result with [boot.sh](boot.sh) (4 GiB of RAM, two vCPUs, HTTPS on `127.0.0.1:28443`), and runs the checks below in order. Each check prints one line per assertion, `[OK ]` or `[BAD]`, and a summary per area.

| Check | What it proves |
|---|---|
| `portal_api.py` | first administrator; system settings, network with rollback, operating-system updates; users, password reset and deletion; repositories, catalogue periods; installs a language and an image model |
| `experience_api.py` | grouped catalogue, parameters, audit, logs, TLS, validation, dashboard, memory rating, client API keys, parallel slots |
| `portal.mjs` | the same pages in Chromium: wording, validation, runtime and repository controls, system tabs |
| `experience.mjs` | *pre*: the guide on an empty appliance; then navigation on desktop and phone, catalogue cards, audit, logs, operations, TLS, API keys, confirmations |
| `documents.mjs` | a document attached in the chat is embedded offline and retrieved |
| `voice.py` | speech models discovered, installed, transcribing from the portal and the API, beside a language model |
| `operations_api.py` | recordings above 32 MB accepted, built-in repositories, alerts raised and dismissed, downloads by name, default model per kind, nothing answering without a login |
| `phase_b.mjs`, `phase_b_api.py` | repository form per provider, trends, sessions and signing out, idle models unloaded, backups sealed with a passphrase, the daily schedule, a restore with the right passphrase and not with a wrong one |
| `phase_c_api.py` | notifications received by a webhook and a mailbox on the lab host, API keys counted and limited, embeddings from the bundled model, revision state of installed models |
| `updates.py` | release key installed by the operator, a signed application update that also installs its NGINX site, a broken release rolled back with the site of the release put back |
| `publish.py`, `chat_image.mjs` | a picture drawn from the chat's Image switch with the language model loaded |
| `chat_settings.mjs` | a chat setting pointed elsewhere is put back at the next boot (restarts the appliance) |
| `phase_d_api.py`, `phase_d.mjs` | after a power-off from the portal, on the same disk with more memory: a voice model discovered, installed with its codec and published; it speaks from the portal and through `/v1/audio/speech`, the speech model hears the same sentence back, and the chat reads aloud with it |

## Requirements

- QEMU with KVM, OVMF, and `sudo` without a password prompt for the lab commands;
- the project virtualenv (`make build`) and the portal's Playwright (`npm --prefix frontend-admin exec playwright install chromium`);
- Internet access from the VM for repositories and model downloads.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AIOS_LAB_DIR` | `build/lab` | disk, firmware variables, console log, screenshots, work files |
| `AIOS_LAB_URL` | `https://127.0.0.1:28443` | where the appliance answers |
| `AIOS_LAB_ADMIN`, `AIOS_LAB_PASSWORD` | `admin@example.org`, a password made up on first use and kept in `AIOS_LAB_DIR/work/admin-password` | the administrator the checks create and use |
| `LAB_RAM` | `4096` | memory of the lab VM, in MiB |
| `LAB_VOICE_RAM` | `8192` | memory of the VM for the voice checks, in MiB |

The recovery SSH key is generated in `AIOS_LAB_DIR` on first use, and every other account a check creates gets a new random password. Nothing in these scripts belongs to a real account.
