# Creation verifies through the user shell seam

Before publication, `shipit repo new` certifies the staged Repo from a clean
child shell rooted there, with inherited pixi project-selection state prevented
from selecting the invoking checkout. The child materializes the staged Repo's
environment and lockfile, then runs `pixi run lint`, `pixi run test`, and
`pixi run build`; `pixi run` is the non-interactive activation boundary, so
creation neither enters an interactive `pixi shell` nor calls Tool internals.
The normal initial commit then runs the installed hooks. This deliberately tests
the same pixi, launcher, task, Tool, and hook composition a user receives.
