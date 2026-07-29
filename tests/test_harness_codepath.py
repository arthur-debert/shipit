from __future__ import annotations

import pytest

from shipit.harness.codepath import is_code_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/shipit/cli.py", True),
        ("/Users/x/h/shipit/src/shipit/harness/policy.py", True),
        ("tests/test_harness_policy.py", True),
        ("tools/release.sh", True),
        ("scripts/foo.py", True),
        ("bin/shipit", True),
        ("bin/deploy", True),
        ("Makefile", True),
        ("Dockerfile", True),
        ("subdir/Makefile", True),
        ("app.ts", True),
        ("components/Button.tsx", True),
        ("server.js", True),
        ("index.mjs", True),
        ("lib.rs", True),
        ("main.go", True),
        ("widget.rb", True),
        ("App.java", True),
        ("vec.cpp", True),
        ("vec.h", True),
        ("parser.c", True),
        ("docs/spec/har01.md", False),
        ("docs/legacy-prd/har01.md", False),
        ("docs/adr/0012-enforcement.lex", False),
        ("AGENTS.lex", False),
        ("README.md", False),
        (".shipit.toml", False),
        ("pixi.toml", False),
        ("pyproject.toml", False),
        ("config.json", False),
        (".github/workflows/ci.yaml", False),
        (".claude/settings.json", False),
        (".claude/agents/implementer.md", False),
        ("docs/examples/snippet.py", False),
        (".claude/hooks/helper.py", False),
        ("styles/site.css", False),
        ("index.html", False),
        ("notes.txt", False),
        ("", False),
    ],
)
def test_is_code_path(path, expected):
    assert is_code_path(path) is expected
