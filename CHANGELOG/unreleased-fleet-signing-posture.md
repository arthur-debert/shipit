- fleet: signing is now a DECLARED posture per portfolio repo, and `shipit fleet
  posture` checks it (#901). Every `[project.portfolio]` entry declares
  `signing` — `signed`, `unsigned` (which must also record the decision in
  `signing_reason`), or `not-applicable` — so a repo can no longer be
  accidentally divergent: an entry without a posture is a config error, never an
  inference. The verb reads each repo's Actions secret NAMES (never values) and
  reports how they diverge from the fleet's one set: a `signed` repo carries
  `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD` and the ASC API-key trio and
  nothing else, and an `unsigned` or `not-applicable` repo carries no signing
  secret at all. Two divergences it exists to surface: the pre-homogenization
  `APPLE_CERTIFICATE_P12_BASE64` name (reported with the canonical name that
  replaces it) and notarization via the Apple-ID trio — the sign block keeps
  accepting either trio, but the FLEET notarizes with the machine-credential ASC
  trio, so an Apple-ID trio on a fleet repo is a finding. A repo whose secrets
  cannot be listed is reported `unknown` and fails the verdict; an unverifiable
  repo is not a pass.
