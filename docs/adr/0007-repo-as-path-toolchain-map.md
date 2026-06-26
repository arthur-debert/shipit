# A repo is a path→toolchain map; dispatch per-entry, not a project-Kind enum

The fleet sweep made the obvious model wrong: a repo is not one *Kind*. A Tauri
app is simultaneously a **rust** path (`src-tauri/`), an **npm** path (the
frontend), and often an **mkdocs** path (`docs/`); `lex` is a single rust path
that emits a CLI, an LSP binary, a wasm npm package, and ~10 crates. Branching CI
behavior on a whole-repo project type (`kind = "tauri-app"`) would re-centralize
exactly the per-Kind reusable-workflow proliferation in `arthur-debert/release`
that this work exists to delete, and it fights the data-driven grain everywhere
else in shipit (the **Reviewer adapter** registry, the lint `Lang` set).

## Decision

- A repo is modeled as the `.shipit.toml` **path→toolchain map** (architecture.lex
  §6): each build-bearing path declares its **toolchain** (rust / npm / mkdocs / go
  / wasm / …). shipit composes provisioning / build / test / lint by *walking the
  map*, per entry — it never reads or branches on a project-type label.
- **Toolchain** is the dispatch axis and a closed registry, the same shape as the
  lint `Lang` set: adding one is adding an entry; nothing downstream changes.
- The hard-20% variation that genuinely differs (a Tauri three-file version bump,
  crates.io vs `vsce` publishing) lives in **endpoint adapters** and declared
  **bundle** / version-sync rules keyed off the map — never in a `kind` switch.
- "Kind" survives only as informal human shorthand for a recognizable composition
  ("a tauri Kind"), never a code dispatch label.

### Alternatives rejected

- **A closed `kind` enum shipit branches on** — the intuitive model; recreates the
  per-Kind branching of the release repo, makes a new project shape a core change,
  and breaks down immediately on multi-toolchain repos (a Tauri app is three Kinds
  at once).
- **One toolchain → one artifact (1:1)** — false in both directions: one rust
  workspace yields many **artifacts**, and one Tauri app composes several
  toolchains. Artifacts are declared separately (ADR-0008's content-key keys them).

## Consequences

- `.shipit.toml` grows the path→toolchain map and a declared **artifact** list
  (each naming its producing toolchain build target(s), optional **bundle** step,
  and **distribution endpoint(s)**); `pixi.toml` still owns provisioning.
- The generic CI workflow stays generic because per-toolchain difference hides
  behind uniform task names (`pixi run build|test|bundle`), per architecture.lex
  §5/§7 — extended here from lint to all of CI.
- New distribution targets arrive as endpoint adapters in a registry, mirroring how
  new reviewers arrive as **Reviewer adapter**s.
