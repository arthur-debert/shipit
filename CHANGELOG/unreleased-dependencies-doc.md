- docs: `docs/dependencies.md` is now the single, authoritative statement of the
  cross-repo dependency model (a plain conda package: channel, name,
  arch-dependent-or-universal, producer publishes, consumer pins). It declares the
  2026-07-20 reset and names the retired mechanisms explicitly, so a reader can tell
  current guidance from stale guidance without archaeology.
