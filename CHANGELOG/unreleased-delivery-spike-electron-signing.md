- delivery: deliverability spike #1 executed — electron packaging + signing
  pipeline from Linux, on lexed including its QuickLook `.appex` (#1197). The
  runnable pipeline lives in `docker/spike-electron-signing/` (Darwin
  xcodebuild leg for the appex, then a Linux container for `@electron/packager`
  .app assembly, rcodesign scoped bottom-up signing, xorrisofs+libdmg-hfsplus
  dmg assembly, notarize + staple over the App Store Connect API, nfpm deb and
  zip), findings in `docs/dev/delivery-spike-electron-signing.md`. Additive
  only: nothing wires into shipit's production code paths; the design/ADR-0084
  amendment is a coordinator follow-up after both delivery spikes conclude.
