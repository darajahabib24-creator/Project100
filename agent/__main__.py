"""python -m agent  ->  same as agent/cli.py.

Exists so the shortest invocation works with no install step:

    python3 -m agent "what is the speed of light"
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
