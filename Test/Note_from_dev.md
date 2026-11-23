## Note from dev in context to test results

`Test_result.jpg` is an older run of `tester.py` against the Render deployment
(30 passed, 1 failed, 1 warned). The one failure, "Lyrics Fast Mode", was a
timing check that was stricter than the server's own fast-mode timeout. It is
now a warning above 8s and a failure only above 25s.

Current status:

- **Unit tests** (`pytest`): run in the CI workflow on Python 3.11 to 3.13 and pass.
- **Live integration tests** (`pytest -m integration` with `LYRICA_RUN_INTEGRATION=1`):
  run weekly, on demand, and on PRs that touch sources or tests (Integration workflow).
  The Genius tests are skipped unless `GENIUS_TOKEN` is set, because Genius's
  public search is blocked from GitHub's runners.

If you find something failing, please open an issue or a PR. Thanks!
