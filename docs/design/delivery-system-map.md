# The Delivery System — the flow

The one-page view of [`delivery-system.md`](delivery-system.md), organized as the
logical flow of a release: what happens, in order. One-liner of scope per step; a
tool line where a lower-level tool is adopted. The detail doc wins on any question.

## The release flow

1. **Prep** — assemble the release data; no artifacts touched yet.
   - **Changelog** — compile `CHANGELOG/` fragments into the changelog; an empty
     release is refused.
   - **Version** — determine the version and tag; tag-authoritative, supplied not
     computed.
   - **Release commit** — commit what prep changed (changelog, version bumps) —
     the reason releases are workflow-triggered, never tag-triggered.
2. **Build** — one arch-bound build per leg of the matrix.
   - **Matrix derivation** — legs computed from the artifacts' declared platforms;
     the build system itself is matrix-free.
   - **Input resolution** — every `requires` key placed at its dest (intra-repo
     output or inter-repo pinned fetch); the resolver owns the read credential.
     - tool: `gh release download` (+ `gh attestation verify`)
   - **Component builds** — per-kind builders, target platform as an explicit
     argument, exit-code contract.
   - **Output verification** — promised paths must exist; a builder that lies fails
     the leg on the spot.
3. **Packaging** — each artifact fans out to its N formats (each with a platform
   selector; identity packaging for direct-deploy outputs); packagers package,
   never build. Signing steps are woven in at format-defined points, owned by the
   Signing subsystem.
   - **linux + windows formats** (deb/rpm/apk/msix/…)
     - tool: nfpm
   - **electron** — `.app`/exe from the prebuilt app dir, then dmg/zip.
     - tool: @electron/packager + dmg/zip leaf tools
   - **tauri**
     - tool: `tauri bundle` (bundle-only flow)
   - **archives** (tarball/zip) — built in.
   - **Signing** — the Signing subsystem owns policy and credentials; format-defined
     execution (bottom-up over the bundle, then dmg assembly, then notarize +
     staple; msix delegated through nfpm with an injected cert). Packagers never
     own signing decisions or credentials.
     - tool: rcodesign (from Linux; verified hands-on — spike 1, PR #1199)
4. **Barrier** — all-or-nothing: every leg built, packaged, and signed, or nothing
   publishes. The only clean abort point.
5. **Release** — the spine event: GH Release matching the tag, compiled changelog,
   standardized-named assets (`<name>-<version>-<platform>-<arch>`).
6. **Distribution** — fan-out to endpoints, each adapter owning its publish auth;
   ordered, fail-fast, idempotent-resumable.
   - **Push jobs** — `needs:`-gated in the release workflow (App/PAT token — the
     default token suppresses downstream events).
   - **Reconcile sweep** — periodic per-target backstop; the replay that events
     don't have.
   - endpoints: brew, crates.io, npm, pypi, deb repos, stores, firebase, …

## Standing systems (not stages — the flow runs on top of these)

- **Substrate** — everything above executes in a container; code mounts from the
  host; layered shipit-owned images; the Mac exception (local GUI launch and the
  xcode build legs — Darwin signing left it when the signing spike passed,
  ADR-0084 amended 2026-07-31) is the only native carve-out.
- **Consumer surface** — the managed 4-verb Task veneer (+ consumer-owned local
  include) delegating to `shipit <verb>`; same entry points locally and in CI.
  - tool: Taskfile (go-task)
- **Declarations** — components / artifacts / requires / artifact-deps in
  `.shipit.toml`: the source of truth every stage above reads.
- **Consumption** — the downstream half: `[artifact-deps] {version}` + `requires`
  placement; version binds identity, the GH digest verifies transport.
- **Fleet validation** — declaration symmetry across the Portfolio: keys resolve,
  platform coverage holds, orphans and drift surface before any release fires.
