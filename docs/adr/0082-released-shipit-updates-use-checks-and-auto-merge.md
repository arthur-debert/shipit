# Released Shipit updates use consumer checks and auto-merge

A fleet-generated update to a released Shipit Version does not request consumer
review. The Shipit change was reviewed in its producer Repo; repeating code
review in every consumer examines generated installation output at the wrong
boundary. Consumer required checks and mergeability are the compatibility proof,
so fleet update arms auto-merge and waits up to 25 minutes: green update PRs
merge, while failed or conflicted PRs remain open for follow-up and timed-out PRs
remain pending with auto-merge armed.

This is a narrow exception to ADR-0003 and the general human-merge lifecycle,
only for fleet-generated updates from released Shipit Versions. It does not
apply to Revision testing, first adoption, consumer overrides, feature PRs, or
failed checks. Fleet output must identify every Repo and include the full PR URL
so exceptional cases are directly actionable.
