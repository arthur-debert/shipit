# Repo new consumes the existing Repo contract

`shipit repo new` only generates the consumer-owned files and declarations that
make its output an ordinary working shipit Repo, then invokes and verifies the
existing install, lint, test, build, and CI behavior unchanged. The feature does
not amend Tool commands, workflow semantics, install behavior, or managed units
for existing Repos. Internal modules, registries, and data may be refactored or
extracted when that lets creation reuse existing Rust and Toolchain knowledge
rather than duplicate it, but regression tests must preserve every touched
command's observable behavior and installed output. Creation is a producer of
valid Repo input, not a new execution path for the systems it configures.
