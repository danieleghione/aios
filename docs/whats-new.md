# What's new since 1.5.1

AIOS 1.12 is a large step from the 1.5.1 release: the appliance now listens and speaks, keeps itself and its models up to date, tells you when something needs attention, and can be run entirely from the portal. This page describes what you will notice; the [changelog](../CHANGELOG.md) lists every release.

## It listens and it speaks

- **Talk to the chat.** The microphone in Open WebUI turns what you say into text on the appliance itself. In the portal, *Transcribe a file* turns a recording (WAV, MP3, M4A, OGG, WebM, up to 200 MiB) into text and shows how long it took.
- **Hear the answers.** The chat's *Read aloud* button reads answers with the appliance's own voice, in ten languages, Italian and English among them. In the portal, *Speak a text* reads anything you type.
- **Six voices to choose from.** Alloy, Echo, Fable, Onyx, Nova and Shimmer, from deep to bright. The voice you choose reads the whole text, from the first sentence to the last. Each chat user picks one under *Settings → Audio*.
- **Four kinds of model at once.** A language model, an image model, a speech model and a voice model can be loaded together, so a picture, a transcription and a conversation do not wait for each other.

[Speech: listening and speaking](voice.md)

## Models that stay current

- **Update a model in one click.** When a repository publishes a newer version of a model you installed, the model shows *Update*. The new version keeps your settings, and the previous one stays until you delete it.
- **A catalogue that is easier to read.** One card per model, with the file that suits this machine already selected, the number of parameters, the release date and a filter by release period.
- **A default for each kind.** The model that answers when nobody names one is marked on its card, separately for language, image, speech and voice.
- **Models give their memory back.** A model nobody has used for a while can be unloaded on its own, and it loads again at the next request.
- **Search and order** your installed models, and see how much space they take together.

## Know what is happening

- **Alerts on the Dashboard**: a repository that cannot be reached, a model that did not start, a failed download, storage almost full, security updates waiting, a restart needed. Each alert closes by itself when the situation is resolved.
- **Notifications by e-mail or webhook** (for example to n8n or a team chat), from the level you choose, with a test button.
- **Trends at a glance**: memory, GPU memory, temperature and generation speed over the last minutes, beside the CPU.
- **A clearer Hardware page**, with gauges for memory and storage, the processor, the GPUs in use and the history of the benchmark.

## Run the appliance from the browser

- **Settings as they really are.** Hostname, time zone (chosen from a list), time servers and the other system settings show the values in use.
- **Network changes you can take back.** After a change the portal asks you to keep it; without confirmation the previous settings come back after two minutes.
- **Operating system updates from the portal**: what is available, how much of it is security, and a button to install it.
- **The certificate in use**, who issued it and when it expires.
- **Users and sessions.** See when each account last signed in and from where you are signed in, end a session, sign an account out everywhere, reset a password or delete an account.
- **Readable history**: the audit log names who did what, and the logs of every service are one click away.
- **A friendlier portal**: a guide for the first steps, navigation that folds into a menu on phones, forms that explain each field, repository settings as simple forms, and confirmations inside the page.

## Backups you do not have to remember

- **Every day, at the hour you choose**, keeping the newest ones.
- **Protected by a passphrase** if you set one: a backup copied elsewhere cannot be opened without it, and it can be restored on another AIOS appliance with that passphrase.

[Backup and restore](backup-restore.md)

## Updates without reinstalling

- **Parts of AIOS can be updated from the portal** — the application, the inference engines and the chat — with releases signed by a key you install. An update that does not work is undone automatically.
- **NVIDIA cards can use CUDA**: an optional package, installed the same way, makes many NVIDIA cards faster; it can be removed from the Hardware page.

## For your own tools

- **One key per tool**, with an expiry, the requests and tokens it has used, an optional limit of requests per minute and the models it may use.
- **More OpenAI-compatible endpoints**: speech to text (`/v1/audio/transcriptions`), text to speech (`/v1/audio/speech`) and embeddings (`/v1/embeddings`, answered by a small model included in the appliance, with no other model loaded), beside chat and pictures.

[API](api.md)

## In the chat

- **The Image switch** draws a picture straight away from what you write.
- **Documents you attach** are read on the appliance, with no Internet access.
- **Only chat models** appear in the model selector; pictures, the microphone and reading aloud use their own models.
