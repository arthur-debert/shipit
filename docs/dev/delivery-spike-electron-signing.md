# Delivery spike: electron packaging + signing from Linux (#1197)

Deliverability spike #1 from `docs/design/delivery-system.md` (PR #1196).
Question under test: can lexed's mac artifact — including its QuickLook
`.appex`, the fleet's hardest signing case (nested bundle, sandbox
entitlements) — be packaged, signed, dmg-assembled, notarized, and stapled
**from a Linux container**, leaving macOS only the appex *build* leg and the
final real-mac *verification*?

**Overall: PASS.** Every pipeline step ran end-to-end with real artifacts and
the real Apple notary service. Two assembly-detail corrections were needed
(recorded below); neither disturbs the design's tool adoption.

Runnable scripts: `docker/spike-electron-signing/` (see its README for the
step map). Test article: fresh clone of `lex-fmt/lexed` (electron 41,
`quicklook/LexQuickLook.xcodeproj`). Host: darwin-arm64 + Docker running
linux/aarch64 containers — CI would use x86_64 or arm64 Linux runners;
rcodesign, nfpm, and xorriso are arch-agnostic, and `@electron/packager`
cross-packages any target arch from any host. Signing used local credentials
(coordinator-provided); no credential value appears in the scripts or in any
artifact.

## Per-step results

| # | step | where | result | wall time |
| --- | --- | --- | --- | --- |
| 1 | appex build, `xcodebuild`, `CODE_SIGNING_ALLOWED=NO` | host (Darwin) | PASS | ~40s |
| 2 | vite build + `@electron/packager` darwin-arm64 `.app` | Linux container | PASS | ~3m (cold npm ci + electron download) |
| 3 | input placement: appex → `Contents/PlugIns/`, extraResources → `Contents/Resources/` | Linux container | PASS | seconds |
| 4 | `rcodesign` scoped bottom-up signing (p12, never `--deep`) | Linux container | PASS — `codesign --verify --deep --strict` clean on host | ~9s for a 321MB app |
| 5a | dmg assembly (xorrisofs → libdmg-hfsplus `dmg`) + `rcodesign` dmg sign | Linux container | PASS (after dropping `-hfsplus`, see gotcha 1) | ~15s |
| 5b | notarize (`rcodesign notary-submit`, App Store Connect API) + staple | Linux container | PASS — "Accepted / Ready for distribution", ticket stapled into the dmg | 71s total (47s notary poll) for a 127MB dmg |
| 6 | nfpm deb + symlink-preserving zip of the same build | Linux container | PASS — `lexed_0.11.3_arm64.deb` + `LexEd-mac-arm64.zip` | ~47s |
| 7 | Gatekeeper install assess + staple validate + launch + QuickLook function | host (real mac) | PASS with one manual residual (see transcript) | — |

## The signing invocations that worked

Bottom-up, scoped, no `--deep` (rcodesign has none; it signs nested bundles
correctly by default). Two invocations:

```sh
# 1. appex first, with ITS OWN sandbox entitlements
rcodesign sign --p12-file cert.p12 --p12-password-file pw --for-notarization \
  --entitlements-xml-file quicklook/LexQuickLook/LexQuickLook.entitlements \
  LexEd.app/Contents/PlugIns/LexQuickLook.appex

# 2. outer app; rcodesign walks helpers/frameworks itself, bottom-up.
#    Scoped --entitlements-xml-file ('<relpath>:<file>') puts the electron
#    JIT entitlements on the main binary and each helper app;
#    --exclude preserves the appex signature from step 1.
rcodesign sign --p12-file cert.p12 --p12-password-file pw --for-notarization \
  --entitlements-xml-file resources/entitlements.mac.plist \
  --entitlements-xml-file 'Contents/Frameworks/LexEd Helper.app:resources/entitlements.mac.plist' \
  ... (one per helper) ... \
  --exclude 'Contents/PlugIns/LexQuickLook.appex' \
  LexEd.app
```

`--for-notarization` = Developer ID required + timestamps + hardened runtime
on every Mach-O — exactly the notarization preconditions, no per-file flag
bookkeeping.

## Gotchas (the transferable findings)

