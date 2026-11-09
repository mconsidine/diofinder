# SynScan documents: what they settle for the TeleTrack link (2026-11-09)

**Status: assessment notes only, no code changed.**

These documents were read after the session's network was widened to the
Sky-Watcher hosts. They are referenced by URL only and not copied into the
repository.

| Document | URL |
|---|---|
| SynScan V3 hand-control manual (SSHCV3-F-141031V1) | https://inter-static.skywatcher.com/upfiles/en_download_caty01415653149.pdf |
| SynScan V4 hand-control manual (SSHCV4-F-161208V1) | https://inter-static.skywatcher.com/downloads/Synscan_V4_Hand_Control_Manual_SSHCV4-F-161208V1-EN.pdf |
| SynScan Serial Communication Protocol v3.3 (SSAP-F-160414V1) | https://inter-static.skywatcher.com/downloads/synscanserialcommunicationprotocol_version33.pdf |
| Sky-Watcher Motor Controller Command Set (identical to the copy uploaded earlier) | https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf |
| SynScan App protocol 2025-09-30 (ASCOM-style text protocol to the phone app; not relevant to the bus) | https://inter-static.skywatcher.com/downloads/synscan_app_protocol_20250930.pdf |
| Index page | https://www.skywatcher.com/download/manual/application-development/ |

**Not readable from the sandbox:**
- `www.astroshop.eu` (HTTP 403);
- Stargazers Lounge (HTTP 403 to automated clients).

## 1. Hand-controller port pinouts (V3 and V4 manuals, Appendix III, identical)

**"Telescope Mount Port", 8-pin RJ-45 on the hand controller:**

| Pin | Signal |
|---|---|
| 1 | GND |
| 2 | — |
| 3 | DROP |
| 4 | — |
| 5 | RX (TTL) |
| 6 | TX (TTL) |
| 7 | — |
| 8 | Vpp+ |

**"Multi-Purpose Port", 6-pin RJ-12 on the hand controller:**

| Pin | Signal |
|---|---|
| 1 | SHUTTER (optoisolator) |
| 2 | TX (RS-232C) |
| 3 | GND |
| 4 | COMMON (optoisolator) |
| 5 | RX (RS-232C) |
| 6 | Vpp+ |

- **Vpp+ is the supply rail, not a programming voltage.** The Power Port's
  pin 1 is also "Vpp+". The multi-purpose port's power output is "Power
  Supply Voltage − 0.7 V, maximum 100 mA". This corrects the earlier guess
  that Vpp+ hinted at a programming function.
- **Mount-side "Hand Control" port by model** (manual §1.2):
  - EQ6 Pro: D-sub 9;
  - HEQ5/EQ5/EQ3 Pro, AZ-EQ6, EQ8: 8-pin RJ-45;
  - **"All Alt-azimuth mounts": 6-pin RJ-12.**

**Consequences for the TeleTrack:**
- The hand controller's mount-side signal set is exactly **GND, DROP,
  TX(TTL), RX(TTL), supply**. The "AUX INTERFACE" schematic's 6-pin jack
  carries the same five signals plus PDL:
  1 DROP, 2 GND, 3 TX, 4 +12 V, 5 RX, 6 PDL.
- The TeleTrack is alt-az, so its `HC` jack should be the 6-pin RJ-12 type.
  The AUX jack very likely uses the same 6-pin pinout as the AUX schematic.
- The meter checks in the bring-up checklist still apply.
- The multi-purpose port pinout agrees with Orion's 9481 Appendix B (RJ-12
  pin 2 = the hand controller's RS-232 transmit, 3 = GND, 5 = its receive).

## 2. Hand-controller protocol facts that bear on "through the HC vs direct"

From the SynScan Serial Communication Protocol v3.3:

- **Firmware needed for variable-rate slews:** `P` & chr(3) & chr(16|17) &
  chr(6|7) …, rate in ¼″/s. Needs hand-controller firmware **3.03 or later
  (V3) / 4.01.01 or later (V4)**. The whole document assumes V3 3.38+ or
  V4 4.38.06+. A 2008-era Orion TeleTrack AZ-G handset may predate these.
- **The motor firmware can be read through the hand controller**, without PC
  Direct: "Get Device Version" `P` & chr(1) & chr(16|17) & chr(254) & chr(0)
  & chr(0) & chr(0) & chr(2) returns chr(major) chr(minor) `#`.
- **"Get Model" `m`:**
  - 128–143 = AZ GOTO series (matches motor-board mount codes 0x80–0x8F);
  - 144–159 = Dob;
  - 160 = AllView.
- **Worst-case latency (Developer Notes):** *"Software drivers should be
  prepared to wait up to 5.0 s (worst case scenario) for a hand control
  response"* for commands that make the hand controller talk to another
  device, such as Get Position. That is a published, documented reason to
  keep the hand controller out of a 5–10 Hz tracking loop.
- **PC Direct mode** is "mainly used to update the motor controller's
  firmware" (V3 §9.2, V4 §9.2). Exit by holding ESC for more than 1 s.

## 3. Forum pinout claims (unverified, not used)

IceInSpace thread t-169962 (2018) quotes a Stargazers Lounge post giving an
"original SynScan cable" RJ-12 ↔ RJ-45 mapping:
- RJ-12 1 unused;
- 2↔6, 3↔7, 4↔5, 5↔4, 6↔2.

It doesn't connect RJ-45 pins 1 or 8, which the manual gives as GND and
supply, so as quoted it can't be the whole cable. A later poster who built
it reported "no response both axes". **Not used.** The manual's pin table
and the AUX schematic are the references.

## 4. Still open

- **PDL:** the function of AUX pin 6 is still undocumented.
- **The source of the "AUX INTERFACE" schematic** (NXW210 / 51562-83): no
  public match.
- **The TeleTrack's own jack:** pins 2 and 4 to be confirmed with a meter,
  as in the bring-up checklist.
