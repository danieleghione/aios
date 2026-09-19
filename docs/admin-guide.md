# Administration

The Dashboard shows CPU, RAM, storage, a local CPU history, load, the number of models and requests, repository state and an aggregated health status. **Installed models** holds both kinds of model this appliance runs: language models, which answer the chat and the API, and diffusion models, which generate pictures and carry a *Generate a picture* button ([image generation](image-generation.md)). One of each kind can be loaded at the same time. Hardware shows the GPUs models use ([GPU acceleration](gpu.md)), topology, ISA, NUMA, disks, network, temperatures where the machine exposes them, and a benchmark you can run again. The benchmark measures memory copy and FP32 matrix multiplication: it is a non-destructive calibration, not a prediction of tokens per second.

Everything below is done from the portal; no shell is involved.

1. Sign in as an administrator and, if asked, change the initial password.
2. **Repositories**: configure the provider, models or publishers and an optional token, then press *Enable and test connection* and *Synchronise*.
3. **Catalogue**: search and read author, licence, quantisation, size, estimated RAM and the reasons behind the compatibility rating. Filters narrow the list by text, by the period in which the model was found and by how well it fits this machine.
4. Accept the licence. An ADMIN can confirm an override for a model rated NOT_RECOMMENDED or INCOMPATIBLE; an OPERATOR cannot.
5. Queue the download and follow progress, speed, ETA, pause/resume and retries under **Downloads**.
6. **Installed models**: publish the model, configure its runtime if needed, and start it. The card shows STOPPED, STARTING or RUNNING and the button offers the opposite action.
7. Open the chat with **Open WebUI** in the header: a published model appears in its selector and answers once it is running.

## Roles

| Role | Can |
|---|---|
| VIEWER | read state and catalogue |
| OPERATOR | manage downloads and runtimes |
| ADMIN | manage repositories, policies, deletion, audit and logs |
| SUPERADMIN | manage accounts, network, TLS, backup/restore, updates and power |

The interface may show sections a role cannot use, but the API always enforces the role and answers with a clear error.

**System** covers hostname, NTP, time zone, proxy, CPU governor, HugePages reservation, licence and concurrency policy, network with rollback, certificates, backups and signed updates. Privileged operations are queued and their outcome is visible under *System operations*. Reboot and shutdown give one minute of notice.

The runtime log carries the messages from llama.cpp itself; application events are JSON in the journal. Remote names and metadata are rendered as text, never as HTML. Repository tokens are never sent back to the browser: leaving the field untouched keeps the stored value, an empty string deletes it.