1. **xorrisofs `-hfsplus` is a trap.** libisofs' HFS+ writer stamps a
   `com.apple.FinderInfo` xattr (`????` creator/type) on every file;
   `codesign --verify --strict` rejects that as "detritus" on every Mach-O in
   the bundle. Plain ISO9660 + Rock Ridge (`-r`) — current bitcoin-core
   practice — preserves symlinks (Electron Framework's `Versions/Current`
   chain mounts intact), carries no xattrs, and macOS mounts it read-only via
   cd9660. libdmg-hfsplus' `dmg` then wraps it into a real UDIF/UDZO that
   rcodesign signs and the notary service accepts.
2. **rcodesign `--exclude` takes the bundle path, not a glob into it.**
   `--exclude 'Contents/PlugIns/Foo.appex/**'` excludes the appex's *contents*
   but still re-signs the appex bundle itself (rcodesign preserved the
   existing entitlements, so it happened to stay correct — but by accident).
   `--exclude 'Contents/PlugIns/Foo.appex'` skips it: "bundle is in exclusion
   list; it will be copied instead of signed".
3. **OpenSSL 3 default p12 export is unreadable by rcodesign 0.29** ("incorrect
   password given when decrypting PFX data") and rcodesign refuses empty p12
   password files outright. Re-export with `openssl pkcs12 -export -legacy`.
4. **`codesign` re-sign inside the outer pass preserves entitlements** (see 2)
   — do not rely on it; scope explicitly or exclude.
5. **The appex Darwin leg needs no signing identity**: `xcodebuild ...
   CODE_SIGNING_ALLOWED=NO` produces an unsigned appex that rcodesign signs
   later from Linux. The mac exception shrinks to *build-only* (no keychain,
   no cert on the runner).
6. **electron-builder parity is placement, not tooling.** The packaged app is
   `dist/ + dist-electron/ + package.json` (main-process deps are bundled by
   vite-plugin-electron, so no `node_modules` ships) plus extraResources and
   the appex — all plain file placement after `@electron/packager`, which is
   precisely the design's "packaging-stage input placement" contract. The
   host app's Info.plist must carry the exported UTIs
   (`UTExportedTypeDeclarations`) or the QuickLook appex never binds to
   `.lex` files; `@electron/packager`'s `extendInfo` covers it.

## Design consequences (for the coordinator follow-up; not applied here)

- Electron packaging row: `@electron/packager` from Linux **settles** — the
  .app assembled in a Linux container passed `codesign --verify --deep
  --strict`, Gatekeeper, and notarization untouched.
- ADR-0084 mac exception: the CI-signing half can be dropped — lexed keeps
  only the appex Darwin *build* leg (`CODE_SIGNING_ALLOWED=NO`, no identity,
  no keychain on the runner).
- rcodesign notarization from Linux needs only the ASC API key — no
  app-specific password, no keychain, no Xcode.

## Verification transcript (step 7, real mac)

All programmatic checks passed on the spawn host (darwin-arm64, macOS 26.5):

- `spctl --assess -vv --type install LexEd.dmg` → **accepted,
  source=Notarized Developer ID**.
- `xcrun stapler validate LexEd.dmg` → "The validate action worked!".
- Installed to `/Applications` from the mounted dmg;
  `spctl --assess --type execute` → accepted;
  `codesign --verify --deep --strict` → valid on disk, satisfies its
  Designated Requirement (appex, all four helpers, all frameworks validated).
- App launches and runs (main + GPU + network helper processes up) — the
  Linux-packaged app is a functioning LexEd.
- QuickLook appex: registered and elected in `pluginkit`
  (`+ com.lex.lexed.quicklook(1.0)` at
  `/Applications/LexEd.app/Contents/PlugIns/LexQuickLook.appex`), and `.lex`
  files resolve to `com.lex.document` (`mdls`), which only the appex claims.

One residual needs a human at an unlocked screen: the *visual* `qlmanage -p`
render. This Run executed headless with the display locked — `qlmanage -p`
cannot present its panel there (`screencapture`: "could not create image from
display"; the QuickLook server handled 0 requests), and the headless variant
`qlmanage -p -o dir` crashes with an NSException in this macOS build (OS bug,
not pipeline). Nothing signing-related is in doubt: Gatekeeper, strict
codesign, notarization, and LaunchServices/pluginkit registration all accept
every bundle. Residual command: `qlmanage -p <file>.lex` at the desktop.

Unrelated to packaging/signing but observed while probing: the appex renders
by shelling out to `Contents/Resources/lexd-lsp convert --to png`, and the
shipped lexd-lsp v0.17.0 answers "Format 'png' not found" — so the preview
*content* would currently fail in any packaging of this lexed build. A lexed
runtime matter; worth a lexed-side issue.
