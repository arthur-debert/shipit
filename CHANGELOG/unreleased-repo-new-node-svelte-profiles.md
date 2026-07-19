- docs(repo-new): plan two new `shipit repo new` Creation profiles — a **Node**
  profile (TypeScript CLI + local library, one black-box `Hello, world!` test,
  `toolchain="npm"`, riding the existing `npm` dispatch leg and the managed
  `nodejs`/`pnpm` provisioning; chooses **pnpm** as its package manager with an
  exact `packageManager` pin (`pnpm@X.Y.Z` — the concrete version the managed
  pixi environment resolves, not a range), a tracked `pnpm-lock.yaml`, and a
  frozen `pnpm install --frozen-lockfile`) and a distinct **svelte-app** profile
  (SPA-only Vite + Svelte + Tailwind + TypeScript, one smoke test; a single-root
  Vite app whose Artifact declares no package and builds via a bare
  `pnpm run build`). This release accepts exactly **one effective profile** per
  `repo new` (one of `rust`, `node`, `svelte-app`); multi-profile composition
  stays future work. Amends `docs/spec/repo-new.md` (Rust → Rust + Node +
  svelte-app; one-effective-profile constraint restored and extended; Cargo naming
  scoped to the Rust profile; retires the Rust-only Non-Goals; carves svelte-app
  out of the minimal lib+CLI limit) and adds ADR-0077 (Node profile) and ADR-0078
  (frontend scaffolds are distinct profiles, not multi-stack composition or a
  base×flavour matrix). Planning only: the build/install toolchain-seam
  generalization the profiles depend on — registry polymorphism, profile-owned
  naming, pre-check dependency materialization, reconciling the `npm`-named
  dispatch leg to pnpm (including pnpm-aware `--filter` Artifact narrowing, not
  `--workspace`), and making `ArtifactDecl.package` optional — is separate work
  tracked in #1083.
