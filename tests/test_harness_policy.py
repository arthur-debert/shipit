from __future__ import annotations

import time

import pytest

from shipit.harness.policy import (
    _REDIRECTS_INTO_TAIL,
    COORDINATOR_DENY_REASON,
    SPAWN_ISOLATION_DENY_REASON,
    WORKTREE_DENY_REASON,
    Decision,
    Permission,
    ToolCall,
    decide,
    decide_cross_tree_write,
    decide_spawn_isolation,
    decide_worktree,
    is_edit_tool,
    tool_call,
)
from shipit.harness.role import Role
from shipit.harness.roleprofile import PROFILES, delegates_code_authorship


@pytest.mark.parametrize(
    ("role", "is_code", "break_glass", "expected"),
    [
        (Role.COORDINATOR, True, False, Permission.DENY),
        (Role.COORDINATOR, True, True, Permission.ALLOW),
        (Role.COORDINATOR, False, False, Permission.ALLOW),
        (Role.COORDINATOR, False, True, Permission.ALLOW),
        (Role.IMPLEMENTER, True, False, Permission.ALLOW),
        (Role.IMPLEMENTER, True, True, Permission.ALLOW),
        (Role.IMPLEMENTER, False, False, Permission.ALLOW),
        (Role.SHEPHERD, True, False, Permission.ALLOW),
        (Role.SHEPHERD, False, False, Permission.ALLOW),
        (Role.EXPLORER, True, False, Permission.ALLOW),
        (Role.EXPLORER, False, False, Permission.ALLOW),
    ],
)
def test_decide_matrix(role, is_code, break_glass, expected):
    assert decide(role, "any/path", is_code, break_glass).permission is expected


def test_edit_guard_is_posture_derived_for_every_role():
    for role in Role:
        denied = (
            decide(role, "src/x.py", is_code=True, break_glass=False).permission
            is Permission.DENY
        )
        assert denied is delegates_code_authorship(role)


def test_only_the_coordinator_delegates_code_authorship_today():
    assert delegates_code_authorship(Role.COORDINATOR)
    for role in (Role.IMPLEMENTER, Role.SHEPHERD, Role.EXPLORER, Role.REVIEWER):
        assert not delegates_code_authorship(role)


def test_coordinator_deny_carries_the_redirect_reason():
    decision = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False)
    assert decision == Decision(Permission.DENY, COORDINATOR_DENY_REASON)
    assert "delegate" in decision.reason
    assert "origin/main" in decision.reason


def test_coordinator_deny_reason_is_the_generated_role_slice():
    reason = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False).reason
    assert "You are the COORDINATOR" in reason
    assert "never implement" in reason
    assert "The roles you delegate to" in reason
    assert "implementer" in reason and "shepherd" in reason


def test_allow_carries_no_reason():
    assert decide(Role.IMPLEMENTER, "src/shipit/cli.py", True, False).reason == ""
    assert decide(Role.COORDINATOR, "src/shipit/cli.py", True, True).reason == ""


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Edit", True),
        ("Write", True),
        ("MultiEdit", True),
        ("NotebookEdit", True),
        ("apply_patch", True),
        ("functions.apply_patch", True),
        ("edit", True),
        ("  Write  ", True),
        ("Read", False),
        ("Bash", False),
        ("Grep", False),
        ("", False),
    ],
)
def test_is_edit_tool(tool, expected):
    assert is_edit_tool(tool) is expected


