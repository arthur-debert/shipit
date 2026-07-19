- cascade: the generated `shipit-artifact-cascade.yml` receive workflow now
  passes shipit's own strict yamllint and invokes the launcher through the `--`
  separator (#1057). The generator's foreign-dispatch guard echo was shortened
  under the 120-column cap (it exceeded it, forcing lex-fmt/vscode#162 to add a
  consumer `[lint].ignore` for the managed bytes), and the `pixi run --locked`
  step now reads `pixi run --locked -- ./bin/shipit channel receive …` so pixi
  never mistakes `./bin/shipit` for a task name. A regression test renders the
  workflow and runs the shipped yamllint config over it, so a >120-char line or
  a dropped separator can't silently return. Consumers no longer need a
  `[lint].ignore` for the generated file.
