import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opener  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

LONG = ("We rebuilt our onboarding flow around one question: what does a new user need to see in the "
        "first ten seconds? Cutting four screens doubled activation. Design reviews got shorter too.")


def person(pid, name, url="https://example.test/in/a", title="Product Designer", skills=("Figma",)):
    return {"crustdata_person_id": pid,
            "basic_profile": {"name": name, "headline": "h"},
            "social_handles": {"professional_network_identifier": {"profile_url": url}} if url else {},
            "skills": {"professional_network_skills": list(skills)},
            "experience": {"employment_details": {"current": [{"title": title}]}}}


def post(url="https://example.test/in/a", text=LONG, kind="original", date="2026-07-10T08:00:00Z"):
    return {"text": text, "post_type": kind, "date_posted": date, "share_url": "https://example.test/p/1",
            "actor": {"professional_network_url": url}}


class ShortlistTests(unittest.TestCase):
    def test_accepts_matching_person(self):
        c = opener.shortlist_ok(person(1, "Ada Lovelace"), "Product Designer", ["figma"])
        self.assertEqual(c["person_id"], 1)
        self.assertEqual(c["profile_url"], "https://example.test/in/a")

    def test_rejects_missing_profile_url(self):
        self.assertIsNone(opener.shortlist_ok(person(1, "A", url=None), "Product Designer", []))

    def test_rejects_title_not_on_a_current_role(self):
        self.assertIsNone(opener.shortlist_ok(person(1, "A", title="Engineer"), "Product Designer", []))

    def test_rejects_missing_skill(self):
        self.assertIsNone(opener.shortlist_ok(person(1, "A"), "Product Designer", ["Prototyping"]))


class PostRuleTests(unittest.TestCase):
    def test_quotable_post_passes(self):
        self.assertTrue(opener.usable_post(post(), "2026-04-01"))

    def test_plain_repost_rejected(self):
        self.assertFalse(opener.usable_post(post(kind="repost_without_thoughts"), "2026-04-01"))

    def test_old_post_rejected(self):
        self.assertFalse(opener.usable_post(post(date="2026-01-01T00:00:00Z"), "2026-04-01"))

    def test_short_or_link_only_post_rejected(self):
        self.assertFalse(opener.usable_post(post(text="Great event! https://example.test/x #design #ux"), "2026-04-01"))

    def test_hashtags_do_not_count_as_words(self):
        tags = " ".join(f"#tag{i}" for i in range(30))
        self.assertFalse(opener.usable_post(post(text="Thanks all " + tags), "2026-04-01"))

    def test_pick_prefers_original_then_newest(self):
        quote = post(kind="repost_quote", date="2026-08-01T00:00:00Z")
        old = post(date="2026-06-01T00:00:00Z")
        new = post(date="2026-07-01T00:00:00Z")
        self.assertIs(opener.pick_post([quote, old, new], "2026-04-01"), new)

    def test_pick_none_when_nothing_usable(self):
        self.assertIsNone(opener.pick_post([post(text="short")], "2026-04-01"))


class QuoteTests(unittest.TestCase):
    def test_quote_cut_at_sentence_and_strips_links(self):
        q = opener.quote_from(LONG + " https://example.test/x", limit=120)
        self.assertTrue(q.endswith("?") or q.endswith("."))
        self.assertLessEqual(len(q), 120)
        self.assertNotIn("http", q)

    def test_fallback_opener_names_month(self):
        line = opener.fallback_opener("Ada", "one idea", "2026-07-10T08:00:00Z")
        self.assertIn("July 2026", line)
        self.assertIn("one idea", line)


class FakeClaude:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt, max_tokens=0):
        self.prompts.append(prompt)
        return "Cutting four screens to double activation is the kind of call I rarely see made."


