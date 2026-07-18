# Repo creation publishes by atomic rename

`shipit repo new` accepts a requested destination only when it is absent or an
empty directory; files, symlinks, and directories containing any entry are
refused. It builds the complete, verified, initially committed Repo in a
temporary sibling under the same parent, then publishes it with one
same-filesystem atomic rename after rechecking that no content appeared. A
handled failure removes the temporary sibling and preserves the destination's
absent-or-empty preflight state; cleanup failure is reported but never publishes
it. V1 never merges into or diffs against a non-empty destination—that is a
separate future capability—so the visible local success outcome is always one
complete Repo. This atomicity boundary ends when the local Repo is published;
the explicitly selected remote bootstrap follows afterward and is governed by
[ADR-0075](0075-repo-remote-bootstrap-is-post-creation-and-best-effort.md).
