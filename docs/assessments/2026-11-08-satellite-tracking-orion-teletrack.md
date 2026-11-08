# Satellite tracking: Orion TeleTrack — assessment (2026-11-08)

**Status: assessment only, nothing built.** Read
[`2026-11-08-satellite-tracking-common.md`](2026-11-08-satellite-tracking-common.md)
and
[`2026-11-08-satellite-tracking-synscan.md`](2026-11-08-satellite-tracking-synscan.md)
first.

## 1. Bottom line

"TeleTrack" covers two different Orion products, and the answer differs:

| Model | Controller (as far as could be established) | Assessment |
|---|---|---|
| **9481 TeleTrack GoTo** | SynScan-type GoTo hand controller (Orion-branded, Synta-made). Orion lists a computer interface cable; its manual has an RS-232 appendix | **Same as the SynScan assessment**, with the differences in §2 |
| **9441 TeleTrack** | A "multi-function hand controller" on a motorised alt-az mount; no evidence found of GoTo or a computer port | **Probably not drivable without hardware changes** (§3) |

**Caveat:** the two manuals could not be read from the build sandbox (the manual
sites were not reachable). Everything about them here comes from search
summaries and product listings, and must be confirmed against the manuals and
the label on the mount.

## 2. TeleTrack GoTo (9481): differences from SynScan proper

- **Firmware:** Orion-branded SynScan firmware, possibly older than current
  SkyWatcher releases. Some NexStar-style commands (path A in the SynScan
  document) may be missing. One owner reports updating the hand controller's
  firmware, so an update may be possible. Check the version (`V`) first.
- **Cable and pinout:**
  - the hand controller's smaller modular jack carries RS-232;
  - Orion's computer interface cable (or the V5's USB port, where present) is the
    connection;
  - the finder's MAX3232 wiring needs a matching adapter.
  Verify the pinout before connecting.
- **Path B** (motor-controller protocol) needs the hand controller's PC-direct
  mode. Whether this firmware has it is unknown.
- **Speeds and build:** a tabletop/tripod alt-az mount; top speed unknown.
  Probe it like the Virtuoso.
- **Otherwise identical:** pointing model (levelling sweep from the polar code),
  tracking loop, safety and phases all as in the SynScan document.

## 3. TeleTrack (9441): if there is no computer port

- **No interface, no control.** If the hand controller only offers direction
  buttons and fixed speeds, there is nothing for the finder to talk to.
- **The only routes are hardware:**
  - replace the hand controller with a microcontroller that drives the motor
    board, if the board's protocol is known (some Synta motor boards speak the
    `:` motor protocol);
  - or retrofit the drive with an OnStepX controller, which then makes the
    OnStepX assessment apply.
  Both are hardware projects, outside diofinder's scope.
- **What diofinder can still do:** plan passes and show pointing directions
  (common §3.2) while the user follows by hand. That already works with the
  laptop tools and would work with the planning page.

## 4. To verify before any work

1. The model number on the mount's label (9481 or 9441, or another).
2. The hand controller: model (SynScan V3/V4/V5 or another), firmware version,
   and whether it has a serial jack or USB.
3. For the GoTo model: whether the NexStar-style motion commands and PC-direct
   mode work (the SynScan phase 0 probe applies unchanged).

## 5. Sources

- [Orion TeleTrack GoTo altazimuth mount (product page, 9481)](https://www.telescope.com/Mounts-Tripods/Altazimuth-Mounts-Tripods/Orion-TeleTrack-GoTo-Altazimuth-Telescope-Mount/pc/-1/c/2/sc/35/p/9481.uts)
  (computer interface cable listed).
- [Orion 9481 manual, Appendix B: RS-232 connection](https://www.manualsdir.com/manuals/204941/orion-9481.html?page=13).
- [Orion TeleTrack 9441 manual (manualslib)](https://www.manualslib.com/manual/1266647/Orion-Teletrack-9441.html)
  (not read; host not reachable from the build sandbox).
- [IceInSpace: TeleTrack owner on SynScan hand-controller firmware](https://www.iceinspace.com.au/forum/showthread.php?p=1340137).
- [Orion SynScan GoTo manual](https://manualmachine.com/orion/synscangoto/20715364-instruction-manual)
  (hand-controller serial port description).
- [Stellarmate: SkyWatcher SynScan driver (Orion mounts)](https://stellarmate.com/devices/mounts/orion/251-skywatcher-synscan.html).
