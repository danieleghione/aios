# Backup, restore and component releases

**System → Backup** creates an archive with the `/etc/aios` configuration, the database (through the SQLite backup API), users, session metadata, the registry and the Open WebUI data. The chat is stopped during the copy so its own database stays consistent. GGUF weights are excluded unless you ask for them. The control plane stays available throughout. Archives are `0600`, reachable through the API only by a SUPERADMIN, and they contain secrets.

To restore, upload the archive and confirm the operation. The broker extracts it into a staging area with limits, refuses traversal, links and device nodes, and verifies the SQLite integrity and schema. It stops the services that write data, keeps a pre-restore copy in `backups/pre-restore-TIMESTAMP`, replaces configuration, database and chat data, then restarts. Sessions are revoked. If the GGUF files are missing, those models become DISABLED and unpublished: install them again or restore the matching weights. The archive does not replace the operating system or files outside the paths it owns.

Do not power off during a restore. If the process is interrupted, use the physical console and the pre-restore copy to put the database, configuration and chat directories back with the services stopped, fix ownership (`aios` for database and configuration, `aios-webui` for the chat) and start the services again. Always keep a copy somewhere else: backups and models on the same disk do not survive that disk failing.

## Component releases

Component releases use a canonical JSON manifest (sorted keys, `,` and `:` separators) with `component`, `version` and `sha256`, signed with Ed25519. The accepted components are `application`, `runtime` and `open-webui`, and the archive carries the contents of the matching directory under `/opt/aios`. The system stops the affected services, keeps a `.previous` copy, applies the release and checks the services; a failure restores the previous directory. An update must match the same Python/OS ABI: a virtualenv built for another path or distribution will not work.

`scripts/sign-release.py` creates a signed manifest. Keep the private key off the appliance and install only the trusted public key, from the console, at `/etc/aios-release.pub`. The build info and the image checksum can be part of a release signed by whoever distributes it.

Operating system updates: the appliance checks the Ubuntu archive and installs security and package updates from the console menu. Platform upgrades (a new AIOS image) are done by reinstalling from a verified image and restoring a backup; there is no A/B layout. Keep the old disk, or a verified clone, to roll the platform back. Model updates are separate installations or revisions, and never automatic.
