# Creation profiles contribute to one structured plan

Each selected Creation profile returns structured contributions—owned files,
Toolchain declarations, Artifacts, and configuration requirements—to one central
repository-creation planner. The planner detects conflicting claims and renders
shared configuration once; profiles do not apply ordered filesystem overlays or
independently splice shared manifests. This makes multi-stack composition
explicit and deterministic instead of turning profile order and overwrite
behavior into a hidden compatibility contract.
