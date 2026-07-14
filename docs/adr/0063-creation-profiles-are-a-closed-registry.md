# Creation profiles are a closed registry

Creation profiles are a closed, shipit-owned registry selected by known
`repo new --stack` keys. Adding Rust's future peers is a reviewed shipit change
with packaged resources and fixtures; v1 does not discover external profile
directories, remote templates, plugins, or arbitrary template paths. This
mirrors the Toolchain registry, keeps generation deterministic, and avoids
making untrusted template execution and profile compatibility part of the
public interface.
