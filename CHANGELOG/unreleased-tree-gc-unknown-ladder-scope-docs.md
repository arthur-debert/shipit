- **Docs:** the write-ladder's "an UNKNOWN PR state is never removable" rule is
  now documented as scoped to the write ladder specifically, rather than reading
  as a fleet-wide invariant (#1011). The clarification keeps the gc ladder's
  conservative keep-on-UNKNOWN behaviour legible next to the batched PR read that
  makes UNKNOWN rare, so a future reader does not mistake a defensive default for
  a hard guarantee that spans every Tree kind.
