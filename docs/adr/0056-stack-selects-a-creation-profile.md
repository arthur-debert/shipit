# Stack selects a Creation profile, not a Repo kind

`shipit repo new` keeps the user-facing, repeatable `--stack` option, while each
selected value resolves internally to a Creation profile that contributes
initial project files and declarations. Creation profiles are inputs to creation
only: the completed Repo persists its path-to-toolchain map and Artifacts, never
a stack, profile, or whole-Repo Kind, preserving ADR-0007's dispatch model while
leaving the command ready for future multi-stack composition.
