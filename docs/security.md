# Security

AIOS is built for a trusted local network, a controlled physical console and identified administrators. This page describes the protections the appliance applies.

## Accounts and sessions

- Passwords are stored with Argon2id (64 MiB, 3 iterations) and must be at least 12 characters long.
- Sessions are random values stored as SHA-256, expire after 8 hours and travel in `Secure`, `HttpOnly`, `SameSite=Strict` cookies; every change also requires a CSRF token.
- Sign-in attempts have a persistent, progressive backoff, and NGINX limits the rate of requests to the sign-in area (answering *429 Too Many Requests* beyond it).
- The bootstrap code that creates the first administrator is random, single-use and valid for 24 hours; the image contains no universal credential.
- Changing a password, disabling an account or changing its role ends every session of that account. Each user can see and end their own sessions; a SUPERADMIN can sign any other account out everywhere.
- Four roles — VIEWER, OPERATOR, ADMIN, SUPERADMIN — are enforced by the API on every request.

## Data at rest

- The database, the secrets and the backups live in directories only the services can read (`0700`, files `0600`).
- The API never returns password hashes, repository tokens, the SMTP password or the webhook secret.
- The journal records events, methods and paths — never request bodies, tokens or conversations. Engine logs are visible to ADMIN and above.
- The audit log is append-only and hash-chained, and names who did what.
- Backups can be encrypted with a passphrase: AES-256-GCM in chunks bound to their position, under a key derived with scrypt, so an altered, reordered or truncated archive is refused. Backup and restore are SUPERADMIN actions.

## Network

- The nftables firewall accepts only established connections, loopback, HTTP/HTTPS, DHCP and ICMP, plus SSH when it was enabled at installation.
- Every inference engine — llama.cpp for language models and text to speech, stable-diffusion.cpp for pictures, whisper.cpp for speech to text, and the embedding service — listens on loopback only. The gateway on port 443 is the only way in, and it authenticates every request with a session or an API key.
- API keys are per client, stored as SHA-256, with an expiry, an optional limit of requests per minute and an optional list of the models they may use; revoking one leaves the others untouched.
- The API reference and the model list answer only a signed-in user or a client holding a key.
- The portal sends a strict Content-Security-Policy: scripts, styles and connections from the appliance itself only.
- A certificate is generated on first boot and can be replaced from the portal with a certificate and key issued by your own authority; a pair NGINX does not accept is rolled back automatically.

## Outgoing connections

- Repositories and downloads use verified TLS, a DNS answer validated and pinned for the connection, the original Host and SNI, revalidated redirects, and tokens sent only to the provider's own host.
- Loopback, link-local, multicast and metadata addresses are refused; addresses on the local network only for a repository explicitly allowed to use them.
- A repository can require a catalogue manifest signed with Ed25519.
- Every download is staged, size-checked, checked against free space and verified with SHA-256; a bounded parser reads each GGUF file before it is installed.
- Notification webhooks cannot point at the appliance itself or at link-local addresses, and their messages can be signed with HMAC-SHA256.
- No path loads remote code from a model repository; `trust_remote_code` is never used.

## Services

- The control plane and the engines run as unprivileged users with systemd hardening, a read-only system, private temporary directories and no capabilities. Only the runtime manager and the hardware profiler belong to the `render` and `video` groups that opening a GPU requires.
- Privileged work — network, certificates, backups, updates, power — goes through `aios-platform`, a broker that runs a closed set of validated operations; the web process never runs shell commands.
- `aios-nvidia-driver` runs once at boot, only on machines with an NVIDIA GPU, and takes no input other than the PCI devices present.
- Open WebUI runs as its own user, writes only its own data and never reads the models. No separate vector database server is started.
- The Open WebUI distribution is rebuilt with pinned, reviewed dependencies, and the build scans them.

## Updates

- Operating system updates come from the Ubuntu archive and are installed from the portal or the console by an administrator.
- AIOS components are replaced only by releases signed with Ed25519 and verified against the public key the operator installed (`/var/lib/aios/system/release.pub`). No key ships in the image, so an appliance accepts no release until its operator trusts one; a key is never accepted from the archive it is meant to verify. Installing or removing the key is a SUPERADMIN action recorded in the audit log.
- A release is checked for its checksum and for symlinks, hardlinks, devices and path traversal before it is unpacked. Its system files — units, the NGINX site, the firewall rules — are validated by the tool that reads them before use, and a release whose services do not answer again is rolled back automatically.

