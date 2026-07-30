# Portfolio Trees borrow only from Shipit-owned donors

Fleet operations create ordinary temporary Trees without reading human
checkouts. Their reference-and-dissociate clones may borrow Git objects only
from a concurrency-safe, Shipit-owned Repo donor cache; missing donors initialize
on demand and existing donors refresh only through
`shipit fleet tree-cache refresh`.

The cache is an optimization, never an authority: each Tree resolves the live
GitHub default branch, and stale donors may cost a fetch but cannot yield stale
Repo state. This preserves existing Tree provisioning and cleanup while removing
shared mutable WorkingDirs from fleet concurrency.
