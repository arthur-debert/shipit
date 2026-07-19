- lint: `shipit provision lexd` is retired — `lexd` now rides the public
  Artifact channel as an ordinary conda dependency, resolved through `pixi.lock`
  and integrity-checked by pixi's sha256 (ARF02-WS06, ADR-0066/0071; #1005). The
  bespoke fetcher (`src/shipit/provision/`, its trust-on-first-use SHAs, the
  `provision lexd` verb, and the `provision-lexd` pixi task) is deleted with no
  fallback. Fleet uniformity moves from a compiled binary constant to a
  shipit-managed, consumer-non-editable `[feature.shipit-lexd]` pixi block
  (channel + `lexd = "==0.19.10"`) that `shipit install` wires into every managed
  repo's lint env (ADR-0047), so a consumer cannot drift its `lexd` version. The
  orphaned `curl` lint dependency (only ever the fetcher's downloader) is dropped.
  Windows (`win-64`) is unserved under the build pause (#895) and now fails closed
  on a lint solve — deliberately, with no `provision` fallback.
