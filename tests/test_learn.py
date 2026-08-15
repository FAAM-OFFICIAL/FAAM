"""Unit tests for FAAM Learn — the course library, points and grading.

Run from the repo root:

    python3 -m unittest discover -s tests -v

Stdlib only, like the rest of FAAM. Each test gets a throwaway HOME so nothing
touches your real ~/.faam data, and app.py is imported fresh per test class so
DATA_DIR points at that temp directory.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


def load_app(home: str):
    """Import app.py with DATA_DIR pointed at a temp HOME and no AI key, so the
    deterministic fallbacks are exercised and no network calls are made."""
    os.environ["HOME"] = home
    os.environ["FAAM_DATA_DIR"] = str(Path(home) / ".faam")
    os.environ["OPENAI_API_KEY"] = ""
    spec = importlib.util.spec_from_file_location(f"faam_{abs(hash(home))}", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LearnTestCase(unittest.TestCase):
    """Base: fresh temp HOME and a fresh app module per test class."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="faam-test-")
        (Path(cls.home) / ".faam").mkdir(parents=True, exist_ok=True)
        cls.m = load_app(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def user(self, name: str) -> str:
        """A unique username per test so records never bleed between tests."""
        return f"{name}-{self.id().rsplit('.', 1)[-1]}"


# ---------------------------------------------------------------- content ----
class TestLibraryContent(LearnTestCase):
    """The course library itself has to be structurally sound."""

    def test_every_course_is_well_formed(self):
        for c in self.m.LEARN_LIBRARY:
            with self.subTest(course=c["id"]):
                for k in ("id", "cat", "level", "mins", "title", "blurb", "lessons"):
                    self.assertIn(k, c)
                self.assertTrue(c["lessons"], "course has no sections")
                self.assertIn(c["cat"], {x["id"] for x in self.m.LEARN_CATEGORIES})
                self.assertIn(c["level"], {"Beginner", "Intermediate", "Advanced"})

    def test_course_ids_are_unique(self):
        ids = [c["id"] for c in self.m.LEARN_LIBRARY]
        self.assertEqual(len(ids), len(set(ids)))

    def test_personal_id_is_not_a_library_id(self):
        # A library course named "personal" would collide with the Titan course.
        self.assertNotIn(self.m.PERSONAL_COURSE_ID,
                         [c["id"] for c in self.m.LEARN_LIBRARY])

    def test_every_section_test_is_answerable(self):
        for c in self.m.LEARN_LIBRARY:
            for i, sec in enumerate(c["lessons"]):
                with self.subTest(course=c["id"], section=i):
                    self.assertTrue(sec["t"] and sec["b"])
                    self.assertEqual(len(sec["o"]), 4, "need exactly 4 options")
                    self.assertTrue(0 <= sec["c"] < 4, "correct index out of range")
                    self.assertEqual(len(set(sec["o"])), 4, "duplicate options")

    def test_every_course_has_a_five_question_review(self):
        for c in self.m.LEARN_LIBRARY:
            with self.subTest(course=c["id"]):
                review = self.m.course_review(c["id"])
                self.assertEqual(len(review), self.m.REVIEW_LEN)
                for q in review:
                    self.assertEqual(len(q["o"]), 4)
                    self.assertTrue(0 <= q["c"] < 4)

    def test_pass_mark_is_achievable(self):
        self.assertLessEqual(self.m.REVIEW_PASS, self.m.REVIEW_LEN)
        self.assertGreater(self.m.REVIEW_PASS, 0)

    def test_diagnostic_bank_is_well_formed(self):
        ids = [q["id"] for q in self.m.TITAN_DIAGNOSTIC]
        self.assertEqual(len(ids), len(set(ids)), "duplicate question ids")
        for q in self.m.TITAN_DIAGNOSTIC:
            with self.subTest(q=q["id"]):
                self.assertEqual(len(q["a"]), 4)
                self.assertTrue(0 <= q["correct"] < 4)
                self.assertIn(q.get("d"), (1, 2, 3), "difficulty must be 1-3")

    def test_bank_is_large_enough_to_serve_a_test(self):
        self.assertGreaterEqual(len(self.m.TITAN_DIAGNOSTIC), self.m.DIAG_SERVE)


# ------------------------------------------------------------ diagnostics ----
class TestDiagnosticSet(LearnTestCase):

    def test_serves_the_expected_number(self):
        self.assertEqual(len(self.m.titan_diagnostic_set()), self.m.DIAG_SERVE)

    def test_covers_every_topic(self):
        topics = {q["topic"] for q in self.m.TITAN_DIAGNOSTIC}
        served = {q["topic"] for q in self.m.titan_diagnostic_set()}
        self.assertEqual(served, topics, "a placement test must measure every topic")

    def test_no_duplicate_questions_in_one_test(self):
        ids = [q["id"] for q in self.m.titan_diagnostic_set()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_retakes_differ(self):
        # Sampled from a larger bank, so two tests should not be identical.
        a = [q["id"] for q in self.m.titan_diagnostic_set()]
        same = sum(1 for _ in range(8)
                   if [q["id"] for q in self.m.titan_diagnostic_set()] == a)
        self.assertLess(same, 8, "retakes always produced the identical test")


class TestDiagnosticGrading(LearnTestCase):

    def key(self):
        return {q["id"]: q["correct"] for q in self.m.TITAN_DIAGNOSTIC}

    def answers(self, wrong=0):
        """A full-length answer sheet with `wrong` deliberate misses."""
        qs = self.m.titan_diagnostic_set()
        k = self.key()
        return [{"id": q["id"],
                 "chosen": (k[q["id"]] + 1) % 4 if i < wrong else k[q["id"]]}
                for i, q in enumerate(qs)]

    def test_perfect_score(self):
        r = self.m.titan_evaluate_diagnostic(self.answers(), self.user("ace"))
        self.assertEqual(r["score"], r["total"])
        self.assertTrue(r["perfect"])
        self.assertEqual(r["level"], "Proficient")

    def test_zero_score(self):
        r = self.m.titan_evaluate_diagnostic(
            self.answers(wrong=self.m.DIAG_SERVE), self.user("zero"))
        self.assertEqual(r["score"], 0)
        self.assertFalse(r["perfect"])
        self.assertEqual(r["level"], "Foundation")
        self.assertEqual(r["earned"], 0)

    def test_level_thresholds_are_percentage_based(self):
        self.assertEqual(self.m._titan_level(0, 15), "Foundation")
        self.assertEqual(self.m._titan_level(7, 15), "Foundation")   # 47%
        self.assertEqual(self.m._titan_level(9, 15), "Developing")   # 60%
        self.assertEqual(self.m._titan_level(12, 15), "Proficient")  # 80%
        # Same proportions at a different test length.
        self.assertEqual(self.m._titan_level(8, 10), "Proficient")

    def test_harder_questions_are_worth_more(self):
        k = self.key()
        by_diff = {}
        for d in (1, 2, 3):
            q = next(q for q in self.m.TITAN_DIAGNOSTIC if q.get("d") == d)
            r = self.m.titan_evaluate_diagnostic(
                [{"id": q["id"], "chosen": k[q["id"]]}], "")
            by_diff[d] = r["detail"][0]["pts"]
        self.assertEqual(by_diff[1], self.m.LEARN_PTS_PER_LEVEL)
        self.assertEqual(by_diff[2], self.m.LEARN_PTS_PER_LEVEL * 2)
        self.assertEqual(by_diff[3], self.m.LEARN_PTS_PER_LEVEL * 3)

    def test_short_submission_earns_nothing(self):
        """Regression: posting one known-easy question used to score a
        'perfect' 1/1 and collect the full perfect-test bonus."""
        k = self.key()
        q = self.m.TITAN_DIAGNOSTIC[0]
        r = self.m.titan_evaluate_diagnostic(
            [{"id": q["id"], "chosen": k[q["id"]]}], self.user("cheat"))
        self.assertFalse(r["counted"])
        self.assertFalse(r["perfect"])
        self.assertEqual(r["earned"], 0)

    def test_duplicate_answers_do_not_inflate_the_score(self):
        k = self.key()
        q = self.m.TITAN_DIAGNOSTIC[0]
        r = self.m.titan_evaluate_diagnostic(
            [{"id": q["id"], "chosen": k[q["id"]]}] * 20, "")
        self.assertEqual(r["total"], 1)

    def test_unknown_and_malformed_answers_are_ignored(self):
        r = self.m.titan_evaluate_diagnostic(
            [{"id": "no-such-question", "chosen": 0},
             {"id": self.m.TITAN_DIAGNOSTIC[0]["id"], "chosen": "banana"}], "")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["score"], 0)

    def test_generated_course_never_carries_the_answer_key(self):
        r = self.m.titan_evaluate_diagnostic(self.answers(wrong=3), self.user("leak"))
        for lesson in r["course"]:
            self.assertNotIn("c", lesson, "check answer leaked to the client")

    def test_a_retake_resets_the_personal_course(self):
        u = self.user("retaker")
        self.m.titan_evaluate_diagnostic(self.answers(wrong=3), u)
        self.m.learn_lesson_read(u, self.m.PERSONAL_COURSE_ID, 0)
        self.assertTrue(self.m.learn_course_stats(
            self.m.learn_progress(u), self.m.PERSONAL_COURSE_ID)["read"])
        self.m.titan_evaluate_diagnostic(self.answers(wrong=1), u)
        stats = self.m.learn_course_stats(
            self.m.learn_progress(u), self.m.PERSONAL_COURSE_ID)
        self.assertEqual(stats["read"], 0, "a new course should start unread")
        self.assertEqual(self.m.learn_progress(u)["attempts"], 2)


# ----------------------------------------------------------------- points ----
class TestRanks(LearnTestCase):

    def test_first_rank_at_zero(self):
        self.assertEqual(self.m.learn_rank(0)["name"], self.m.LEARN_RANKS[0][1])

    def test_rank_boundaries(self):
        for need, label in self.m.LEARN_RANKS:
            self.assertEqual(self.m.learn_rank(need)["name"], label)
            if need:
                self.assertNotEqual(self.m.learn_rank(need - 1)["name"], label)

    def test_top_rank_has_no_next(self):
        top = self.m.LEARN_RANKS[-1][0]
        r = self.m.learn_rank(top + 10_000)
        self.assertIsNone(r["next"])
        self.assertEqual(r["pct"], 100.0)

    def test_progress_percent_stays_in_range(self):
        for xp in (0, 1, 249, 250, 599, 1100, 1799, 1800, 99_999):
            self.assertGreaterEqual(self.m.learn_rank(xp)["pct"], 0)
            self.assertLessEqual(self.m.learn_rank(xp)["pct"], 100)

    def test_negative_xp_is_clamped(self):
        self.assertEqual(self.m.learn_rank(-500)["name"], self.m.LEARN_RANKS[0][1])


# ------------------------------------------------------- sections & tests ----
class TestSectionProgress(LearnTestCase):
    COURSE = "stocks-orders"

    def sections(self):
        return self.m.LEARN_COURSES[self.COURSE]["lessons"]

    def test_reading_a_section_earns_points_once(self):
        u = self.user("reader")
        first = self.m.learn_lesson_read(u, self.COURSE, 0)
        self.assertEqual(first["gained"], self.m.LEARN_PTS_LESSON)
        again = self.m.learn_lesson_read(u, self.COURSE, 0)
        self.assertEqual(again["gained"], 0, "re-reading must not pay twice")

    def test_out_of_range_section_is_rejected(self):
        u = self.user("oob")
        self.assertIn("error", self.m.learn_lesson_read(u, self.COURSE, 99))
        self.assertIn("error", self.m.learn_lesson_read(u, self.COURSE, -1))
        self.assertIn("error", self.m.learn_lesson_check(u, self.COURSE, 99, 0))

    def test_unknown_course_is_rejected(self):
        u = self.user("nocourse")
        self.assertIn("error", self.m.learn_lesson_read(u, "not-a-course", 0))

    def test_section_test_first_try_pays_full(self):
        u = self.user("firsttry")
        correct = self.sections()[0]["c"]
        r = self.m.learn_lesson_check(u, self.COURSE, 0, correct)
        self.assertTrue(r["correct"])
        self.assertEqual(r["gained"], self.m.LEARN_PTS_CHECK)

    def test_section_test_after_a_miss_pays_the_retry_rate(self):
        u = self.user("retry")
        correct = self.sections()[1]["c"]
        miss = self.m.learn_lesson_check(u, self.COURSE, 1, (correct + 1) % 4)
        self.assertFalse(miss["correct"])
        self.assertEqual(miss["gained"], 0)
        late = self.m.learn_lesson_check(u, self.COURSE, 1, correct)
        self.assertTrue(late["correct"])
        self.assertEqual(late["gained"], self.m.LEARN_PTS_CHECK_RETRY)

    def test_a_passed_section_test_cannot_be_farmed(self):
        u = self.user("farmer")
        correct = self.sections()[0]["c"]
        self.m.learn_lesson_check(u, self.COURSE, 0, correct)
        for _ in range(5):
            self.assertEqual(
                self.m.learn_lesson_check(u, self.COURSE, 0, correct)["gained"], 0)

    def test_streak_bonus_starts_at_three(self):
        u = self.user("streak")
        gains = []
        for i in range(4):
            gains.append(self.m.learn_lesson_check(
                u, self.COURSE, i, self.sections()[i]["c"])["gained"])
        self.assertEqual(gains[0], self.m.LEARN_PTS_CHECK)
        self.assertEqual(gains[1], self.m.LEARN_PTS_CHECK)
        self.assertEqual(gains[2], self.m.LEARN_PTS_CHECK + self.m.LEARN_STREAK_BONUS)
        self.assertEqual(gains[3], self.m.LEARN_PTS_CHECK + self.m.LEARN_STREAK_BONUS)

    def test_a_wrong_answer_breaks_the_streak(self):
        u = self.user("broken")
        for i in range(3):
            self.m.learn_lesson_check(u, self.COURSE, i, self.sections()[i]["c"])
        self.assertGreaterEqual(self.m.learn_progress(u)["streak"], 3)
        wrong = (self.sections()[3]["c"] + 1) % 4
        r = self.m.learn_lesson_check(u, self.COURSE, 3, wrong)
        self.assertEqual(r["streak"], 0)

    def test_answer_reveals_only_after_passing(self):
        u = self.user("reveal")
        pub = self.m.learn_course_public(self.m.learn_progress(u), self.COURSE)
        self.assertFalse(any("ans" in s for s in pub["lessons"]),
                         "answer exposed before the section test was passed")
        self.m.learn_lesson_check(u, self.COURSE, 0, self.sections()[0]["c"])
        pub = self.m.learn_course_public(self.m.learn_progress(u), self.COURSE)
        self.assertEqual(pub["lessons"][0]["ans"], self.sections()[0]["c"])
        self.assertNotIn("ans", pub["lessons"][1], "other sections must stay sealed")

    def test_public_course_never_carries_the_key(self):
        u = self.user("sealed")
        for cid in list(self.m.LEARN_COURSES)[:4]:
            pub = self.m.learn_course_public(self.m.learn_progress(u), cid)
            for sec in pub["lessons"]:
                self.assertNotIn("c", sec)

    def test_finishing_the_article_pays_a_bonus_once(self):
        u = self.user("article")
        total = len(self.sections())
        gains = [self.m.learn_lesson_read(u, self.COURSE, i)["gained"]
                 for i in range(total)]
        self.assertEqual(gains[-1],
                         self.m.LEARN_PTS_LESSON + self.m.LEARN_PTS_ARTICLE)
        self.assertTrue(self.m.learn_course_stats(
            self.m.learn_progress(u), self.COURSE)["articleDone"])


# ----------------------------------------------------------------- review ----
class TestReview(LearnTestCase):
    COURSE = "risk-101"

    def key(self):
        return [q["c"] for q in self.m.course_review(self.COURSE)]

    def read_article(self, u):
        for i in range(len(self.m.LEARN_COURSES[self.COURSE]["lessons"])):
            self.m.learn_lesson_read(u, self.COURSE, i)

    def test_review_is_locked_until_the_article_is_read(self):
        u = self.user("locked")
        pub = self.m.learn_course_public(self.m.learn_progress(u), self.COURSE)
        self.assertEqual(pub["review"], [], "questions served while locked")
        self.assertFalse(pub["progress"]["reviewUnlocked"])
        self.assertIn("error", self.m.learn_review_submit(u, self.COURSE, [0] * 5))

    def test_review_unlocks_and_hides_the_key(self):
        u = self.user("unlock")
        self.read_article(u)
        pub = self.m.learn_course_public(self.m.learn_progress(u), self.COURSE)
        self.assertTrue(pub["progress"]["reviewUnlocked"])
        self.assertEqual(len(pub["review"]), self.m.REVIEW_LEN)
        for q in pub["review"]:
            self.assertNotIn("c", q, "review answer key leaked")

    def test_wrong_length_submission_is_rejected(self):
        u = self.user("short")
        self.read_article(u)
        self.assertIn("error", self.m.learn_review_submit(u, self.COURSE, [0, 1]))
        self.assertIn("error", self.m.learn_review_submit(u, self.COURSE, "nope"))

    def test_failing_earns_no_certificate(self):
        u = self.user("fail")
        self.read_article(u)
        wrong = [(c + 1) % 4 for c in self.key()]
        r = self.m.learn_review_submit(u, self.COURSE, wrong)
        self.assertEqual(r["score"], 0)
        self.assertFalse(r["passed"])
        self.assertEqual(r["gained"], 0)
        self.assertNotIn(self.COURSE, r["certs"])

    def test_passing_earns_the_certificate_and_bonus(self):
        u = self.user("pass")
        self.read_article(u)
        r = self.m.learn_review_submit(u, self.COURSE, self.key())
        self.assertTrue(r["passed"])
        self.assertTrue(r["firstPass"])
        self.assertIn(self.COURSE, r["certs"])
        expected = self.m.REVIEW_LEN * self.m.LEARN_PTS_REVIEW + self.m.LEARN_PTS_COURSE
        self.assertEqual(r["gained"], expected)

    def test_below_the_pass_mark_still_scores_but_does_not_pass(self):
        u = self.user("partial")
        self.read_article(u)
        k = self.key()
        answers = list(k)
        for i in range(self.m.REVIEW_LEN - (self.m.REVIEW_PASS - 1)):
            answers[i] = (k[i] + 1) % 4          # leave PASS-1 correct
        r = self.m.learn_review_submit(u, self.COURSE, answers)
        self.assertEqual(r["score"], self.m.REVIEW_PASS - 1)
        self.assertFalse(r["passed"])
        self.assertEqual(r["gained"], r["score"] * self.m.LEARN_PTS_REVIEW)

    def test_retaking_pays_only_for_improvement(self):
        u = self.user("improve")
        self.read_article(u)
        k = self.key()
        two_wrong = list(k)
        two_wrong[0] = (k[0] + 1) % 4
        two_wrong[1] = (k[1] + 1) % 4
        first = self.m.learn_review_submit(u, self.COURSE, two_wrong)
        self.assertEqual(first["score"], 3)
        second = self.m.learn_review_submit(u, self.COURSE, k)   # 5/5
        # Paid for the 2-point improvement plus the pass bonus, not all 5 again.
        self.assertEqual(second["gained"],
                         2 * self.m.LEARN_PTS_REVIEW + self.m.LEARN_PTS_COURSE)
        third = self.m.learn_review_submit(u, self.COURSE, k)
        self.assertEqual(third["gained"], 0, "a repeat perfect score must pay nothing")

    def test_a_worse_retake_does_not_lower_the_best(self):
        u = self.user("worse")
        self.read_article(u)
        k = self.key()
        self.m.learn_review_submit(u, self.COURSE, k)
        self.m.learn_review_submit(u, self.COURSE, [(c + 1) % 4 for c in k])
        stats = self.m.learn_course_stats(self.m.learn_progress(u), self.COURSE)
        self.assertEqual(stats["reviewBest"], self.m.REVIEW_LEN)
        self.assertTrue(stats["reviewPassed"], "passing should not be revoked")

    def test_detail_explains_each_miss(self):
        u = self.user("detail")
        self.read_article(u)
        k = self.key()
        answers = list(k)
        answers[2] = (k[2] + 1) % 4
        r = self.m.learn_review_submit(u, self.COURSE, answers)
        self.assertEqual(len(r["detail"]), self.m.REVIEW_LEN)
        self.assertFalse(r["detail"][2]["correct"])
        self.assertNotEqual(r["detail"][2]["your"], r["detail"][2]["answer"])

    def test_reading_alone_never_certifies(self):
        """Regression: the certificate used to be granted for scrolling."""
        u = self.user("scroller")
        self.read_article(u)
        stats = self.m.learn_course_stats(self.m.learn_progress(u), self.COURSE)
        self.assertTrue(stats["articleDone"])
        self.assertFalse(stats["certified"])
        self.assertFalse(stats["done"])


# --------------------------------------------------------------- progress ----
class TestProgressAndStreaks(LearnTestCase):

    def test_new_user_starts_empty(self):
        p = self.m.learn_progress(self.user("new"))
        self.assertEqual(p["xp"], 0)
        self.assertEqual(p["courses"], {})
        self.assertEqual(p["certs"], {})

    def test_library_is_public_without_any_keys(self):
        pub = self.m.learn_public(self.m.learn_progress(self.user("pub")))
        self.assertEqual(len(pub["library"]), len(self.m.LEARN_LIBRARY))
        for row in pub["library"]:
            self.assertNotIn("lessons_content", row)
            self.assertIn("progress", row)

    def test_personal_course_appears_only_after_a_test(self):
        u = self.user("personal")
        pub = self.m.learn_public(self.m.learn_progress(u))
        self.assertFalse(any(c.get("personal") for c in pub["library"]))
        p = self.m.learn_progress(u)
        p["personal"] = [{"t": "X", "b": "Y", "q": "Z?", "o": ["a", "b", "c", "d"], "c": 0}]
        self.m.learn_write(u, p)
        pub = self.m.learn_public(self.m.learn_progress(u))
        self.assertTrue(pub["library"][0].get("personal"),
                        "the Titan course should sit at the top of the catalog")

    def test_day_streak_counts_once_per_day(self):
        p = self.m._learn_fresh()
        self.assertEqual(self.m.learn_touch_day(p), 1)
        self.assertEqual(self.m.learn_touch_day(p), 0, "same day must not increment")
        self.assertEqual(p["days"], 1)

    def test_day_streak_continues_from_yesterday(self):
        import time as _t
        p = self.m._learn_fresh()
        p["day"] = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() - 86400))
        p["days"] = 4
        self.m.learn_touch_day(p)
        self.assertEqual(p["days"], 5)
        self.assertEqual(p["bestDays"], 5)

    def test_day_streak_resets_after_a_gap(self):
        import time as _t
        p = self.m._learn_fresh()
        p["day"] = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() - 6 * 86400))
        p["days"], p["bestDays"] = 9, 9
        self.m.learn_touch_day(p)
        self.assertEqual(p["days"], 1, "a missed day resets the streak")
        self.assertEqual(p["bestDays"], 9, "best is a record, it should not drop")

    def test_awards_are_never_negative(self):
        p = self.m._learn_fresh()
        self.m.learn_award(p, -50, "nope")
        self.assertEqual(p["xp"], 0)

    def test_points_persist_across_reloads(self):
        u = self.user("persist")
        self.m.learn_lesson_read(u, "crypto-101", 0)
        xp = self.m.learn_progress(u)["xp"]
        self.assertEqual(self.m.learn_progress(u)["xp"], xp)
        self.assertGreater(xp, 0)


