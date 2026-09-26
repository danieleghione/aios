# Demo: from an empty disk to an appliance that chats, draws, listens and speaks

Every screenshot below comes from a real appliance installed from the released ISO on a virtual machine with two cores and no GPU: everything shown here runs on the CPU. The language, image and speech models run in 4 GiB of RAM; the voice model needs 8 GiB, which is what this machine has.

## 1. Boot the installer

The ISO boots under BIOS and UEFI. The first entry starts by itself after five seconds; the second one is for a serial console.

![AIOS boot menu](images/boot-menu.png)

## 2. Answer the guided installer

No menu to find, no shell: the installer lists the disks it can use, proposes the first one and asks for language, keyboard, hostname, network, time zone, clock and optional recovery SSH.

![Guided installer, with a rejected hostname asked for again](images/installer.png)

Each value is checked as you type it. A wrong value is explained and asked for again on its own — nothing typed before it is lost:

```
  Invalid value: hostname must be 1-63 letters, digits and hyphens. Please enter it again.
Hostname [aios]:
```

Once you confirm the summary, the disk is erased and the system is installed in five steps — usually a few minutes — and the machine reboots by itself.

## 3. First boot

![AIOS boot splash](images/boot-splash.png)

The console then shows the address, the URLs and a one-time bootstrap code, valid 24 hours:

```
AIOS READY
Hostname: aios-lab
IP: 192.168.1.50
User portal: https://192.168.1.50/
Admin portal: https://192.168.1.50/admin/
Admin bootstrap secret (valid 24h): X1fFQ9gGCaTFe9GOY0fwaAeQe44ukv3HWGwdqco5zvg


AIOS local console
1) Show network and service status
2) Regenerate the bootstrap code (only while no administrator exists)
3) Check and install system updates
4) Reboot
5) Power off
6) Show API key for external tools (n8n, OpenAI clients)
```

There is no shell behind that menu, and every entry asks for an administrator password once one exists.

## 4. Create the administrator

Open `https://IP/admin/` and use the bootstrap code to create your own account. That code is then consumed. The release number is at the bottom of every page.

![Sign-in page](images/portal-sign-in.png)

## 5. The appliance at a glance

![Dashboard](images/portal-dashboard.png)

CPU, memory, model storage and the models running; the CPU of the last five minutes; trends for memory, GPU memory, temperature and generation speed; the models loaded right now and the device each one runs on; the state of every repository; and, further down, the **alerts** — a repository that cannot be reached, a model that did not start, storage almost full, security updates waiting. An alert closes by itself when the situation is resolved, and can also be sent by e-mail or webhook (step 13).

## 6. Check the hardware and the GPU

**Hardware** shows what the models will run on. The *GPU acceleration* card lists every GPU AIOS can use — NVIDIA, AMD or Intel, dedicated or integrated — with its memory and driver, and marks the ones models use; this machine has none, so the card says models run on the CPU. Below it: the processor and its instructions, memory and storage as gauges, and the calibration benchmark with its earlier runs.

![Hardware and GPU](images/portal-hardware.png)

## 7. Enable a model repository

Repositories ship disabled. One button enables a repository and tests the connection; *Synchronise* then fetches metadata only — no weights. Besides the language model publishers there are three repositories for the other kinds: **Image models**, **Speech models** and **Voice models**.

![Repositories](images/portal-repositories.png)

Each provider is configured with a form of its own — here, the publishers and the search words a Hugging Face repository follows. The same options are available as JSON under *Advanced options*.

![Repository form](images/portal-repository-form.png)

## 8. Pick a model

The catalogue lists the newest releases first and only files this appliance can actually run: one card per model, with the file that suits this machine already selected. Filter by text, by the period in which a model was released, and by how well it fits this machine (OPTIMAL … INCOMPATIBLE). Before installing, each card shows licence, parameters, size, the estimated memory and the reasons behind the rating; you accept the licence explicitly.

![Catalogue](images/portal-catalogue.png)

Image, speech and voice models appear in the same catalogue, marked with their kind and rated on the memory they need. Here the voice models, each downloaded together with the codec it needs:

![Voice models in the catalogue](images/portal-voice-catalogue.png)

## 9. Publish and start

**Downloads** follows each model by name, with progress, speed and time left.

![Downloads](images/portal-downloads.png)

Under **Installed models** every model shows its kind, whether it is the default of its kind, and whether it is running and published. A model is started by hand or on the first request, and one of each kind can be loaded at the same time — here a voice model and a speech model are running together. Each kind carries its own tool: *Speak a text*, *Transcribe a file*, *Generate a picture*. When a repository publishes a newer version of an installed model, the card offers *Update*.

![Installed models](images/portal-installed-models.png)

**Runtime** shows each loaded model, the device it runs on, what it was started with and its log.

![Runtime](images/portal-runtime.png)

## 10. Chat

Open WebUI uses the same AIOS account; the published language models are in its selector. Documents attached to a message are read on the appliance, the microphone turns speech into text, and the speaker icon under each answer reads it aloud.

![Chat](images/chat-answer.png)

## 11. Speak and listen

*Speak a text* reads what you type with one of six voices — Alloy, Echo, Fable, Onyx, Nova and Shimmer — in the language it detects or the one you choose, and plays it in the page. The voice stays the same from the first sentence to the last. In the chat, each user chooses the voice under *Settings → Audio*.

![Speak a text](images/portal-speak.png)

*Transcribe a file* turns a recording into text. The recording here is the one the voice model produced: the round trip shows both engines at work.

![Transcribe a file](images/portal-transcribe.png)

[Speech: listening and speaking](voice.md)

## 12. Generate pictures

*Generate a picture* opens a small studio: a prompt, a size, the number of steps. The model is loaded on the first picture, next to the language model, and the result appears in the page with the time it took. The chat's *Image* switch draws the same way from what you write.

![Generating a picture from the portal](images/portal-image-studio.png)

Without a GPU a 512×512 picture takes minutes rather than seconds: [image generation](image-generation.md) has the measured numbers and the trade-offs.

## 13. Look after the appliance

**Backups** run every day at the hour you choose and keep the newest ones; with a passphrase, every archive is encrypted and can be restored on another AIOS appliance with that passphrase.

![Backups](images/portal-backup.png)

**Notifications** send each new alert by e-mail and to a webhook — n8n, a team chat, a ticketing system — from the level you choose, with a button to test them.

![Notifications](images/portal-notifications.png)

**Updates** shows the operating system updates available, how many are security fixes and whether a restart is pending, and installs them. The same page takes signed releases of AIOS components, undone automatically if they do not work.

![Updates](images/portal-updates.png)

**Users and roles** shows when each account last signed in and how many sessions it has open, with password reset, sign-out everywhere and deletion.

![Users](images/portal-users.png)

## 14. Use it from your own tools

Under **System → API keys** each tool gets its own key, with an expiry, an optional limit of requests per minute and the models it may use; the list shows what each key has used.

![API keys](images/portal-api-keys.png)

The same models answer any OpenAI-compatible client:

```bash
curl -k https://IP/v1/models -H "Authorization: Bearer <key>"
curl -k https://IP/v1/chat/completions \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"model":"<id>","messages":[{"role":"user","content":"What is 4+9?"}]}'
curl -k https://IP/v1/audio/speech \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"input":"Good morning.","voice":"nova"}' -o morning.mp3
```

In n8n, use the *OpenAI Chat Model* node with base URL `https://IP/v1`, the key and the model `id`. See [the API guide](api.md) for pictures, transcription, embeddings and the limits.
