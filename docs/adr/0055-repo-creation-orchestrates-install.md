# Repo creation orchestrates managed installation

`shipit repo new` is a separate repository-creation orchestrator: it creates the
consumer-owned project scaffold, then invokes the existing install domain
in-process to add shipit's managed state. `shipit install` remains a reconciler
for managed units and does not learn how to create project source or manifests;
this preserves one authority for the managed catalog, keeps creation policy out
of adoption, avoids a subprocess seam, and lets the creation orchestrator present
one typed result and failure model across both phases.