# -------------------------------------------------------------- migration ----
class TestMigration(LearnTestCase):
    """Records written before the per-course restructure must survive."""

    def legacy(self):
        return {
            "xp": 710, "attempts": 1, "best": 12, "streak": 2, "bestStreak": 4,
            "course": [
                {"t": "Old A", "b": "body", "q": "q?", "o": ["a", "b", "c", "d"], "c": 1},
                {"t": "Old B", "b": "body", "q": "q2?", "o": ["a", "b", "c", "d"], "c": 2},
            ],
            "lessons": {"0": {"read": True, "check": "right", "pts": 70}},
            "courseBonus": False,
            "result": {"score": 12, "total": 15, "level": "Developing"},
            "log": [],
        }

    def migrate(self, name):
        u = self.user(name)
        d = self.m._load_json(self.m.LEARN_FILE, {})
        d[u] = self.legacy()
        self.m._save_json(self.m.LEARN_FILE, d)
        return u, self.m.learn_progress(u)

    def test_xp_and_history_survive(self):
        _, p = self.migrate("legacy-xp")
        self.assertEqual(p["xp"], 710)
        self.assertEqual(p["best"], 12)
        self.assertEqual(p["bestStreak"], 4)

    def test_old_course_becomes_the_personal_course(self):
        _, p = self.migrate("legacy-course")
        self.assertEqual(len(p["personal"]), 2)
        marks = p["courses"][self.m.PERSONAL_COURSE_ID]["lessons"]
        self.assertTrue(marks["0"]["read"])

    def test_stale_keys_are_dropped(self):
        _, p = self.migrate("legacy-stale")
        for key in ("course", "lessons", "courseBonus"):
            self.assertNotIn(key, p)

    def test_migrated_record_is_immediately_usable(self):
        u, _ = self.migrate("legacy-usable")
        pub = self.m.learn_course_public(
            self.m.learn_progress(u), self.m.PERSONAL_COURSE_ID)
        self.assertEqual(len(pub["lessons"]), 2)
        for sec in pub["lessons"]:
            self.assertNotIn("c", sec)
        r = self.m.learn_lesson_read(u, self.m.PERSONAL_COURSE_ID, 1)
        self.assertTrue(r["ok"])