@pytest.mark.parametrize(
    ("tool_name", "command", "expected"),
    [
        ("EnterWorktree", "", Permission.DENY),
        ("enterworktree", "", Permission.DENY),
        ("  EnterWorktree  ", "", Permission.DENY),
        ("Bash", "git worktree add ../tree-x my-branch", Permission.DENY),
        ("Bash", "git   worktree   add ../t b", Permission.DENY),
        ("Bash", "cd /repo && git worktree add ../t b", Permission.DENY),
        ("Bash", "git worktree add ../t b; ls", Permission.DENY),
        ("bash", "git worktree add ../t b", Permission.DENY),
        ("Bash", "git -C /repo worktree add ../t b", Permission.DENY),
        ("Bash", "git --no-pager worktree add ../t b", Permission.DENY),
        ("Bash", "git -c core.hooksPath= worktree add ../t b", Permission.DENY),
        ("Bash", "FOO=bar git worktree add ../t b", Permission.DENY),
        ("Bash", "git status", Permission.ALLOW),
        ("Bash", "git checkout -b feature/x", Permission.ALLOW),
        ("Bash", "git fetch origin", Permission.ALLOW),
        ("Bash", "git pull --rebase", Permission.ALLOW),
        ("Bash", "git push -u origin HEAD", Permission.ALLOW),
        ("Bash", "git worktree list", Permission.ALLOW),
        ("Bash", "git worktree prune", Permission.ALLOW),
        ("Bash", "gh pr create --draft", Permission.ALLOW),
        ("Bash", "gh pr ready 123", Permission.ALLOW),
        # A QUOTED mention lexes to one word and can never match.
        ("Bash", 'rg "git worktree add"', Permission.ALLOW),
        ("Bash", "printf 'git worktree add'", Permission.ALLOW),
        ("Read", "git worktree add ../t b", Permission.ALLOW),
        ("Bash", "echo worktree add", Permission.ALLOW),
        ("Bash", "", Permission.ALLOW),
        ("Edit", "", Permission.ALLOW),
        # codex names its shell tool `exec_command`, not `bash`.
        ("exec_command", "git worktree add ../t b", Permission.DENY),
        ("exec_command", "git status", Permission.ALLOW),
        ("shell", "git worktree add ../t b", Permission.DENY),
        ("unified_exec", "git worktree add ../t b", Permission.DENY),
        # An unquoted newline cannot hide what follows it.
        ("Bash", "git status\ngit worktree add ../t b", Permission.DENY),
        ("Bash", "echo hi\r\ngit worktree add ../t b", Permission.DENY),
        ("Bash", "ls\n\ngit worktree add ../t b", Permission.DENY),
        ("Bash", "git worktree add ../t b\nls", Permission.DENY),
        ("Bash", "git status \\\ngit worktree add ../t b", Permission.DENY),
        # ...and a newline INSIDE quotes is still data, not a separator.
        ("Bash", 'echo "hi\ngit worktree add ../t b"', Permission.ALLOW),
        ("Bash", "printf 'x\ngit worktree add ../t b'", Permission.ALLOW),
        # No wrapper or keyword ahead of `git` defeats the rule: nothing is
        # counted or skipped, so this set never has to be enumerated.
        ("Bash", "git \\\n worktree add ../t b", Permission.DENY),
        ("Bash", "git \\\nworktree add ../t b", Permission.DENY),
        ("Bash", "git\nworktree add ../t b", Permission.DENY),
        # A comment ends at the newline no matter what precedes it, so the next
        # line is a live command.
        ("Bash", "# \\\ngit worktree add ../t b", Permission.DENY),
        ("Bash", "# c \\\n git worktree add x", Permission.DENY),
        # ...but a continuation with NO space before the backslash concatenates:
        # this runs `gitworktree`, which is not git, so nothing is created.
        ("Bash", "git\\\nworktree add ../t b", Permission.ALLOW),
        ("Bash", "if true; then git worktree add x; fi", Permission.DENY),
        ("Bash", "sudo git worktree add ../t b", Permission.DENY),
        ("Bash", "env  git worktree add ../t b", Permission.DENY),
        ("Bash", "time git worktree add ../t b", Permission.DENY),
        ("Bash", "nice -n 10 git worktree add ../t b", Permission.DENY),
        ("Bash", "while :; do git worktree add x; done", Permission.DENY),
        ("Bash", "ls | xargs -I{} git worktree add {} b", Permission.DENY),
        # An option ARGUMENT can no longer misalign the pair. A quoted `";"` and
        # an unquoted `;` lex identically under posix rules, so counting `-C`'s
        # argument was unfixable — the rule counts nothing instead.
        ("Bash", 'git -C ";" worktree add ../t b', Permission.DENY),
        ("Bash", 'git -C "" worktree add ../t b', Permission.DENY),
        ("Bash", "git -C /tmp worktree add x", Permission.DENY),
        ("Bash", 'git -c "a;b" worktree add x', Permission.DENY),
        # A non-matching `worktree` word must not end the scan.
        ("Bash", "git worktree list && git worktree add x", Permission.DENY),
        ("Bash", "git worktree prune; git worktree add x", Permission.DENY),
        # The `git` word is required, so a grep for the phrase is not a match.
        ("Bash", "grep worktree add file", Permission.ALLOW),
        ("Bash", "grep -r worktree add/", Permission.ALLOW),
        # The accepted cost of counting nothing: these deny. Over-denying costs
        # one redirect message; under-denying silently violates ADR-0014.
        ("Bash", "echo git worktree add", Permission.DENY),
        ("Bash", "git config -l ; worktree add", Permission.DENY),
        # KNOWN-EVADABLE BY DESIGN, not bugs. The words are hidden from the
        # lexer, and this rule is a hygiene nudge for a cooperating agent, not a
        # security boundary. Chasing these would need a shell interpreter.
        ("Bash", "eval 'git worktree add ../t b'", Permission.ALLOW),
        ("Bash", "G=git; $G worktree add ../t b", Permission.ALLOW),
        ("Bash", "sh -c 'git worktree add ../t b'", Permission.ALLOW),
        ("Bash", 'bash -c "git worktree add ../t b"', Permission.ALLOW),
    ],
)
def test_decide_worktree_matrix(tool_name, command, expected):
    call = ToolCall(tool_name=tool_name, command=command)
    assert decide_worktree(call).permission is expected


