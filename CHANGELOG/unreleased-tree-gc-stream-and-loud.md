- `tree gc` now **streams each removal as it happens and exits loud on a
  partly-seen fleet** (#1011). Two failures are fixed. First, the sweep used to
  buffer every `REMOVED <path>` line until the whole run finished, so a sweep
  interrupted at minute 14 (a `timeout`, a Ctrl-C reacting to what looked like a
  hang) had deleted Trees and printed **nothing** — no record of which ones. Each
  path is now streamed from inside `sweep` as it is removed, making a multi-minute
  destructive operation legible and its audit trail interrupt-safe. Second, a run
  that could only PARTIALLY see the fleet — some PR states unreadable — used to
  exit 0 reporting `removed 0`, indistinguishable from a genuinely clean fleet;
  that is what let 526 stale Trees accumulate. An incomplete view now exits
  **non-zero** and leads its summary with the skip, because gc's job is to decide
  the whole root and a run that could not read part of it did not do that job.
