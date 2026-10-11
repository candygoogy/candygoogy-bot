## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## Checks

- [ ] `pytest` passes
- [ ] `ruff check .` and `ruff format --check .` are clean
- [ ] `basedpyright` is clean
- [ ] Nothing from a chat reaches a command line (`tests/test_subprocess_invariant.py`
      passes and wasn't loosened)
- [ ] If settings changed: `.env.example` and the README's configuration table are updated
- [ ] If behaviour changed: the README (and `docs/`, if relevant) still describe it correctly
