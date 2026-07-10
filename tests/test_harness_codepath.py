"""Code-path classifier: path fixtures -> code (guarded) / non-code (allowed).

The HAR01 default (ADR-0012): `src/**` + `tests/**` + known code extensions are
code; docs, `.lex`, config files, and `.claude/**` are non-code so the
coordinator can author + plan. Fail-open bias: when unsure, non-code.
"""

from __future__ import annotations

import pytest

from shipit.harness.codepath import is_code_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # --- CODE (guarded) ---------------------------------------------------
        ("src/shipit/cli.py", True),
        ("/Users/x/h/shipit/src/shipit/harness/policy.py", True),  # absolute form
        ("tests/test_harness_policy.py", True),
        ("tools/release.sh", True),  # shell script
        ("scripts/foo.py", True),  # a .py anywhere is code
        # Executables + build files are code (fail-open default was too narrow).
        ("bin/shipit", True),  # bin/ executable, no extension
        ("bin/deploy", True),
        ("Makefile", True),  # known code filename
        ("Dockerfile", True),
        ("subdir/Makefile", True),
        # Mainstream language extensions are code wherever they live.
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
        # --- NON-CODE (allowed) -----------------------------------------------
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
        # Allow-overrides win over the code rules: a .py under docs/ or .claude/
        # is still non-code (fail-open bias).
        ("docs/examples/snippet.py", False),
        (".claude/hooks/helper.py", False),
        # Markup/styling stays NON-code (config/docs surface, not guarded code).
        ("styles/site.css", False),
        ("index.html", False),
        # Unknown extension, no code dir -> non-code (conservative).
        ("notes.txt", False),
        ("", False),
    ],
)
def test_is_code_path(path, expected):
    assert is_code_path(path) is expected
