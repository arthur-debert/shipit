# Repo remote bootstrap is post-creation and best-effort

`shipit repo new` requires an explicit remote policy, but fulfills it only after
the complete, verified, initially committed local Repo has been atomically
published. Local publication defines command success: remote creation, reuse,
attachment, push, or Actions discovery never participates in the local
transaction and never rolls back either the local Repo or external GitHub state.
A remote-publication failure therefore produces a prominent warning and
actionable recovery commands but exits zero. This deliberately trades an exit
code that certifies the whole requested remote outcome for a stronger guarantee
that a usable local Repo remains the authoritative result; typed results and
rendered output must keep local success, remote publication, and best-effort
Actions discovery distinct.
