# Templates render text, not structured data

Creation profiles may use Jinja2 under `StrictUndefined` for the text they own,
such as Markdown, source code, and scripts. They do not template structured data
formats—including JSON, TOML, and YAML—even when a profile exclusively owns the
file; those documents are built as structured values and serialized by one
format-aware renderer. This prevents textual escaping, ordering, and merge rules
from becoming an accidental data model while keeping expressive substitution
where the output is fundamentally authored text.