# ------------------------------------------------------------ concurrency ----
class TestConcurrentWrites(LearnTestCase):
    """Reading an article fires several section-read calls at once. Every one of
    them is a read-modify-write over a single JSON file, so without a lock the
    later writes discard the earlier ones and sections silently go unread."""

    COURSE = "strat-long"

    def test_concurrent_section_reads_all_survive(self):
        import threading
        u = self.user("race")
        n = len(self.m.LEARN_COURSES[self.COURSE]["lessons"])
        start = threading.Barrier(n)

        def read(i):
            start.wait()                       # fire them at the same instant
            self.m.learn_lesson_read(u, self.COURSE, i)

        threads = [threading.Thread(target=read, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = self.m.learn_course_stats(self.m.learn_progress(u), self.COURSE)
        self.assertEqual(stats["read"], n, "a concurrent write was lost")
        self.assertTrue(stats["articleDone"])

    def test_concurrent_section_tests_all_bank_points(self):
        import threading
        u = self.user("race-check")
        secs = self.m.LEARN_COURSES[self.COURSE]["lessons"]
        start = threading.Barrier(len(secs))

        def answer(i):
            start.wait()
            self.m.learn_lesson_check(u, self.COURSE, i, secs[i]["c"])

        threads = [threading.Thread(target=answer, args=(i,)) for i in range(len(secs))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = self.m.learn_course_stats(self.m.learn_progress(u), self.COURSE)
        self.assertEqual(stats["passed"], len(secs), "a concurrent check was lost")
        self.assertGreaterEqual(self.m.learn_progress(u)["xp"],
                                len(secs) * self.m.LEARN_PTS_CHECK)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------- rewards & leaderboard ----
class TestDailyReward(LearnTestCase):

    def test_first_claim_pays_the_base(self):
        u = self.user("d1")
        r = self.m.learn_claim_daily(u)
        self.assertTrue(r["ok"])
        self.assertEqual(r["gained"], self.m.LEARN_DAILY_BASE)

    def test_cannot_claim_twice_in_a_day(self):
        u = self.user("d2")
        self.m.learn_claim_daily(u)
        self.assertIn("error", self.m.learn_claim_daily(u))

    def test_reward_grows_on_consecutive_days(self):
        u = self.user("d3")
        seen = []
        for run in range(4):
            p = self.m.learn_progress(u)
            if run:
                p["daily"] = {"day": self.m._utc_day(-1), "run": run}
                self.m.learn_write(u, p)
            seen.append(self.m.learn_claim_daily(u)["gained"])
        self.assertEqual(seen[0], self.m.LEARN_DAILY_BASE)
        for a, b in zip(seen, seen[1:]):
            self.assertGreater(b, a, "each consecutive day should pay more")

    def test_reward_stops_growing_at_the_cap(self):
        u = self.user("d4")
        p = self.m.learn_progress(u)
        p["daily"] = {"day": self.m._utc_day(-1), "run": 50}
        self.m.learn_write(u, p)
        gained = self.m.learn_claim_daily(u)["gained"]
        cap = self.m.LEARN_DAILY_BASE + (self.m.LEARN_DAILY_STEPS - 1) * self.m.LEARN_DAILY_STEP
        self.assertEqual(gained, cap)

    def test_a_missed_day_restarts_the_run(self):
        u = self.user("d5")
        p = self.m.learn_progress(u)
        p["daily"] = {"day": self.m._utc_day(-5), "run": 6}
        self.m.learn_write(u, p)
        self.assertEqual(self.m.learn_claim_daily(u)["gained"], self.m.LEARN_DAILY_BASE)

    def test_boost_arrives_on_schedule(self):
        u = self.user("d6")
        p = self.m.learn_progress(u)
        p["daily"] = {"day": self.m._utc_day(-1),
                      "run": self.m.LEARN_DAILY_BOOST_EVERY - 1}
        self.m.learn_write(u, p)
        r = self.m.learn_claim_daily(u)
        self.assertTrue(r["boostStarted"])
        self.assertTrue(r["boost"]["active"])
        self.assertEqual(r["boost"]["mult"], self.m.LEARN_BOOST_MULT)


class TestBoost(LearnTestCase):

    def test_boost_multiplies_awards(self):
        p = self.m._learn_fresh()
        p["boost"] = {"until": int(__import__("time").time()) + 600,
                      "mult": self.m.LEARN_BOOST_MULT}
        self.assertEqual(self.m.learn_award(p, 50, "x"), 50 * self.m.LEARN_BOOST_MULT)

    def test_expired_boost_does_not_multiply(self):
        p = self.m._learn_fresh()
        p["boost"] = {"until": int(__import__("time").time()) - 5, "mult": 2}
        self.assertEqual(self.m.learn_award(p, 50, "x"), 50)

    def test_claiming_the_boost_does_not_multiply_the_claim(self):
        """Otherwise the boost would pay for itself the moment it started."""
        u = self.user("b3")
        p = self.m.learn_progress(u)
        p["daily"] = {"day": self.m._utc_day(-1),
                      "run": self.m.LEARN_DAILY_BOOST_EVERY - 1}
        self.m.learn_write(u, p)
        r = self.m.learn_claim_daily(u)
        expected = self.m.LEARN_DAILY_BASE + min(
            self.m.LEARN_DAILY_BOOST_EVERY - 1,
            self.m.LEARN_DAILY_STEPS - 1) * self.m.LEARN_DAILY_STEP
        self.assertEqual(r["gained"], expected, "the claim itself must not be boosted")


class TestStreakFreeze(LearnTestCase):
    COURSE = "stocks-101"

    def pass_review(self, u, course):
        for i in range(len(self.m.LEARN_COURSES[course]["lessons"])):
            self.m.learn_lesson_read(u, course, i)
        return self.m.learn_review_submit(
            u, course, [q["c"] for q in self.m.course_review(course)])

    def test_passing_a_review_earns_a_freeze(self):
        u = self.user("f1")
        r = self.pass_review(u, self.COURSE)
        self.assertTrue(r["freezeEarned"])
        self.assertEqual(r["freezes"], 1)

    def test_freezes_are_capped(self):
        u = self.user("f2")
        earned = [self.pass_review(u, c)["freezeEarned"]
                  for c in ("stocks-101", "stocks-orders", "stocks-charts", "risk-101")]
        self.assertEqual(sum(1 for e in earned if e), self.m.LEARN_FREEZE_MAX)
        self.assertEqual(len(self.m.learn_prune_freezes(self.m.learn_progress(u))),
                         self.m.LEARN_FREEZE_MAX)

    def test_a_freeze_covers_one_missed_day(self):
        u = self.user("f3")
        self.pass_review(u, self.COURSE)
        p = self.m.learn_progress(u)
        p["day"], p["days"] = self.m._utc_day(-2), 9      # skipped yesterday
        self.m.learn_write(u, p)
        p = self.m.learn_progress(u)
        self.m.learn_touch_day(p)
        self.assertEqual(p["days"], 10, "the streak should have survived")
        self.assertEqual(len(p["freezes"]), 0, "the freeze should have been spent")

    def test_streak_resets_when_freezes_run_out(self):
        u = self.user("f4")
        self.pass_review(u, self.COURSE)                   # exactly one freeze
        p = self.m.learn_progress(u)
        p["day"], p["days"] = self.m._utc_day(-4), 12      # three missed days
        self.m.learn_write(u, p)
        p = self.m.learn_progress(u)
        self.m.learn_touch_day(p)
        self.assertEqual(p["days"], 1, "one freeze cannot cover three days")
        self.assertEqual(len(p["freezes"]), 1, "and it should not have been spent")

    def test_expired_freezes_are_not_usable(self):
        p = self.m._learn_fresh()
        import time as _t
        p["freezes"] = [int(_t.time()) - 1, int(_t.time()) + 86400]
        self.assertEqual(len(self.m.learn_prune_freezes(p)), 1)

    def test_a_freeze_expires_after_the_stated_window(self):
        p = self.m._learn_fresh()
        import time as _t
        self.m.learn_grant_freeze(p)
        life_days = (p["freezes"][0] - int(_t.time())) / 86400
        self.assertAlmostEqual(life_days, self.m.LEARN_FREEZE_DAYS, delta=0.01)

    def test_no_freeze_is_spent_on_an_unbroken_streak(self):
        u = self.user("f7")
        self.pass_review(u, self.COURSE)
        p = self.m.learn_progress(u)
        p["day"], p["days"] = self.m._utc_day(-1), 4
        self.m.learn_write(u, p)
        p = self.m.learn_progress(u)
        self.m.learn_touch_day(p)
        self.assertEqual(p["days"], 5)
        self.assertEqual(len(p["freezes"]), 1, "yesterday counts — nothing to cover")


class TestLeaderboard(LearnTestCase):

    def seed(self):
        for name, xp in (("lb-jo", 2100), ("lb-ak", 1240), ("lb-sam", 890), ("lb-zero", 0)):
            p = self.m.learn_progress(name)
            p["xp"] = xp
            self.m.learn_write(name, p)

    def test_ranks_by_points_descending(self):
        self.seed()
        top = self.m.learn_leaderboard("lb-ak")["top"]
        names = [r["name"] for r in top if r["name"].startswith("lb-")]
        self.assertEqual(names[:3], ["lb-jo", "lb-ak", "lb-sam"])

    def test_places_are_sequential(self):
        self.seed()
        top = self.m.learn_leaderboard()["top"]
        self.assertEqual([r["place"] for r in top], list(range(1, len(top) + 1)))

    def test_players_with_no_points_are_excluded(self):
        self.seed()
        names = [r["name"] for r in self.m.learn_leaderboard()["top"]]
        self.assertNotIn("lb-zero", names)

    def test_your_own_row_is_returned(self):
        self.seed()
        you = self.m.learn_leaderboard("lb-ak")["you"]
        self.assertEqual(you["name"], "lb-ak")
        self.assertEqual(you["xp"], 1240)

    def test_no_row_for_a_stranger(self):
        self.seed()
        self.assertIsNone(self.m.learn_leaderboard("not-a-player")["you"])

    def test_only_public_fields_are_exposed(self):
        """A leaderboard must never leak emails or holdings."""
        self.seed()
        allowed = {"name", "xp", "rank", "days", "certs", "place"}
        for row in self.m.learn_leaderboard()["top"]:
            self.assertEqual(set(row) - allowed, set())
