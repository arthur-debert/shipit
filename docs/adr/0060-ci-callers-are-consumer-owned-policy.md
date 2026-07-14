# CI callers are consumer-owned policy

`shipit repo new` seeds a thin generic GitHub Actions caller as consumer-owned
Repo policy; `shipit install` does not reconcile that caller as a managed unit.
Shipit owns and versions the reusable workflow implementation, while each Repo
owns its triggers, permissions, concurrency, secret forwarding, and required-
Check presentation. This keeps reusable CI logic centralized without turning
legitimate per-Repo policy changes into managed-unit overrides.
