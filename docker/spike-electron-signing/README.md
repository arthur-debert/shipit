# Spike: electron packaging + signing pipeline from Linux (issue #1197)

Runnable scripts for deliverability spike #1 of `docs/design/delivery-system.md`:
prove that lexed's mac artifact — including its QuickLook `.appex`, the fleet's
hardest signing case — can be packaged, signed, dmg'd, notarized, and stapled
from a Linux container, with only the appex *build* (xcodebuild) and the final
*verification* on macOS.

Findings report: `docs/dev/delivery-spike-electron-signing.md`.

## Pipeline

| step | where | script |
| --- | --- | --- |
| 1. build appex (unsigned) | host (Darwin) | `10-build-appex.sh <lexed> <out>` |
| 2+3. vite build, `@electron/packager` .app, place appex + resources | Linux container | `20-package-mac.sh` |
| 4. rcodesign scoped bottom-up signing (never `--deep`) | Linux container | `30-sign-mac.sh` |
| 5a. dmg assemble (xorrisofs → libdmg-hfsplus) + sign | Linux container | `40-dmg.sh` |
| 5b. notarize (ASC API) + staple | Linux container | `50-notarize.sh` |
| 6. nfpm deb + zip | Linux container | `60-linux-artifacts.sh` |
| 7. Gatekeeper + QuickLook verification | host (real mac) | `70-verify-mac.sh <out> [sample.lex]` |

Container steps run via:

```sh
run-container.sh <lexed-dir> <appex-products-dir> <out-dir> <secrets-dir|-> <script>
```

The secrets dir (mounted read-only at `/work/secrets`, pass `-` for the
unsigned steps) contains local signing credentials (coordinator-provided):
`cert.p12`, `p12-password.txt`, `asc-key.p8`, `asc-key-id.txt`,
`asc-issuer-id.txt`. Credential values never appear in scripts or output.

The test article is a fresh clone of `lex-fmt/lexed` (with the `comms`
submodule initialized — the icon pipeline reads it).
