# Security

Trust boundary: the LAN, a controlled physical console, identified administrators.

Passwords use Argon2id (64 MiB, 3 iterations); sessions are random values stored as SHA256, expire after 8 hours, and travel in Secure/HttpOnly/SameSite=Strict cookies with a CSRF token on every mutation. Login attempts have a persistent backoff and NGINX adds rate limiting. The bootstrap secret is random, single-use and expires after 24 hours; the image contains no universal credential. Changing a password revokes every session of that account.

The database and the backups live in `0700` directories; the database and secrets are `0600`. The backend never returns password hashes or repository credentials. The journal records events, method and path — not bodies, queries, tokens or conversations. Upstream runtime logs may contain technical details of the model: ADMIN access. The audit log is append-only with hash chaining; root and physical access stay outside that guarantee.

Downloads use verified TLS, a DNS address validated and pinned for the connection, the original Host/SNI, revalidated redirects and tokens confined to the provider host. Loopback, link-local, multicast, metadata endpoints and private ranges are refused unless a LAN repository is explicitly allowed. A `.part` staging file, a size limit, a free-space check, SHA256 and a bounded GGUF parser come before installation. A valid GGUF structure does not prove a model is semantically correct: the runtime can still refuse it.

Both inference engines — llama.cpp for language models and stable-diffusion.cpp for image models — listen on loopback only, with no key of their own: the gateway on port 443 is the only way in, and it authenticates every request. llama.cpp and the control plane are native non-root services with systemd hardening, a protected filesystem and empty capabilities. The runtime manager and the hardware profiler also belong to the `render` and `video` groups, which is what opening a GPU requires; the control plane does not. `aios-nvidia-driver` runs as root once at boot and only on machines with an NVIDIA GPU: it installs kernel module packages carried in the read-only image (or, for a kernel the image did not ship with, from the Ubuntu archive) and loads them. It takes no input other than the PCI devices present. Open WebUI runs as another user, can write only its own data and never reads the models. No ChromaDB server is started: Open WebUI uses the embedded library, so the Chroma HTTP APIs named in advisories are not exposed. No AIOS path loads remote Python code from GGUF repositories.

nftables firewall: incoming traffic is allowed only for established connections, loopback, HTTP/HTTPS, DHCP and ICMP. SSH is absent unless it was enabled during installation; in that case port 22 is opened and the account created is limited to recovering the first administrator's code (see below). Internal services listen on loopback only. A self-signed certificate is generated on first boot and can be replaced with a matching certificate and key; if NGINX refuses the new pair, the previous one is restored. The initial certificate does not attest the identity of a DHCP address: check it from the console or install a valid certificate.

Component updates require an Ed25519 signature verified against `/etc/aios-release.pub`, a checksum, and an archive free of symlinks, hardlinks, devices and traversal. The trusted public key is installed from the console; it is never accepted from the same archive it is meant to verify. Backup and restore are SUPERADMIN privileges: backups include secrets and belong on encrypted storage. Version 1 does not encrypt the disk or the backups by itself.

The downstream Open WebUI distribution applies explicit overrides to upstream pins through a wheel with the local version `+aios.1`, preserving code and licence. Do not read "no advisory applies to the configured path" as "no vulnerabilities". Secure Boot is not configured.

## Physical console

The installed appliance's console is a closed menu: **there is no shell**, no entry that opens one, and no other terminal offers a login.

- Entries: network and service status, bootstrap regeneration, checking and installing system updates, reboot, power off, API key. Each one asks for the username and password of an AIOS account with the `ADMIN` or `SUPERADMIN` role.
- The update state is shown above the menu: the `aios-update-check` timer queries the Ubuntu archive 5 minutes after boot and then daily; when packages are waiting (with the number of security ones) the menu says so and entry 3 installs them. Installing removes no packages and keeps the appliance's configuration files; if a reboot is needed the menu says so. Installing software always requires an administrator, even before one exists.
- Ctrl-C, Ctrl-\ and Ctrl-Z are ignored; if the menu ends for any reason systemd starts it again.
- No login on the other virtual terminals (`NAutoVTs=0`, `ReserveVT=0`; `getty@tty1` and `serial-getty@ttyS0` masked), the root account is locked, and SysRq keys are disabled (`kernel.sysrq=0`).
- GRUB is locked: the installer sets a superuser with a random PBKDF2 password that is never stored or shown. The AIOS entry boots without asking (`--unrestricted`), but editing the boot parameters (`init=/bin/bash`, for instance) or opening the GRUB command line is not possible. Recovery entries are disabled and the menu is hidden.
- The password is read with the echo off and passed to the verifier on standard input, so it never appears in process arguments.
- Failed attempts use the same progressive backoff as the portal, shared in the `login_attempts` table, and are recorded in the append-only audit log as `console_denied`. Successful authorisations record the action.
- A disabled account authorises nothing, even with the right password.
- While no administrator exists, the read-only entries, bootstrap regeneration, reboot and power off stay open: there is no credential to check, and the bootstrap code is on that same screen anyway.

