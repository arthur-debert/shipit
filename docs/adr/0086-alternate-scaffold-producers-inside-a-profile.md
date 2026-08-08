# Alternate scaffold producers live inside a Creation profile

`repo new --standout-wizard` runs the released `standout new-project` wizard and
uses its Cargo workspace as the new Repo's application tree. It is an alternate
**scaffold producer** inside the closed `rust` Creation profile, not a new
`--stack` value and not a template or plugin mechanism: the flag is accepted only
alongside `--stack rust`, the registry stays the closed set ADR-0063 fixes, and
the default producer — shipit's own hello-world workspace — is unchanged. Shipit
copies no Standout templates, so wizard improvements stay owned by Standout.

The planner stays pure and ADR-0057 holds. The wizard runs as an effect at the
same level as the author reader, in a scratch directory of its own, before
staging exists; its generated tree is then read back into ordinary owned files
and the profile contributes them exactly as it contributes its own six. Conflict
detection, plan composition, and plan writing never learn that a wizard ran. The
Artifact is derived from the one generated workspace member that builds a binary,
by its manifest's `package.name` — never from the Repo name, which the wizard's
separate executable-name answer may differ from — and zero or several such
members refuse.

Shipit keeps the whole surrounding lifecycle: the destination and Repo identity
follow the positional `NAME`, the generated directory's *contents* are imported
at the staging root so no nested `NAME/NAME` survives, and managed installation,
provisioning, the three staged Checks, the single initial commit, and the atomic
rename are unchanged. The wizard has no destination or name injection, so a
project-name answer that disagrees with `NAME` refuses rather than creating two
identities. Cancellation, a nonzero exit, a missing executable, unsafe or
undecodable generated entries, and ambiguous manifests are all creation failures
that leave the destination absent and remove every scratch directory. The wizard
exits 0 when it is cancelled, so producing nothing — not the exit code — is what
reads as cancelled.

The wizard is the **single** interactive exception to ADR-0062's non-interactive
boundary. It gets a new Exec-seam function that inherits the caller's stdin,
stdout, and stderr, captures nothing, and returns only an rc; the certification
that still decides publication runs through `pixi run` exactly as before. This is
the only place shipit hands the terminal to a child, and ADR-0028 still holds:
that child goes through the one Exec seam and is recorded like every other.
