"""Enable ``python -m shipit`` as an alias for the console-script entrypoint."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
