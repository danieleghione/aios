# Changelog

What each release of AIOS brings. Versions follow the ISO releases.

## 1.12.4

- **Hardware** lists each disk once, under its main mount point.
- Repository cards show the state of the repository and nothing more.
- Sign-in requests beyond the allowed rate are answered with 429 *Too Many Requests*.

## 1.12.3

- **Six voices** for text to speech — Alloy, Echo, Fable, Onyx, Nova, Shimmer — each reading a whole text from the first sentence to the last. Chosen in *Speak a text*, in the chat's audio settings or through the API.

## 1.12.2

- *Speak a text* plays the spoken text in the portal's own player.

## 1.12.1

- The portal, the API and the Hardware page show the release they belong to.

## 1.12.0

- A signed application release installs the systemd units, NGINX site, firewall rules and chat stylesheet it carries, each checked before use and put back if the check fails; the same happens at every boot. An appliance updated in place gets everything a new installation has.
- Text to speech on an appliance whose language runtime predates it says which component to update.
- The Dashboard shows dedicated GPU memory over time.
- In the chat, a short sentence read aloud keeps the language of the one before it.
- A notification webhook cannot point at the appliance's own services.
- Installing operating system updates tries again, up to three times, when an Ubuntu mirror briefly does not serve a package it has just listed.

## 1.11.1

- The language catalogue lists only GGUF files that name a model architecture llama.cpp runs; diffusion models are offered by the *Image models* repository.
- Publishing a voice model points the chat's *Read aloud* button at it also when Open WebUI has saved its own defaults.

## 1.11.0

- **Text to speech**: a fourth engine reads text aloud in ten languages with llama.cpp — the chat's *Read aloud* button, *Speak a text* in the portal, and `/v1/audio/speech`. [Speech](docs/voice.md#text-to-speech)
- `/v1/embeddings` with the bundled model answers on every installation.
- A transcription or a picture made from the portal releases its model when it ends, so the model can be unloaded when idle or replaced by another of its kind.
- A lighter installer image: about 1 GiB less once installed.
- **CUDA for NVIDIA cards**, optional: `make cuda-package` builds llama.cpp's CUDA backend as a signed component; installed, it is used for NVIDIA cards in place of Vulkan, and can be removed from **Hardware**. [GPU acceleration](docs/gpu.md#cuda-for-nvidia-cards-optional)

## 1.10.0

- **Model updates**: when a repository publishes a newer revision of an installed model, the model shows *Update*. The new revision takes over the settings of the old one, which stays until you delete it.
- **Notifications**: alerts by webhook, signed with a shared secret, and by e-mail, from the level you choose.
- **API keys**: requests and tokens used per key, a limit of requests per minute, and the models a key may use.
- **Embeddings for API clients**: `/v1/embeddings` answers with the embedding model shipped in the image, with no language model loaded.
- **Encrypted, scheduled backups**: a daily backup at the hour you choose, keeping the newest ones, and a passphrase that seals every archive with AES-256-GCM. A sealed backup restores on another appliance with its passphrase. [Backup and restore](docs/backup-restore.md)
- **Unload idle models**: a model nobody has used for the configured time gives its memory back, and loads again on the next request.
- **Sessions**: see where you are signed in and end a session; administrators see each account's last sign-in and can sign it out everywhere.
- The Dashboard shows trends for memory, temperature and generation speed.
- Repositories are configured with a form for each provider; the JSON stays available under *Advanced options*.
- **Installed models** can be searched and ordered, and shows the space they take together. **Hardware** is laid out as gauges and sections, with the benchmark history.
- Confirmations and messages open in the portal instead of browser dialogs; each runtime field explains what it does.
- The chat's connection to the appliance is restored at boot also when Open WebUI has saved numeric settings.
- The chat's model selector lists language models only; speech models answer the microphone.

## 1.9.0

- Recordings up to 200 MiB are accepted by `/v1/audio/transcriptions` and *Transcribe a file*.
- Signed releases can also replace the image and speech engines, and the repositories a release adds appear on installations updated or restored from an earlier one.
- At every boot, the chat settings that connect Open WebUI to the appliance — its key, the image and speech endpoints, local document embedding, legacy function calling — are set again before the chat starts.
- The Dashboard lists alerts: a repository that fails to synchronise, a model that could not start, a failed download, storage almost full, security updates waiting, a restart pending. Each closes on its own when the condition clears, and can be dismissed.
- **Downloads** names each model; **Installed models** marks the default of each kind, language, image and speech.
- The API reference is for signed-in users, and `/v1/models` for clients holding a key.
- The lab verification is part of the repository: `make lab-test ISO=...` installs an ISO on a fresh virtual machine and runs every check.

