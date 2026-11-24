# Access from anywhere – Tailscale or WireGuard

Hesperos DX is meant for your home network. To look at the dashboard, listen live or get the hits while you are
away, **do not open a port on the router** (no port forwarding of 8080 to the Pi). The built-in web server speaks
plain HTTP, and a dashboard reachable from the whole internet would be found and tried by scanners within hours.

Use a VPN instead: your phone or laptop joins your home network (or a private network with the Pi) through an
encrypted tunnel, and the dashboard stays invisible to everyone else. Two ways work well:

| | **Tailscale** | **WireGuard on the router** (e.g. FRITZ!Box) |
|---|---|---|
| Set up on | the Pi and each phone/laptop (app) | the router and each phone/laptop (app) |
| Works without a public IPv4 address (DS-Lite, many cable/fibre/mobile lines) | yes | usually no – the router must be reachable from the internet |
| Account needed | yes (Tailscale account, free plan for personal use) | no (FRITZ!Box: MyFRITZ! address) |
| Reaches | the Pi (and optionally the whole home network) | the whole home network |
| Software on the Pi | `tailscale` (a small service) | nothing |

Either way, **set a password** for the dashboard first (*Settings → Access protection*) and do not list the VPN
address range as "network without login" – then a lost phone with an active VPN still cannot change anything
without the password.

## Tailscale

1. On the Pi (Raspberry Pi OS), install and log in:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

   `tailscale up` prints a link – open it in a browser and log in to your Tailscale account. The Pi then appears in
   the Tailscale admin console under *Machines*.
2. In the admin console, open the Pi's menu and choose **Disable key expiry** – otherwise the Pi drops out of the
   network after some months and has to be logged in again (you would notice only when you are away).
3. Install the Tailscale app on your phone/laptop and log in with the same account.
4. Open the dashboard: `http://<name-of-the-pi>:8080` – the name is the one shown in the admin console (with
   MagicDNS switched on, which is the default), or the Tailscale address from `tailscale ip -4` (`100.x.y.z`).
   The address `hesperosdx.local` / `<station>.local` only works at home, not through the VPN.

The connection is encrypted end to end by Tailscale (it is built on WireGuard), so plain HTTP inside the tailnet is
fine. Tailscale runs as a small background service and is idle when nobody connects.

**`tailscale serve` (HTTPS name inside the tailnet)** works too – the dashboard then has an address like
`https://<pi>.<tailnet>.ts.net`. Since 1.8.44 Hesperos DX recognises requests that come through a proxy on the Pi
itself and asks for the password as for any other device (before, such requests looked like the Pi itself, which
never has to log in).

**`tailscale funnel` – please don't.** Funnel publishes the dashboard on the public internet. Even with the
password, every scanner can then try it. Use the VPN instead.

## WireGuard on the router (FRITZ!Box)

FRITZ!OS 7.50 and later can be a WireGuard server; older models without updates (and the 6490/6590 Cable) cannot.
The router needs a MyFRITZ! address (or another DynDNS name) and must be reachable from the internet – with DS-Lite
(no public IPv4 address) this usually fails; then use Tailscale.

1. FRITZ!Box: *Internet → Permit Access → VPN (WireGuard)* → *Add connection* → *Simplified setup* → connect a
   single device, give it a name (e.g. "Phone").
2. The FRITZ!Box shows a QR code (phones) or a configuration file (laptops). Install the **WireGuard** app, add a
   tunnel from the QR code / file.
3. Switch the tunnel on and open the dashboard with the Pi's address in the home network
   (`http://192.168.x.y:8080` – the address is on *System → Raspberry Pi*). `.local` names usually do not work
   through the tunnel.

Other routers with WireGuard (OpenWrt, pfSense/OPNsense, many others) work the same way. Running a WireGuard server
on the Pi itself (e.g. with PiVPN) is possible too, but then you have to open a UDP port on the router and keep the
server up to date yourself – the router or Tailscale is less work.

## What to expect on the road

- **Live listening** streams 24 kHz mono audio, about 0.4 Mbit/s – roughly 170 MB per hour of mobile data. Spectrum
  and waterfall add a little. Recordings on hit cards are only loaded when you press play.
- The dashboard itself is light; the station map loads map tiles from OpenStreetMap directly (not through the Pi).
- If the connection is slow, Live may stutter while the dashboard stays usable. Notifications (mail/push) work
  without any VPN.

## Checklist

- [ ] Dashboard password set, VPN range **not** listed as "network without login"
- [ ] No port forwarding of 8080 (or 8081, the rescue page) on the router
- [ ] Tailscale: key expiry disabled for the Pi
- [ ] Test once from outside (phone on mobile data, Wi-Fi off) before you rely on it
