# FAAM tests

Stdlib `unittest` only — no pip installs, same as the app.

Run everything from the repo root:

```bash
python3 -m unittest discover -s tests -v
```

Run one class or one test:

```bash
python3 -m unittest tests.test_learn.TestReview -v
python3 -m unittest tests.test_learn.TestReview.test_passing_earns_the_certificate_and_bonus
```

Treat deprecation warnings as failures (worth doing before a release):

```bash
python3 -W error::DeprecationWarning -m unittest discover -s tests
```

## What's covered

`test_learn.py` exercises the FAAM Learn backend:

| Area | Checks |
|---|---|
| Library content | Every course well-formed, unique ids, 4 distinct options per question, a 5-question review per course |
| Placement test | Full-length sampling, every topic covered, retakes differ, percentage-based levels, difficulty weighting |
| Section progress | Read-once payment, retry rate after a miss, streak bonus at 3, answers revealed only after passing |
| Review | Locked until the article is read, key never served, pass mark, certificate, best-score-only scoring |
| Points & ranks | Rank boundaries, clamping, day streaks (same day / next day / after a gap) |
| Migration | Records written before the per-course restructure still load and stay usable |
| Concurrency | Simultaneous section reads and section tests all persist |

## Notes

- Each test class gets its own temp `HOME`, so your real `~/.faam` is never touched.
- `OPENAI_API_KEY` is cleared, so the deterministic fallbacks run and no network calls are made.
- Tests that assert *absence* of an answer key are the important ones — the grading model depends on the key never reaching the browser.
- `TestConcurrentWrites` is a regression guard: remove the lock in `app.py` and it fails.
