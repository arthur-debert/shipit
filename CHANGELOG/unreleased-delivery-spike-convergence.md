- docs: post-spike convergence for the Delivery System design — both
  deliverability spikes passed (#1197/PR #1199, #1198/PR #1200), so the
  design's pending-spike rows settle: `@electron/packager` + leaf dmg tools
  and `rcodesign` flip to verified in `docs/design/delivery-system.md`
  (adopted-tools table, Signing and Substrate sections, spike ledger), and
  ADR-0084's Mac exception is amended — CI signing/notarization and
  `.app`/`.dmg` packaging leave the exception (they run in Linux containers);
  only the `xcode`-kind build legs and local GUI launch remain macOS-native.