## 1.8.1

- The speech engine transcodes recordings in a directory it owns, so any format the chat records is converted before transcription.
- The portal transcribes or draws with a model that is installed, whether or not it is published: publishing decides what the chat and the API may use.

## 1.8.0

- **Speech to text** with whisper.cpp, the third engine on the appliance: the whisper models in the catalogue, a *Transcribe a file* button in the portal, the microphone in the chat, and an OpenAI-compatible `/v1/audio/transcriptions`. A language, an image and a speech model can be loaded at once. [Speech to text](docs/voice.md)

## 1.7.1

- An application release is installed with the ownership and permissions its services need, whatever the archive carries.
- A rollback clears systemd's restart counter before starting the previous release, so it comes back immediately.

## 1.7.0

- **System → Updates** also replaces parts of AIOS itself — control plane, inference runtime, chat — from a signed archive, without reinstalling. The appliance accepts releases signed by the Ed25519 key an administrator installs, and that key can be replaced or removed at any time.
- A release is kept only when its services come back and the component answers its health endpoint again; otherwise the previous one is put back in place.
- An internal or HTTP repository can require a catalogue manifest signed with Ed25519 (`public_key` in its options; `scripts/sign-catalog.py` signs it).
- The control plane is organised by subject: the application, the gateway, and the account, model and system routes each live in their own module.

## 1.6.1

- When memory is short, the model of the other kind is unloaded before a new one is loaded, so a picture and a conversation share a small machine.
- A load the kernel stops for memory is reported as such, and the runtime manager keeps serving the models it already holds.

## 1.6.0

### Portal

- **System** shows the values the appliance is using now — hostname, time zone, NTP, governor, HugePages — and the time zone is chosen from the list the system knows.
- **Network** shows the current interface, addresses, gateway, DNS and DHCP state, and offers *Keep the new settings* after a change, within the 120-second rollback.
- **Updates** installs Ubuntu updates: what is available, how much of it is security, and whether a restart is pending.
- **TLS** shows the certificate in use, its issuer, its names, its fingerprint and its expiry.
- **API keys**: one named key per external client, with an expiry, the date it was last used and revocation. Only a SHA-256 of each key is stored.
- **Users**: a SUPERADMIN resets another user's password and deletes accounts; those controls never appear on your own account.
- **Catalogue**: one card per model, with the quantisation chosen inside it and the file recommended for this machine preselected. Parameter counts are read from model names when publishers do not declare them, and the period filter uses the upstream release date.
- **Runtime**: the model's name, the actions that apply to its state, and image models labelled as such, with a configuration form of their own.
- Navigation grouped in three sections, a menu button on phones, a *Getting started* guide, and empty pages that point to the next step.
- Dashboard: the last five minutes of CPU at first paint, and the models loaded right now with the device they run on and the memory they hold.
- Audit names who acted; logs are newest first, decoded, and cover every AIOS service; system operations are named, dated and readable.
- Confirmations open in the portal, and every action reports what it did.

### Engine

- Weights are counted whole when an integrated GPU runs the model, and a model that cannot stay resident is rated LIMITED.
- The AUTO profile serves four requests at once over one shared KV cache.
- The Runtime page reports the devices a model was loaded on and how many layers went to the GPU.

### Chat

- The *Image* switch in Open WebUI draws the picture straight away, with your own words as the prompt.
- Documents attached in the chat are embedded on the appliance by a model shipped in the image, with no Internet access.

## 1.5.1

- ModelScope discovery follows the current API, and a repository waiting for its configuration reports NOT CONFIGURED.

## 1.5

- Image generation with stable-diffusion.cpp (SD 1.5, SDXL Turbo, FLUX.1 schnell): catalogue entries, installation of every component, a second runtime on its own port, an OpenAI-compatible `/v1/images/generations`, and *Generate a picture* in the portal.

## 1.4 and earlier

- GPU acceleration through Vulkan with automatic device choice, vision projectors, the guided installer, the closed console, recovery SSH, backups and signed component releases.
