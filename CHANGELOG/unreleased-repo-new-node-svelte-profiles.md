- docs(repo-new): plan two new `shipit repo new` Creation profiles — a **Node**
  profile (TypeScript CLI + local library, one black-box `Hello, world!` test,
  `toolchain="npm"`, riding the existing `npm` dispatch leg and the managed
  `nodejs`/`pnpm` provisioning) and a distinct **svelte-app** profile (SPA-only
  Vite + Svelte + Tailwind + TypeScript, one smoke test). Amends
  `docs/spec/repo-new.md` (Rust → Rust + Node + svelte-app; retires the Rust-only
  Non-Goals; carves svelte-app out of the minimal lib+CLI limit) and adds
  ADR-0077 (Node profile) and ADR-0078 (frontend scaffolds are distinct
  profiles, not multi-stack composition or a base×flavour matrix). Planning only:
  the build/install toolchain-seam generalization the profiles depend on —
  registry polymorphism, profile-owned naming, and pre-check dependency
  materialization — is separate work tracked in #1083.
