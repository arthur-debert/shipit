shipit Verbs & Tasks

shipit's standardized tasks are *verbs*: `pixi run <verb>` means the same thing in
every repo, and the per-toolchain mechanics hide behind the verb name. This document
fixes that vocabulary — what each verb means, grouped by the role it plays; which
names are deliberately NOT verbs; and the one operational convention (argument
passthrough) that lets a human or an agent reach the underlying tool. It records the
FINAL STATE shipit aims for. WHICH verb runs at WHICH trigger is the lane model — it
lives in [../../CONTEXT.md] and [../prd/wf01-pixi-encapsulation-generic-ci.md], not
here ([#5]).

1. The uniform interface

    `pixi run <verb>` is the whole interface. A verb is a uniform-named pixi task: the
    name is fixed across the portfolio, the task body — supplied by the consumer — is
    whatever that repo's toolchain needs. `pixi run test` runs `python -m pytest` in a
    python package and `cargo test` in a rust crate; the CALLER (lefthook, CI, an
    agent) writes the same line either way and never learns the difference. The verb
    is shipit's stable per-repo interface; the toolchain underneath is free to vary.

    pixi PROVISIONS and RUNS the real tool — cargo, pytest, tauri, mkdocs,
    electron-builder — it is never the build backend itself ([./architecture.lex#3]).
    The verb is the stable handle; the real builder stays the real builder.

    Local == CI. The same `pixi run <verb>` invocation runs on a laptop, in local
    Docker, and on a CI runner — one definition invoked everywhere, so the checks
    cannot drift into two transcriptions ([./architecture.lex#7]). "Run it the way CI
    runs it" is just running the verb.

2. The verb vocabulary, by role

    The verbs group by the ROLE they play in the dev cycle — what they enforce, what
    they produce, what they make convenient. The role, not the toolchain, decides
    where a verb runs (commit hook, CI lane, local only). The trigger/lane mapping
    itself is [../../CONTEXT.md]'s *Checks & enforcement* concern, summarized in [#5].

    2.1. Blocking checks — run at commit / push

        lint:
            The standardized multi-language gate (`shipit lint`). Read-only; a missing
            tool hard-fails, never skips. Blocks at commit and at push, and is a
            required CI lane.

        test:
            The fast suite — the consumer-supplied test task. The blocking commit/push
            set is exactly `lint` + the fast `test`; named variants ([#5]) are advisory
            at commit and blocking later.

    2.2. Producing — run local == CI; typically required in CI, not in hooks

        build:
            Compile / bundle the real artifact through the real builder. Runnable on a
            laptop before anything reaches CI, which kills the "push an RC to find out
            if it works" loop ([./architecture.lex#3]).

        docs-build:
            Render the docs site (`mkdocs build`, …) — the producing half of the docs
            split ([#6]).

        release:
            An UMBRELLA over the cut, NOT one command: changelog -> version -> package
            -> sign -> publish, composable opt-in stages ([./workflows.lex#6]). A repo
            wires in only the stages it needs (a CLI skips signing; an unsigned app
            skips notarization). `release` names the whole; the stages are the parts.

    2.3. Convenience — local-first, usually no CI check

        fmt:
            The write-mode sibling of `lint` (`shipit lint --fix`). Mutates files; run
            by a human or an agent, never a gate.

        run / serve:
            Execute the thing locally. Polymorphic by toolchain: a dev-launch for a
            tauri / electron app, a `serve` for a server, a plain exec for a CLI. One
            verb; the toolchain decides the launch.

        docs-serve:
            Live-reload the docs site (`mkdocs serve`). The run-family member for docs
            — `docs-serve` is to `docs-build` what `run` is to `build` ([#6]).

        clean:
            Reset build / artifact state.

3. What is NOT a verb

    Stated explicitly, because each is a natural temptation:

    - provision / install / setup: the pixi-native substrate. Provisioning IS pixi
      (`pixi install`, environments); there is no shipit verb in front of it
      ([./architecture.lex#1]).
    - changelog: a release-internal stage (inside the `release` umbrella, [#2.2]), not
      a top-level verb.
    - bench / typecheck: fold into a `test` or `lint` variant where a repo needs them,
      rather than minting a new top-level verb.

4. Argument passthrough — the -- convention

    The one operational convention. pixi appends trailing arguments to the task
    command verbatim, so a caller reaches the underlying tool by passing them after a
    `--` separator:

    Argument passthrough:

        pixi run <verb> -- <native-stack args>

    :: text ::

    Use the `--` so pixi does not parse a leading flag (`-x`, `-e`, `-p`) as its OWN.
    shipit does NOT model the per-stack argument surface — the consumer's task and the
    underlying tool own it entirely; shipit only guarantees the args arrive verbatim.

    Per-stack examples:

        pixi run test -- -k test_foo      # pytest: select a test
        pixi run test -- --release        # cargo test: a profile flag
        pixi run test -- tests/e2e/spec.ts   # a path to one spec
        pixi run build -- --target x86_64-unknown-linux-gnu   # a build target

    :: sh ::

    The `--` is the safe default; it is strictly required only when the first
    passthrough token would otherwise look like a pixi flag. (Verified: `pixi run test
    -k x` already runs `python -m pytest -q -k x` — the `-k x` is appended verbatim.)

5. Test variants and the lane model

    A repo exposes `test` — the fast, blocking suite — plus named variants:
    `test-e2e`, `test-wasm`, `test-tauri`, … Each variant is its own uniform-named
    task.

    WHICH variant runs at WHICH operation is the LANE model, and it is NOT redefined
    here: a lane declares `{ run = a verb, required, local, trigger, scope }` and the
    generic workflow fans the lanes into jobs. See [../../CONTEXT.md]'s *Lane* and
    *Scope* entries and [../prd/wf01-pixi-encapsulation-generic-ci.md].

    The one rule this doc fixes: the commit/push blocking set is `lint` + the fast
    `test`. Expensive variants are advisory at commit and blocking later in CI
    ([../../CONTEXT.md], *Checks & enforcement*). A verb names the unit of work; the
    lane decides when it runs.

6. The docs split

    Docs get the same producing / running split as code: `docs-build` renders the site
    (the producing verb, parallels `build`) and `docs-serve` live-reloads it locally
    (the run-family verb, parallels `run` / `serve`). Keeping them as two verbs means a
    CI docs lane runs `docs-build` while a human runs `docs-serve`, with no flag
    toggling one task between two modes.

7. Cross-references

    - [../../CONTEXT.md] — the glossary: *Checks & enforcement*, *Toolchain*,
      *Path->toolchain map*, *Lane*, *Scope*, *Verb / task*.
    - [../prd/wf01-pixi-encapsulation-generic-ci.md] — the generic CI / lane model
      that consumes these verbs.
    - [../prd/pixi-test-build-release.md] — the test / build / release task
      encapsulation.
    - [./architecture.lex#3] — producing logic is a pixi task, never the build
      backend; [./architecture.lex#7] — the one-gate definition.
    - [./workflows.lex#2] — the producing / routing boundary applied;
      [./workflows.lex#6] — release as composable opt-in stages.
