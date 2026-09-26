# AIOS architecture

AIOS is an x86-64 appliance based on Ubuntu 24.04 LTS (hardware enablement kernel), with no desktop, no mandatory SSH and no containers, bootable under both UEFI and legacy BIOS. The raw GPT disk carries a 256 MiB FAT32 ESP, a 10 GiB ext4 root, an ext4 data partition over the rest of the medium, and a 1 MiB BIOS boot partition in the space before the ESP, so the data partition stays last and can grow at boot.

NGINX exposes HTTPS on the LAN: `/` serves Open WebUI, `/admin/` the React portal, `/api/v1/aios/` the FastAPI control plane and `/v1/` the inference gateway. Open WebUI already has its own `/api/v1/` routes, so the `aios` suffix avoids a real collision. Ports 8080, 8081 and 8090 are loopback only.

SQLite in WAL mode avoids another daemon while keeping transactions, foreign keys, versioned migrations and consistent online backups. `schema.sql` holds the idempotent initial migration and the `schema_migrations` table. Models and archives never live inside the database. Sessions store only hashes of random tokens. Repository secrets are separate `0600` files; the database and backup directories are `0700`.

The control plane records the desired state of each runtime; `aios-runtime-manager` owns the engine processes of every kind. That separation keeps management available during a crash or a long load. One model of each kind can be loaded at a time; when memory is short, loading one unloads a model of another kind that is not answering. `aios-download-worker` owns the persistent queue, the `.part` files, resume and backoff. `aios-platform` runs, as root, a closed set of validated operations; the backend never invokes arbitrary shell commands.

Open WebUI uses a dedicated virtualenv, a separate user and persistent data in `/var/lib/aios/webui`. It has no access to the GGUF weights, to administrative secrets or to the control plane database. Its OpenAI endpoint and the local key are configured on first boot. The downstream distribution keeps upstream code, frontend and licence, applying explicit patches to dependency constraints where needed: see `config/openwebui-security-overrides.json` and the provenance file generated during the build.

Three ggml projects are compiled from pinned sources, each with dynamic backends, every x86 CPU variant and Vulkan, and each in its own prefix so none can load another's backends. They run four kinds of model, each on its own loopback port:

| Kind | Engine | Prefix | Port |
|---|---|---|---|
| Language | llama.cpp `llama-server` | `/opt/aios/runtime` | 8090 |
| Image | stable-diffusion.cpp `sd-server` | `/opt/aios/imaging` | 8091 |
| Speech to text | whisper.cpp `whisper-server` | `/opt/aios/voice` | 8092 |
| Text to speech | llama.cpp `llama-tts`, behind a small service | `/opt/aios/runtime` | 8094 |

One table in the runtime manager (`ENGINES` in `runtime.py`) says, for each kind, which models it runs, its port, how it starts and how it reports that it is ready. The embedding model shipped for the chat's documents also answers `/v1/embeddings` for API clients, on port 8093: systemd starts it on the first request and it exits after ten minutes without one. llama.cpp is compiled with `GGML_NATIVE=OFF`, dynamic backends, every x86 CPU variant available in the pinned commit and the Vulkan backend. Instruction selection happens inside the upstream runtime. Before each start the runtime manager asks llama.cpp which GPUs it can use and passes the chosen ones explicitly, or `--device none`; `aios-nvidia-driver` installs the NVIDIA kernel modules that match the cards at boot. See [GPU acceleration](gpu.md). No remote model is executed as code, and `trust_remote_code` is never used in the AIOS inference path.

The audit log has SQLite triggers that refuse UPDATE and DELETE, plus a SHA256 chain. That guarantees append-only at the application level; it does not survive a root compromise or someone replacing the whole database file. Protect physical and hypervisor console access.

## Visual identity

The logo is `branding/AIOS_Logo.png`. From it, `scripts/make-branding.py` derives every other image — the portal mark, the lockup used on the sign-in page and the boot splash, the boot menu background and the favicon — and the results are committed under `branding/`, so a build needs no image library. Regenerate them after changing the logo:

```bash
sudo cp branding/AIOS_Logo.png scripts/make-branding.py build/rootfs/tmp/
sudo chroot build/rootfs /opt/aios/webui/bin/python /tmp/make-branding.py /tmp/out
sudo cp build/rootfs/tmp/out/*.png branding/
```

The portal palette comes from the logo: cyan `#22d3ee`, blue `#3b6dff`, violet `#8b5cf6` over `#05070e`, with the brand gradient on primary buttons, the selected menu entry and the charts. The boot splash and the GRUB menu use the same colours.

In the chat only the colours are applied, through `config/webui-custom.css`, which Open WebUI loads from `/static/custom.css`: its licence forbids replacing the name, the logo and other identifiers, except for deployments of up to fifty users or with the author's agreement.
