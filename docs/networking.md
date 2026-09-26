# Network

The initial Ethernet DHCP configuration is handled by netplan and systemd-networkd, and matches interfaces named `e*`, including `eth0`, `enp1s0` and `ens3`. The console shows the addresses in use. Wi-Fi is not part of the initial target.

**System → Network** shows the connection in use now: interface, DHCP or static, address, gateway and DNS. The form below starts from those values; choose DHCP or a CIDR address with gateway and DNS. The API validates names and addresses. Before applying anything it saves the previous configuration and a 120-second deadline in `/var/lib/aios-network-rollback.json`.

After the change, reopen the portal on the new address: **System → Network** shows the pending change with *Keep the new settings*; press it before the deadline. Without that confirmation the broker restores the previous file and runs `netplan apply`, including after a reboot once the deadline has passed. The backend keeps listening on the same loopback address; only LAN reachability changes.

Hostname, time zone, NTP and the HTTP/HTTPS proxy are configured separately, under **System → Configuration**, which shows the values the system is using now; the time zone is chosen from the zones the system knows. The proxy does not accept inline credentials in the URL, so no secret ends up in the journal or in the settings. Internal repositories need `allow_private=true`; loopback and link-local addresses are never accepted as sources, not even through a proxy.

The firewall stays default-drop for incoming traffic, with HTTP/HTTPS allowed, plus port 22 when recovery SSH was enabled during installation.
