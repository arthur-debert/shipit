"""shipit.install — the managed-unit domain behind ``shipit install``.

The install reconciliation as an importable API (ADR-0030: domain packages ARE
the API), with the plan/apply split as its spine:

- :mod:`.units` — the managed-set catalog: the :class:`~.units.Unit` model and
  :func:`~.units.load_units`, the packaged desired state.
- :mod:`.splice` — the pure text splicers: marker-delimited blocks and the
  settings.json JSON-hook merge. String in, string out, no filesystem.
- :mod:`.reconcile` — the decision core (the four-case managed compare and the
  three-case retired-files compare, docs/dev/lessons-learned.lex §4), the one
  read boundary (:func:`~.reconcile.gather`), and the pure
  :func:`~.reconcile.reconcile` that aggregates every decision into a frozen
  :class:`~.reconcile.Plan` — inspectable before any file is written.
- :mod:`.apply` — the ONE effectful path: :func:`~.apply.apply` takes a Plan
  and a mode and owns every write, retired-file unlink, hook activation, git
  staging, and PR creation, returning a typed
  :class:`~.apply.InstallResult`. It logs (ADR-0029) and never prints.

Rendering lives at the verb (:mod:`shipit.verbs.install`): the domain returns
values; the terminal is the renderer's.
"""
