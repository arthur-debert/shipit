- review: the local-agent reviewers (`codex` / `agy`) now work from a consumer
  repo's pinned shipit. PyJWT — the App-JWT signer the whole App-auth path rests
  on — moves from the optional `review` extra into shipit's base dependencies, so
  it rides every install rather than only a checkout that opted into an extra. The
  `review` extra is gone with it, as is the `pr next` fallback that caught a
  "missing PyJWT" error, sniffed the message for it, and re-ran itself inside a
  `pixi run -e review` environment that exists only in shipit's own repo (in a
  consumer it failed with `unknown environment 'review'`). Consumers could not
  require or drive codex/agy at all; their PRs got a single-reviewer net.
- verify-apps: the verb now tells the three situations apart instead of reporting
  them all as NOT LIVE. A machine with no App credentials — no `doppler`, no
  login, no key — gets `UNVERIFIED` and exit 2, because nothing was ever asked of
  GitHub and the Apps' real state is unknown; a repo whose owner has not installed
  the App (or has not consented to `checks: write`) still gets `NOT LIVE` and exit
  1; all-live is exit 0. The old output claimed the Apps were missing from repos
  where they were in fact live, which is a claim about a repo drawn from a fact
  about a laptop. The expected credential gap also stops spraying a traceback per
  App, and the printed verdict and the exit code are now derived from one decision
  so they cannot drift.
- review: `ReviewAuthError` carries the KIND of failure (credentials unavailable
  here / App not installed there / the probe itself failed) and the HTTP status
  when there was one. The "App is not installed" branch reads that status instead
  of grepping `"HTTP 404"` out of the rendered message, so an unrelated error
  whose body quotes a 404 can no longer be reported as a missing installation.
  An answer that arrives but is unusable — a success response that is not JSON,
  is not UTF-8, or carries a non-numeric installation `id` — is reported as a
  failed probe too, rather than escaping as a raw decode error; `verify-apps`
  reports UNVERIFIED for it instead of printing a traceback.