def test_a_quoted_mention_never_matches_however_it_is_wrapped():
    """Quoting is the whole discrimination: a mention is ONE word, an invocation is adjacent words."""
    for command in (
        'echo "git worktree add ../t b"',
        "echo 'git worktree add ../t b'",
        'rg -n "git worktree add" docs/',
        'grep -r "git worktree add" .',
        'echo "git status\ngit worktree add x"',
    ):
        assert decide_worktree(ToolCall("Bash", command=command)).permission is (
            Permission.ALLOW
        ), command


@pytest.mark.parametrize(
    "command",
    [
        'git "worktree\n" add x',
        '"git\n" worktree add x',
        'git worktree "\n" add x',
    ],
)
def test_quoted_crlf_at_a_word_edge_is_an_accepted_false_positive(command):
    """ACCEPTED, not desired: none of these invokes `git worktree add`, yet all deny.

    The word-edge CR/LF strip cannot tell a quoted newline from a syntactic line
    continuation, because shlex reports no provenance. A correct fix needs a
    provenance-keeping tokenizer, which is a design change rather than a
    normalization tweak — tracked in #1189, and consistent meanwhile with the
    deny bias in docs/adr/0080.
    """
    assert decide_worktree(ToolCall("Bash", command=command)).permission is (
        Permission.DENY
    )


def test_a_quoted_word_keeps_an_interior_newline_so_a_mention_stays_one_word():
    """The limit above is edge-only: interior CR/LF survives, so quoting still discriminates."""
    for command in ('echo "git\nworktree add"', 'echo "hi\ngit worktree add x"'):
        assert decide_worktree(ToolCall("Bash", command=command)).permission is (
            Permission.ALLOW
        ), command


def test_a_line_continuation_follows_the_shell_word_join_exactly():
    """Whether a continuation invokes git depends on the space BEFORE the backslash.

    Verified against bash, by whether a worktree actually appears:
    `git \\<newline>worktree add X` runs `git worktree add X` and CREATES one —
    the space before the backslash already ended the word. Without that space,
    `git\\<newline>worktree` joins into `gitworktree`, which is not a command.
    """
    creates_a_worktree = "git \\\nworktree add ../t b"
    joins_into_one_word = "git\\\nworktree add ../t b"
    assert (
        decide_worktree(ToolCall("Bash", command=creates_a_worktree)).permission
        is Permission.DENY
    )
    assert (
        decide_worktree(ToolCall("Bash", command=joins_into_one_word)).permission
        is Permission.ALLOW
    )


