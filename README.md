# ripe-atlas-data-quality-akuma

Assessing the data quality of RIPE Atlas 👹

This repo contains scripts/outputs for some data quality metrics we propose for RIPE
Atlas. We intend to run these periodically so we can see if/how data quality improves
over time.

## Anchor Measurements

We track the following types of anchor measurements (and anchors) that require
additional investigation:

- **Failing measurements to connected anchors:** Indicates a potential misconfiguration
  of the measurement (e.g., targeting an old IP) or the anchor (e.g., no IPv6).
- **Failing measurements to nonconnected anchors:** This is expected, but since these
  are ongoing measurements they should probably be stopped.
- **Succeeding measurements to nonconnected anchors:** This can indicate probes that
  hallucinate results or anchors that do not communicate with the Atlas backend even
  though they are online.
- **Measurements to unknown anchors:** These are anchors without an entry in the
  /anchors endpoint, which should never happen.
- **Disconnected anchors:** A simple list of disconnected anchors sorted by
  disconnection time. Anchors that are disconnected for a long time should be
  decommissioned.