The ISO installation environment offers no shell either: its menu holds install, reboot and power off.

This protects against someone reaching a terminal or the VM console. It does **not** protect against someone who can boot the machine from another medium or take the disk out: the disk is not encrypted, so that remains a physical trust boundary. On Proxmox, access to the noVNC console and to the VM storage should therefore be restricted to hypervisor administrators.

## One identity for the portal and the chat

Open WebUI has no credential store of its own: AIOS is the login authority. The mechanism is trusted-header authentication, and its security rests on three conditions, all verified in the shipped configuration.

- **The headers cannot be forged by the client.** On `location /`, NGINX sets `X-AIOS-Email`, `X-AIOS-Name` and `X-AIOS-Role` with `proxy_set_header`, using values from the authentication subrequest. `proxy_set_header` replaces whatever the browser sent; when the value is empty the header is not forwarded at all, so a missing session never becomes an arbitrary identity.
- **Open WebUI cannot be reached around NGINX.** It listens on loopback only, so there is no network path that would deliver unfiltered headers.
- **The session check does not lock the portal out.** `/_aios/chat-session` is declared `internal` and cannot be called directly; `/admin/` and `/api/v1/aios/` are more specific locations than `/` and stay outside the check, otherwise signing in would be impossible.

The SUPERADMIN role becomes a chat administrator, every other role a normal user. Disabling an account or changing its role deletes the AIOS sessions, and Open WebUI is reconciled within a minute of the next session. Users without an email address get an internal identity in the reserved `users.aios.invalid` domain, which the portal refuses as a username precisely to prevent collisions. Email addresses are compared case-insensitively: a second account differing only in case is refused.

Backups contain both the AIOS accounts and the chat data.

## Recovery SSH (optional)

The installer asks whether to enable SSH. The default is **yes**, because it is the only remote way to recover the first administrator's code or the API key; answering no leaves the package installed but inert. On this Ubuntu release sshd is socket-activated, so disabling only the service would still leave port 22 listening: **both** units, `ssh.service` and `ssh.socket`, are disabled, and `/etc/ssh/sshd_not_to_be_run` — the switch OpenSSH itself honours in both — is present. There are no host keys and no account can authenticate.

When it is enabled, the installer creates a dedicated account with a single purpose: reading or regenerating the one-time code that creates the first administrator, and showing the API key. In practice:

- The account's login shell **is not a shell**: it ignores any command sent by the client, so `ssh host <command>` runs nothing. It gives no access to the filesystem or the logs.
- `sudo` is granted for **exactly three invocations** and nothing else: `aios-bootstrap-recovery show`, `reset` and `apikey`. The last one shows the inference key only after verifying the username and password of an AIOS administrator, with a backoff dedicated to the SSH channel and an audit record. The rule contains no `ALL` and cannot be widened without editing `/etc/sudoers.d/aios-recovery`.
- The bootstrap commands refuse to work as soon as an administrative account exists. From then on the SSH account exposes nothing but the API key, and only with an administrator password.
- `PermitRootLogin no`, `AllowUsers` limited to that account, TCP/agent/X11 forwarding and tunnels disabled, `MaxAuthTries 3`.
- With a public key, password authentication is switched off (`PasswordAuthentication no`) and the account is locked with `passwd -l`. With a password, the minimum length is 12 characters.
- The password is never stored: it appears neither in `/etc/aios/install-settings.json` nor anywhere else in clear text.
- Host keys are never cloned from the image: they are generated on first boot, on the machine that will use them.

Enabling SSH still adds network surface the appliance would not otherwise have. Where the physical or noVNC console is reachable, that remains the path with less exposure.