## Physical console

The installed appliance's console is a closed menu: **there is no shell**, no entry that opens one, and no other terminal offers a login.

- Entries: network and service status, bootstrap regeneration, checking and installing system updates, reboot, power off, API key. Each one asks for the username and password of an AIOS account with the `ADMIN` or `SUPERADMIN` role.
- The update state is shown above the menu: the `aios-update-check` timer queries the Ubuntu archive 5 minutes after boot and then daily. Installing removes no packages and keeps the appliance's configuration files.
- Ctrl-C, Ctrl-\ and Ctrl-Z are ignored; if the menu ends for any reason systemd starts it again.
- No login on the other virtual terminals (`NAutoVTs=0`, `ReserveVT=0`; `getty@tty1` and `serial-getty@ttyS0` masked), the root account is locked, and SysRq keys are disabled (`kernel.sysrq=0`).
- GRUB is locked: the installer sets a superuser with a random PBKDF2 password that is never stored or shown. The AIOS entry boots without asking, but editing the boot parameters or opening the GRUB command line requires that password. Recovery entries are disabled and the menu is hidden.
- The password is read with the echo off and passed to the verifier on standard input, so it never appears in process arguments.
- Failed attempts use the same progressive backoff as the portal and are recorded in the audit log as `console_denied`; successful authorisations record the action.
- A disabled account authorises nothing, even with the right password.
- Before the first administrator exists, the menu offers status, bootstrap regeneration, reboot and power off, and shows the bootstrap code on the same screen.

The ISO installation environment offers no shell either: its menu holds install, reboot and power off.

Physical access to the machine and, on a hypervisor, access to the VM console and storage are administrator privileges: grant them to the appliance's administrators only.

## One identity for the portal and the chat

Open WebUI has no credential store of its own: AIOS is the login authority, through trusted-header authentication with three safeguards.

- **The headers cannot be forged by the client.** NGINX sets `X-AIOS-Email`, `X-AIOS-Name` and `X-AIOS-Role` from the authentication subrequest with `proxy_set_header`, which replaces whatever the browser sent; without a session the headers are not forwarded at all.
- **Open WebUI is reachable only through NGINX.** It listens on loopback only.
- **The session check is internal.** `/_aios/chat-session` is declared `internal` and cannot be called directly.

The SUPERADMIN role becomes a chat administrator, every other role a normal user. Disabling an account or changing its role ends its sessions, and Open WebUI follows at the next session. Users without an email address get an internal identity in the reserved `users.aios.invalid` domain, which the portal refuses as a username; email addresses are compared case-insensitively, and a second account differing only in case is refused.

## Recovery SSH (optional)

The installer asks whether to enable SSH; it is the remote way to recover the first administrator's code or the API key. When it is not enabled, both `ssh.service` and `ssh.socket` are disabled, `/etc/ssh/sshd_not_to_be_run` is present, no host keys exist and no account can authenticate.

When it is enabled, the installer creates a dedicated account with a single purpose:

- Its login shell **is not a shell**: it ignores any command sent by the client and gives no access to the filesystem or the logs.
- `sudo` is granted for **exactly three invocations**: `aios-bootstrap-recovery show`, `reset` and `apikey`. The last one shows the inference key only after verifying the username and password of an AIOS administrator, with a backoff dedicated to the SSH channel and an audit record.
- The bootstrap commands stop working as soon as an administrative account exists.
- `PermitRootLogin no`, `AllowUsers` limited to that account, TCP/agent/X11 forwarding and tunnels disabled, `MaxAuthTries 3`.
- With a public key, password authentication is switched off and the account's password is locked. With a password, it must be at least 12 characters long and is never stored in clear text.
- Host keys are generated on first boot, on the machine that will use them.

Where the physical or hypervisor console is at hand, it offers the same recovery without enabling SSH.

## Reporting

Report vulnerabilities privately, as described in [SECURITY.md](../SECURITY.md).