def test_a_comment_ends_at_its_newline_even_after_a_backslash():
    """Splicing continuations before lexing let a comment swallow the next line."""
    for command in (
        "# \\\ngit worktree add ../t b",
        "# c \\\n git worktree add x",
        "echo hi # \\\ngit worktree add x",
    ):
        assert decide_worktree(ToolCall("Bash", command=command)).permission is (
            Permission.DENY
        ), command


def test_a_trailing_comment_is_still_dropped():
    call = ToolCall("Bash", command="ls  # git worktree add ../t b")
    assert decide_worktree(call).permission is Permission.ALLOW


def test_an_unlexable_command_falls_back_to_the_conservative_regex():
    """An unbalanced quote cannot be lexed, so the check errs toward denying."""
    call = ToolCall("Bash", command='echo "unterminated; git worktree add ../t b')
    assert decide_worktree(call).permission is Permission.DENY


def test_worktree_deny_carries_the_redirect_reason():
    for decision in (
        decide_worktree(ToolCall("EnterWorktree")),
        decide_worktree(ToolCall("Bash", command="git worktree add ../t b")),
    ):
        assert decision == Decision(Permission.DENY, WORKTREE_DENY_REASON)
        assert "shipit tree create" in decision.reason
        assert "ADR-0014" in decision.reason


def test_worktree_allow_carries_no_reason():
    assert decide_worktree(ToolCall("Bash", command="git status")).reason == ""
    assert decide_worktree(ToolCall("Bash", command="git worktree list")).reason == ""


@pytest.mark.parametrize(
    "junk",
    [
        {"a": 1},
        {"mode": "worktree"},
        [1],
        ["worktree"],
        1,
        0.5,
        True,
        object(),
    ],
)
def test_a_non_string_isolation_reads_as_absent_and_denies(junk):
    """Coercing truthy junk to a string would read as "isolated" — the one direction that must not fail open."""
    call = tool_call(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "isolation": junk},
        }
    )
    assert call.isolation is None
    assert decide_spawn_isolation(call).permission is Permission.DENY


@pytest.mark.parametrize("falsey", [{}, [], 0, False, None])
def test_a_falsey_non_string_isolation_also_denies(falsey):
    call = tool_call(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "isolation": falsey},
        }
    )
    assert call.isolation is None
    assert decide_spawn_isolation(call).permission is Permission.DENY


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_tool_call_reads_a_blank_isolation_as_absent(blank):
    call = tool_call(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "isolation": blank},
        }
    )
    assert call.isolation is None


def test_tool_call_keeps_a_non_blank_isolation_verbatim():
    call = tool_call(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "isolation": " worktree "},
        }
    )
    assert call.isolation == "worktree"


def test_tool_call_reads_the_payload_cwd():
    call = tool_call(
        {"tool_name": "Bash", "cwd": "/trees/t1", "tool_input": {"command": "ls"}}
    )
    assert call == ToolCall(tool_name="Bash", command="ls", cwd="/trees/t1")


def test_tool_call_reads_a_codex_shell_command_under_cmd():
    call = tool_call(
        {
            "tool_name": "exec_command",
            "cwd": "/repo",
            "tool_input": {"cmd": "git worktree add ../t b", "workdir": "/repo"},
        }
    )
    assert call.command == "git worktree add ../t b"
    assert decide_worktree(call).permission is Permission.DENY


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": "a raw string"},
        {"tool_name": "Bash", "tool_input": None, "cwd": None},
        {"tool_name": None},
    ],
)
def test_tool_call_reads_absent_and_malformed_fields_as_absent(payload):
    call = tool_call(payload)
    assert call.command == ""
    assert call.cwd == ""
    assert call.subagent_type == ""
    assert call.isolation is None


