- The managed bootstrap scripts (`bin/setup-dev-env.sh`, `agent-start`) now
  resolve their **repo root symlink-safely** (#994). `cd -P` resolves every
  path component physically, so a symlinked intermediate `bin` sent `..` to the
  LINK TARGET's parent, and a symlinked script path (`~/bin/agent-start` → the
  checkout's copy) was never followed at all — both landed outside the
  checkout, provisioning the wrong repo or rooting a coordinator session in it.
  Each script now follows its own link chain first, then resolves its directory
  logically.
