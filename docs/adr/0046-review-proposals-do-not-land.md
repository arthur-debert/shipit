# Review proposals do not land themselves

A **Reviewer Run** may eventually produce a **Review proposal** — a candidate
diff, patch, branch, or stacked PR — as supporting output, but it does not mutate
the reviewed checkout or land changes into the PR it is reviewing. The reviewed
source of truth remains the reviewer’s **Read-only Tree**; any future
**Proposal Work Env** is auxiliary and exists only to prepare or validate the
proposal. The **Shepherd** remains the actor that decides whether and how to
incorporate proposed changes, preserving the role split while leaving room for
higher-value reviewer output.

We record this now because the glossary terms alone do not explain the boundary:
reviewers may become more useful than comment-only agents without becoming
implementers, and linted/tested candidate changes are valuable only if they do
not blur ownership of the active PR.