@pytest.mark.parametrize(
    ("subagent_type", "isolation", "expected"),
    [
        ("implementer", None, Permission.DENY),
        ("shepherd", None, Permission.DENY),
        ("reviewer", None, Permission.DENY),
        ("coordinator", None, Permission.DENY),
        ("  Implementer  ", None, Permission.DENY),
        ("implementer", "worktree", Permission.ALLOW),
        # Any non-blank STRING is isolated: which modes are valid is the harness's
        # call, not shipit's. (Non-strings are covered separately, and deny.)
        ("implementer", "remote", Permission.ALLOW),
        # ...but a blank one is not a value at all.
        ("implementer", "", Permission.DENY),
        ("implementer", "   ", Permission.DENY),
        ("implementer", "\t\n", Permission.DENY),
        # `explorer` runs in the ambient WorkingDir — no Tree, so nothing to isolate.
        ("explorer", None, Permission.ALLOW),
        # Not shipit roles: they resolve to no RoleProfile and cannot be gated.
        ("general-purpose", None, Permission.ALLOW),
        ("claude", None, Permission.ALLOW),
        ("Explore", None, Permission.ALLOW),
        ("Plan", None, Permission.ALLOW),
        ("statusline-setup", None, Permission.ALLOW),
        ("", None, Permission.ALLOW),
    ],
)
def test_decide_spawn_isolation_matrix(subagent_type, isolation, expected):
    tool_input: dict[str, str] = {"subagent_type": subagent_type}
    if isolation is not None:
        tool_input["isolation"] = isolation
    call = tool_call({"tool_name": "Agent", "tool_input": tool_input})
    assert decide_spawn_isolation(call).permission is expected


def test_fork_spawn_is_never_denied():
    """A fork inherits the parent's context, so it CANNOT be isolated."""
    call = tool_call({"tool_name": "Agent", "tool_input": {"subagent_type": "fork"}})
    assert decide_spawn_isolation(call).permission is Permission.ALLOW


def test_spawn_isolation_only_governs_the_spawn_tool():
    for tool in ("Bash", "Edit", "Task", "Read", ""):
        call = tool_call(
            {"tool_name": tool, "tool_input": {"subagent_type": "implementer"}}
        )
        assert decide_spawn_isolation(call).permission is Permission.ALLOW


def test_spawn_isolation_is_derived_from_the_roleprofile_registry():
    for role, profile in PROFILES.items():
        call = tool_call(
            {"tool_name": "Agent", "tool_input": {"subagent_type": role.value}}
        )
        expected = Permission.DENY if profile.checkout.tree_backed else Permission.ALLOW
        assert decide_spawn_isolation(call).permission is expected, role


def test_spawn_isolation_deny_names_the_parameter_to_pass():
    call = tool_call(
        {"tool_name": "Agent", "tool_input": {"subagent_type": "implementer"}}
    )
    decision = decide_spawn_isolation(call)
    assert decision == Decision(Permission.DENY, SPAWN_ISOLATION_DENY_REASON)
    assert 'isolation: "worktree"' in decision.reason
    assert "explorer" in decision.reason


def test_spawn_isolation_allow_carries_no_reason():
    call = tool_call(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "isolation": "worktree"},
        }
    )
    assert decide_spawn_isolation(call).reason == ""


OWN_LEAF = "shipit-claude-20260730-015230-141f2c5a-1ca0-4170-b86d-b3a10f3e8c3a"
OTHER_LEAF = "shipit-claude-20260729-233341-297e983f-54ce-4a8f-8afa-c9dea2a23c8b"


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A Trees root holding the caller's own Tree and one other, as `(own_dir, other_dir)`."""
    root = tmp_path / "trees"
    own = root / OWN_LEAF
    other = root / OTHER_LEAF
    for path in (own, other):
        path.mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))
    return own, other


def _shell(command: str, cwd: str, tool: str = "Bash") -> ToolCall:
    return ToolCall(tool, command=command, cwd=cwd)


def test_an_inline_cd_then_a_non_git_writer_is_denied(trees):
    own, other = trees
    call = _shell(f"cd {other} && python3 /tmp/s.py apply src/shipit/git.py", str(own))
    decision = decide_cross_tree_write(call)
    assert decision.permission is Permission.DENY
    assert OTHER_LEAF in decision.reason
    assert OWN_LEAF in decision.reason


