"""One-off/operational CLI entry points, run as `python -m app.scripts.<name>`.

Each module in here follows the same shape as `app/seed.py` (the project's
existing precedent for a `python -m app.<module>` entry point): a plain
`main()` function, an `if __name__ == "__main__": main()` guard, its own
SessionLocal usage, and a process exit code that reflects success/failure.
"""
