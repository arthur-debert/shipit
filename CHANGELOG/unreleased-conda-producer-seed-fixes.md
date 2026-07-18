- conda: three fixes in the untested producer path, surfaced by the FIRST
  real run — the ARF02 channel seed (#1049, blocks #1002). (1) The rendered
  recipe's copy source is now the bare binary name: rattler-build STRIPS the
  archive's single top-level `<artifact>-<triple>/` dir on extraction, so the
  old prefixed source failed `cp: cannot stat`. (2) The S3 env seam feeds
  rattler-build the AWS SDK credential-chain names (`AWS_ENDPOINT_URL` /
  `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) — the `S3_*`
  names were ignored and publish died "Could not determine region from AWS
  SDK configuration". (3) The managed rust-release deps block pins
  `rattler-build = "0.69.*"`: 0.68.* panicked during the S3 upload
  (opendal-core "concurrent tasks executed with no executor") even with
  correct creds. All three validated live against the `lex-fmt/lex`
  `v0.19.8-rc.1` `lexd-lsp` archive + a push to the public channel bucket.
