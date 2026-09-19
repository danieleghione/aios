# Design notes

The choices behind AIOS, and the reasons for them. [Architecture](architecture.md) describes what the system is; this page explains why it is that.

## Goals

- **One machine, one appliance.** Install from an ISO onto bare metal or a VM and get a working system: administration portal, chat, inference engine, model catalogue. No container stack to operate, no external service to depend on.
- **Runs on hardware people already have.** Intel/AMD x86-64, no GPU required, and any GPU used when there is one. The build compiles llama.cpp with every x86 CPU variant and the Vulkan backend and lets upstream pick at runtime.
- **Local by construction.** After the models are downloaded, nothing leaves the machine. Model weights are never executed as code and `trust_remote_code` is never used.
- **Operable without a shell.** Everything an administrator needs is in the portal or in a closed console menu. That is also what makes the appliance lockable.
- **Honest about limits.** RAM estimates, compatibility ratings and errors say what is really happening, including when a model will not fit.

## Non-goals

- Multi-node clustering, per-model GPU scheduling or serving many concurrent models. One language model and one image model are loaded at a time.
- Being a general-purpose Linux box. There is no desktop, no user shell and no package management beyond system updates.
- Direct exposure to the Internet. The trust boundary is a LAN with identified administrators.

## Decisions worth knowing

**SQLite in WAL mode, not a database server.** One fewer daemon to run and back up, with real transactions, foreign keys, versioned migrations and consistent online backups. Model weights and archives stay on the filesystem, not in the database. A persistent anchor connection per process keeps the WAL files alive under churn.

**A separate runtime manager.** The control plane records the *desired* state; `aios-runtime-manager` owns the llama.cpp processes. Management stays responsive while a model loads or crashes.

**A privileged broker with a closed set of operations.** `aios-platform` runs as root and accepts only validated operations (network, TLS, power, backup, restore, updates). The backend never builds shell commands from input.

**The catalogue only offers what can run.** Repositories publish more than models: vision projectors, split archives, speculative-decoding drafts, importance matrices, adapters. They are filtered out from the structure of their GGUF header, not from their names, and refused again at download, publication and start — so a mistake surfaces as a sentence, not as an obscure engine error. A vision projector is the one companion that is kept: it is downloaded with the model it belongs to and handed to llama.cpp, because without it a vision model silently loses its eyes.

**Publishers, not fixed lists.** A hard-coded list of models can never see a release published after it was written. Repositories therefore query trusted publishers for their newest GGUF releases and keep the most recent distinct models that this appliance can run.

**Ask for a large context, then fit it.** The default runtime asks for 101024 tokens and reduces it to the smaller of what the model declares and what free RAM allows, halving until it fits. An explicit value is honoured instead, and refused with a reason when it does not fit.

**One GPU path for every vendor.** CUDA, ROCm and oneAPI would each add gigabytes, a narrow list of supported cards and a separate build to test. Vulkan reaches NVIDIA, AMD and Intel GPUs — discrete, integrated or passed through to a VM — with one llama.cpp backend and the drivers Ubuntu already ships. The price is some speed against CUDA on NVIDIA cards; the gain is that the same image works on whatever the operator owns. A GPU that fails to load a model never costs the model: it starts again on the CPU and the portal says why.

**A budget for reasoning.** Models that think before answering can spend an entire context on reasoning and stop without answering. By default reasoning gets half the context of a single request; then llama.cpp closes it and the model must answer.

**Two engines, one appliance.** Diffusion models need an engine llama.cpp does not provide, so stable-diffusion.cpp runs beside it: same ggml foundation, same Vulkan backends, its own prefix and port. Both are owned by the same runtime manager and reached through the same authenticated gateway, so image generation inherits the appliance's rules instead of becoming a second system to operate.

**One identity for portal and chat.** Open WebUI has no credential store: NGINX authenticates the session against the control plane and passes trusted headers that the client cannot forge. One account, one place to disable it.

**A console that is a menu, not a terminal.** No shell, no login on other virtual terminals, locked boot entries, SysRq off, every action behind an administrator password. The cost is that a lost administrator password means restoring a backup or reinstalling; that trade is deliberate.

**Append-only audit with hash chaining.** SQLite triggers refuse UPDATE and DELETE and each row chains to the previous one. It is an application-level guarantee: root or physical access is outside it, and the documentation says so rather than implying more.

## History

The project started from a single, long specification written for an AI coding agent, and was then driven by real installations on Proxmox: every defect found on that appliance — slow installs, empty catalogues, models that never answered, a console that asked for nothing — turned into a fix, a test and a rebuild. The current behaviour reflects that loop rather than the original document.