class RunTests(unittest.TestCase):
    def make_transport(self, calls):
        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            if url.endswith("/person/search"):
                return {"profiles": [person(1, "Ada Lovelace", url="https://example.test/in/a"),
                                     person(2, "Bob Quiet", url="https://example.test/in/b"),
                                     person(3, "Eve Wrong", url="https://example.test/in/c", title="Engineer")],
                        "next_cursor": None}
            return {"posts": [post(url="https://example.test/in/a")], "next_cursor": None}
        return transport

    def test_run_uses_documented_endpoints_and_version(self):
        calls = []
        rows, shortlist = opener.run(Crustdata(api_key="test", transport=self.make_transport(calls)),
                                     "Product Designer", "London", [], "2026-04-01", 10, 10)
        self.assertEqual([c[0].rsplit("/", 2)[-2:] for c in calls], [["person", "search"], ["social_post", "search"]])
        self.assertEqual(calls[0][2]["x-api-version"], "2025-11-01")
        post_filter = calls[1][1]["filters"]["conditions"][0]
        self.assertEqual(post_filter["field"], "actor.professional_network_url")
        self.assertEqual(post_filter["value"], ["https://example.test/in/a", "https://example.test/in/b"])
        self.assertEqual(len(shortlist), 2)          # the wrong-title result is dropped in code
        self.assertEqual(len(rows), 1)               # the person with no post gets no row
        self.assertEqual(rows[0]["opener_source"], "template")
        self.assertIn("Hi Ada", rows[0]["opener"])

    def test_claude_opener_used_when_enabled(self):
        fake = FakeClaude()
        original = opener.ask_claude
        opener.ask_claude = fake
        try:
            rows, _ = opener.run(Crustdata(api_key="test", transport=self.make_transport([])),
                                 "Product Designer", "London", [], "2026-04-01", 10, 10, use_claude=True)
        finally:
            opener.ask_claude = original
        self.assertEqual(rows[0]["opener_source"], "claude")
        self.assertIn("Cutting four screens", fake.prompts[0])

    def test_claude_none_falls_back_to_template(self):
        original = opener.ask_claude
        opener.ask_claude = lambda *a, **k: None
        try:
            rows, _ = opener.run(Crustdata(api_key="test", transport=self.make_transport([])),
                                 "Product Designer", "London", [], "2026-04-01", 10, 10, use_claude=True)
        finally:
            opener.ask_claude = original
        self.assertEqual(rows[0]["opener_source"], "template")

    def test_summary_counts(self):
        rows, shortlist = opener.run(Crustdata(api_key="test", transport=self.make_transport([])),
                                     "Product Designer", "London", [], "2026-04-01", 10, 10)
        s = opener.summary(rows, shortlist)
        self.assertEqual(s["shortlist"], 2)
        self.assertEqual(s["with_quotable_post"], 1)
        self.assertEqual(s["no_post_in_window"], 1)
        self.assertEqual(s["quotable_share"], 0.5)
        self.assertEqual(s["picked_post_months"], {"2026-07": 1})


class OutputTests(unittest.TestCase):
    ROW = {"id": "1:2026-07-10", "name": "Ada Lovelace", "post_date": "2026-07-10",
           "opener": "Hi Ada, a line.", "post_url": "https://example.test/p/1"}

    def test_slack_text(self):
        text = opener.slack_text([self.ROW], "Product Designer")
        self.assertTrue(text.startswith(":writing_hand: 1 openers drafted for Product Designer"))
        self.assertIn("Hi Ada, a line.", text)

    def test_state_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(opener.only_new([self.ROW], path)), 1)
            opener.save_state([self.ROW], path)
            self.assertEqual(opener.only_new([self.ROW], path), [])
            with open(path) as fh:
                self.assertEqual(json.load(fh), ["1:2026-07-10"])

    def test_person_filters_use_documented_fields(self):
        f = opener.build_person_filters("Product Designer", "London")
        self.assertEqual([c["field"] for c in f["conditions"]],
                         ["experience.employment_details.current.title", "basic_profile.location.city"])


if __name__ == "__main__":
    unittest.main()
