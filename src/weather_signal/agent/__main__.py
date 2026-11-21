"""`python -m weather_signal.agent run|install`."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "run":
        from weather_signal.agent.worker import main as run

        run(rest)
    if cmd == "replay-refused":
        from weather_signal.agent import refused

        sys.exit(refused.main())
    if cmd == "install":
        from weather_signal.agent.launchd import main as install

        sys.exit(install(rest))
    sys.stderr.write(
        "usage: python -m weather_signal.agent run [--as-of HH:30Z] [--dry-run] | install"
        " | replay-refused\n"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
