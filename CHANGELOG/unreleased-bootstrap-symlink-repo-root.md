- The managed bootstrap scripts (`bin/setup-dev-env.sh`, `agent-start`) now
  resolve their **repo root symlink-safely** (#994). `cd -P` resolves every
  path component physically, so a symlinked intermediate `bin` sent `..` to the
  LINK TARGET's parent, and a symlinked script path (`~/bin/agent-start` → the
  checkout's copy) was never followed at all — both landed outside the
  checkout, provisioning the wrong repo or rooting a coordinator session in it.
  Each script now follows its own link chain first — joining relative link
  targets against the directory physically holding the link, as the kernel does
  — then resolves the final directory logically. Every resolution step is
  fail-open: a missing or erroring `readlink`, or a `cd` into a directory that
  is gone, warns and uses the path as-is instead of aborting the script or
  silently degrading to a bare `.` root.
