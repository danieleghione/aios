# Administration

The navigation is grouped in three parts: *Overview* (Dashboard, Hardware), *Models* (Catalogue, Installed models, Downloads, Runtime, Repositories) and *Administration* (Users and roles, System, Audit, Logs); on a phone it folds into a menu button. The Dashboard shows CPU, RAM, storage, the CPU history of the last five minutes, trends for memory, dedicated GPU memory, the hottest sensor and generation speed, load, requests, tokens per second, the models loaded right now with the device they run on and the memory they hold, repository state and an aggregated health status. Until a model is installed it also shows a *Getting started* guide, and every empty page points to the step that fills it. **Installed models** holds the three kinds of model this appliance runs: language models, which answer the chat and the API; diffusion models, which generate pictures and carry a *Generate a picture* button ([image generation](image-generation.md)); speech models, which turn recordings into text and carry a *Transcribe a file* button ([speech to text](voice.md)); and voice models, which read text aloud and carry a *Speak a text* button ([text to speech](voice.md#text-to-speech)). One of each kind can be loaded at the same time. Hardware shows the GPUs models use ([GPU acceleration](gpu.md)), topology, ISA, NUMA, disks, network, temperatures where the machine exposes them, and a benchmark you can run again. The benchmark measures memory copy and FP32 matrix multiplication: it is a non-destructive calibration, not a prediction of tokens per second.

Everything below is done from the portal; no shell is involved.

1. Sign in as an administrator and, if asked, change the initial password.
2. **Repositories**: choose the provider and fill in the fields it asks for — publishers and search words, a fixed list of models, or the manifest URL and its signing key — and an optional token (*Advanced options* shows the same settings as JSON), then press *Enable and test connection* and *Synchronise*. An enabled repository offers *Test connection*, *Disable* and *Synchronise*; a disabled one shows DISABLED.
3. **Catalogue**: one card per model. Choose the quantisation inside the card: the one recommended for this machine is preselected (best compatibility first, then Q4_K_M and the other usual trade-offs). Each card shows author, licence, parameters (read from the model, or from its name and marked ≈ when the publisher does not declare them), release date, size, estimated RAM and the reasons behind the rating. Filters narrow the list by text, by the period in which the model was *released* and by how well it fits this machine.
4. Accept the licence. An ADMIN can confirm an override for a model rated NOT_RECOMMENDED or INCOMPATIBLE; an OPERATOR cannot.
5. Queue the download and follow progress, speed, ETA, pause/resume and retries under **Downloads**.
6. **Installed models**: publish the model, configure its runtime if needed, and start it. The card shows STOPPED, STARTING or RUNNING and the button offers the opposite action.
7. Open the chat with **Open WebUI** in the header: a published model appears in its selector and answers once it is running.

## Alerts

The Dashboard lists what needs attention: a repository whose synchronisation fails, a model that could not start, a download that failed, model storage almost full, security updates waiting or a restart pending after them. Each alert closes on its own when the condition clears; *Dismiss* closes it by hand, and it opens again if the condition is seen again.

**System → Notifications** sends alerts outside the portal as well, once each time one opens and from the level you choose (INFO, WARNING or ERROR): to a **webhook**, which receives a JSON POST (`event`, `id`, `severity`, `message`, `created_at`, `appliance`), signed with HMAC-SHA256 in `X-AIOS-Signature` when a secret is set; and by **e-mail** through your SMTP server (STARTTLS, TLS or plain). *Send a test* tries every configured channel and shows the answer. The page shows the last delivery of each channel; a destination that does not answer is tried again every minute for an hour. The SMTP password and the webhook secret are stored under `/etc/aios/secrets` and never shown again. A webhook may point at any address on your network, but not at the appliance itself or a link-local address, where its own services listen. A newer revision of an installed model is also an alert, of level INFO.

The model that answers a request naming none is marked **DEFAULT** under **Installed models**, one per kind: language, image, speech and voice. Choose it from the model's runtime configuration.

## Roles

| Role | Can |
|---|---|
| VIEWER | read state and catalogue |
| OPERATOR | manage downloads and runtimes |
| ADMIN | manage repositories, policies, deletion, audit and logs |
| SUPERADMIN | manage accounts, network, TLS, backup/restore, updates and power |

**Users and roles** shows when each account last signed in and how many sessions it has open; a SUPERADMIN can *Sign out everywhere* an account left signed in on a lost device. Under **System → Account** you see where you are signed in yourself — browser, address, since when — and can end any of those sessions.

The interface may show sections a role cannot use; the API enforces the role and answers in plain words. A SUPERADMIN can reset another user's password (the user must choose a new one at the next sign-in and every session ends) and delete an account; the controls never appear on your own account, and the last active SUPERADMIN cannot be removed.

**System** covers hostname, NTP, time zone (chosen from the system's list), proxy, CPU governor, HugePages reservation, licence and concurrency policy, network with rollback, certificates, client API keys, backups (daily schedule, retention and encryption: [backup and restore](backup-restore.md)) and operating system updates. *Model policy* also sets **Unload idle models after**: a loaded model that has answered nothing for that many minutes is stopped and its memory given back, unless it is answering a request or set to start with the appliance; it loads again on the next request. 0 keeps models loaded. Every form shows the values the system is using now, so *Apply* changes only what you edit. *Network* shows the current connection and, after a change, a *Keep the new settings* button until the 120-second rollback. *TLS* shows the certificate in use, who issued it and when it expires. *API keys* creates a named key per external tool, with an expiry, and revokes it. *Updates* shows the Ubuntu updates available and installs them. Privileged operations are queued and their outcome is visible under *System operations*, with date and a readable result. Reboot and shutdown give one minute of notice. Confirmations open in the portal, not in a browser dialog, and every action reports what it did.

The runtime log carries the messages from llama.cpp or stable-diffusion.cpp. **Logs** shows the journal of each AIOS service — control plane, runtime manager, downloads, platform, Open WebUI, repository sync, hardware profiler, NVIDIA driver, update check, first boot — newest first, with warnings and errors highlighted. **Audit** names who did what, not an identifier. Remote names and metadata are rendered as text, never as HTML. Repository tokens are never sent back to the browser: leaving the field untouched keeps the stored value, an empty string deletes it.

## Documents in the chat

Files attached in Open WebUI (text, PDF, office documents) are split and embedded on the appliance by `all-MiniLM-L6-v2`, a 90 MB model shipped in the image under `/opt/aios/embedding`; nothing is fetched from the Internet. The relevant passages are placed in the prompt of the language model (Open WebUI's *legacy* function calling, which small local models handle better than tool calls).