def test_deny_reason_states_it_is_not_a_boundary(trees):
    own, other = trees
    decision = decide_cross_tree_write(_shell(f"tee {other}/x", str(own)))
    assert "nudge, not a boundary" in decision.reason
    assert "assembled at runtime" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "cd {other} && python3 /tmp/s.py apply src/x.py",
        "sed -i s/a/b/ {other}/src/shipit/git.py",
        "tee {other}/x.txt",
        "cp AGENTS.md {other}/AGENTS.md",
        "mv x {other}/x",
        "/usr/bin/sed -i s/a/b/ {other}/x",
        "python3.13 -c open({other}/x)",
        "echo hi > {other}/x.txt",
        "echo hi >> {other}/x.txt",
        # No space after the operator: the leaf must still be found AT the leaf,
        # or the redirect check reads a head that does not end in `>`.
        "echo hi >{other}/x.txt",
        "echo hi >>{other}/x.txt",
        'echo hi >"{other}/x.txt"',
        # A bare leaf, and `../<leaf>`, reach a sibling Tree without an abs path.
        "cp x ../{other_leaf}/x",
        "cp x {other_leaf}/x",
        # A writer with a dotted suffix counts; accepted, not desired.
        "sed.orig -i s/a/b/ {other}/x",
        "python3.orig /tmp/s.py {other}/x",
        "sed.py {other}/x",
    ],
)
def test_writes_into_another_tree_are_denied(trees, command):
    own, other = trees
    call = _shell(command.format(other=other, other_leaf=OTHER_LEAF), str(own))
    assert decide_cross_tree_write(call).permission is Permission.DENY


@pytest.mark.parametrize(
    "command",
    [
        # Reading another Tree in place is legitimate and stays allowed.
        "ls {other}/CHANGELOG",
        "grep -rn foo {other}/src",
        "cat {other}/AGENTS.md",
        # A redirect that targets something ELSE must not count as writing the
        # path it merely reads — `2>/dev/null` is the common shape.
        "grep -rn foo {other}/src 2>/dev/null",
        "cat {other}/x > /tmp/y",
        'echo "a" > b.txt && ls {other}',
        # A `>` inside a CLOSED quote is not an operator on the mention.
        'echo "a > b" && ls {other}',
        # git after a cross-checkout `cd` is the host's own guard; not doubled.
        "cd {other} && git status",
        "git -C {other} log --oneline -1",
        # `rm -rf <Tree>` IS how a Tree is reclaimed, and `chmod` is how a
        # reviewer Tree is made read-only: both are legitimate cross-Tree traffic.
        "rm -rf {other}",
        "chmod -R a-w {other}",
        "shipit tree remove {other}",
        # The caller's own Tree, however addressed.
        "cd {own} && python3 -m pytest tests/",
        "sed -i s/a/b/ {own}/src/shipit/git.py",
        "python3 -m pytest tests/",
    ],
)
def test_commands_that_do_not_write_another_tree_are_allowed(trees, command):
    own, other = trees
    call = _shell(command.format(other=other, own=own), str(own))
    assert decide_cross_tree_write(call).permission is Permission.ALLOW


def test_accepted_false_positive_a_writer_word_used_as_data(trees):
    """A write command's NAME as data denies; accepted, not desired."""
    own, other = trees
    call = _shell(f"grep -rn python3 {other}/src", str(own))
    assert decide_cross_tree_write(call).permission is Permission.DENY


@pytest.mark.parametrize(
    ("root_name", "command"),
    [
        ("trees root", 'echo hi > "{other}/x"'),
        ("trees root", "echo hi > '{other}/x'"),
        ("trees root", "echo hi > {escaped}/x"),
        ("trees", 'echo hi > "{other}/x"'),
    ],
)
def test_a_redirect_target_may_contain_spaces(
    monkeypatch, tmp_path, root_name, command
):
    """`SHIPIT_TREES_ROOT` may contain a space, so the target is read up to the closing quote."""
    root = tmp_path / root_name
    own, other = root / OWN_LEAF, root / OTHER_LEAF
    for path in (own, other):
        path.mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))

    call = _shell(
        command.format(other=other, escaped=str(other).replace(" ", "\\ ")), str(own)
    )
    assert decide_cross_tree_write(call).permission is Permission.DENY


