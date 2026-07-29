"""shipit.prstate — a reviewer-agnostic GitHub PR state engine: a read-only
model of where a PR stands, with reviewer-specific mechanics isolated in
swappable adapters so the core never names a reviewer. stdlib only."""
