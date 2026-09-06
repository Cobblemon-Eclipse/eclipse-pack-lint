# Contributing

- Conventional Commits (`fix:`, `feat:`, `docs:`, `test:`, `chore:`).
- Every new rule needs three things: an entry in `packlint/findings.py` carrying
  the **Cobblemon source citation** it was derived from, a synthetic fixture
  test in `tests/test_rules.py`, and a line in the README's finding-class list.
- A rule that can fire on a pack that is actually fine is worse than no rule.
  When in doubt, calibrate against the jar-only baseline (see
  `Validator.vanilla_also_broken`) and suppress rather than report.
- `python -m unittest discover -s tests -t .` must pass before pushing.
- After a Cobblemon version bump, re-run `packlint extract-builtin` and follow
  the README's "Updating for a new Cobblemon version".