@pytest.mark.parametrize(
    "command",
    [
        # A writer outside the registry: the set is the shapes a cooperating
        # agent types, and enumerating every interpreter is not achievable.
        "cd {other} && ruby -e File.write",
        "cd {other} && ./tools/rewrite.sh",
        # The path never appears literally, so there is no leaf to find.
        'cd "$(cat /tmp/target)" && python3 /tmp/s.py',
        'eval "cd $T && python3 /tmp/s.py"',
    ],
)
def test_known_evasions_are_not_caught(trees, command):
    """Documented limits, not aspirations: a writer the registry omits, or a path never spelled."""
    own, other = trees
    call = _shell(command.format(other=other), str(own))
    assert decide_cross_tree_write(call).permission is Permission.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        'eval "cd {other} && python3 /tmp/s.py"',
        "sh -c 'cd {other} && python3 /tmp/s.py'",
        "T={other}; cd $T && python3 /tmp/s.py",
    ],
)
def test_a_literal_path_inside_eval_or_sh_c_is_still_caught(trees, command):
    """The raw command is read, not its shell words: `eval`/`sh -c` hide structure, not text."""
    own, other = trees
    call = _shell(command.format(other=other), str(own))
    assert decide_cross_tree_write(call).permission is Permission.DENY


def test_a_caller_outside_the_trees_root_is_never_refused(trees, tmp_path):
    """ "Another Tree" has no meaning without an own Tree, so the rule stands down."""
    _own, other = trees
    call = _shell(f"cp x {other}/x", str(tmp_path / "ambient-checkout"))
    assert decide_cross_tree_write(call).permission is Permission.ALLOW


def test_a_blank_cwd_does_not_fall_back_to_the_hook_process_cwd(trees):
    """The hook runs in the spawning session's checkout, so a blank cwd reads as unknown."""
    _own, other = trees
    assert (
        decide_cross_tree_write(_shell(f"cp x {other}/x", "")).permission
        is Permission.ALLOW
    )


def test_the_rule_only_fires_on_shell_tools(trees):
    """The edit entry runs the same decider and must be untouched by it."""
    own, other = trees
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Agent"):
        call = _shell(f"cp x {other}/x", str(own), tool=tool)
        assert decide_cross_tree_write(call).permission is Permission.ALLOW, tool


@pytest.mark.parametrize(
    "head",
    [
        "> " + "\\a" * 60,
        "> " + "\\a" * 60 + " ",
        '> "' + "\\a" * 60,
        "> '" + "\\a" * 60,
        ">> " + "\\ " * 60,
    ],
)
def test_the_redirect_prefix_check_stays_linear(head):
    """A timing bound, not a verdict: only this catches a rewrite that lets the alternatives overlap again."""
    started = time.perf_counter()
    _REDIRECTS_INTO_TAIL.search(head)
    assert time.perf_counter() - started < 1.0


def test_the_rule_fires_on_the_codex_shell_tool(trees):
    """codex names its shell tool `exec_command` and puts the command under `cmd`."""
    own, other = trees
    call = tool_call(
        {
            "tool_name": "exec_command",
            "cwd": str(own),
            "tool_input": {"cmd": f"tee {other}/x"},
        }
    )
    assert decide_cross_tree_write(call).permission is Permission.DENY


def test_a_misconfigured_trees_root_allows(trees, monkeypatch):
    """A relative `SHIPIT_TREES_ROOT` raises inside the resolver; it must not refuse every shell command."""
    _own, other = trees
    monkeypatch.setenv("SHIPIT_TREES_ROOT", "relative/not-absolute")
    call = _shell(f"tee {other}/x", str(other))
    assert decide_cross_tree_write(call).permission is Permission.ALLOW
