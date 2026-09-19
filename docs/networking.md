# Network

The initial Ethernet DHCP configuration is handled by netplan and systemd-networkd, and matches interfaces named `e*`, including `eth0`, `enp1s0` and `ens3`. The console shows the addresses in use. Wi-Fi is not part of the initial target.

Under **System → Network** choose the interface name shown on the Hardware page, then DHCP or a CIDR address with gateway and DNS. The API validates names and addresses. Before applying anything it saves the previous configuration and a 120-second deadline in `/var/lib/aios-network-rollback.json`.

After the change, reach the appliance on its new address and confirm the operation ID before the deadline. Without that confirmation the broker restores the previous file and runs `netplan apply`, including after a reboot once the deadline has passed. The backend keeps listening on the same loopback address; only LAN reachability changes.

Hostname, time zone, NTP and the HTTP/HTTPS proxy are configured separately. The proxy does not accept inline credentials in the URL, so no secret ends up in the journal or in the settings. Internal repositories need `allow_private=true`; loopback and link-local addresses are never accepted as sources, not even through a proxy.

The firewall stays default-drop for incoming traffic, with HTTP/HTTPS allowed, plus port 22 when recovery SSH was enabled during installation.
