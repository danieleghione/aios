# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub's **Report a vulnerability** button on the repository's *Security* tab. Do not open a public issue.

Include the AIOS version (shown at the bottom of every portal page), what an attacker needs (network position, account role), and the steps to reproduce. A fix is released as a new ISO and noted in the changelog, with credit if you wish.

## Supported versions

Only the latest release receives fixes. Operating system packages are updated from the portal (**System → Updates**) or the console without a new release.

## Scope

In scope: the installer, the console and recovery SSH, the control plane and its API, the platform broker, the admin portal, the gateway in front of the inference engines, and the way AIOS configures Open WebUI, nginx and the system.

Out of scope: vulnerabilities in upstream projects that AIOS does not change (please report them upstream; tell us if AIOS makes them reachable), attacks that need an ADMIN or SUPERADMIN account to do what the role allows, and denial of service by sending the appliance more inference work than its hardware can do.

The security model is described in [docs/security.md](docs/security.md).
