#!/usr/bin/env python3
"""Outreach Opener Writer.

Finds a shortlist for a role (title, skills, city), pulls each person's recent posts,
picks one real thing they said, and drafts the first line of a recruiting message
around it. Claude writes the line when configured; a plain template is the fallback.
Data layer: Crustdata POST /person/search and POST /social_post/search
(https://docs.crustdata.com).

  build_person_filters / build_post_filters -> the two documented calls, filters in code
  shortlist_ok / usable_post                -> deterministic rules (unit tested)
  pick_post                                 -> one post per person
  draft_opener                              -> Claude, or a template fallback
  summary()                                 -> aggregate numbers (safe to show in a README)
  slack_text -> the digest; send_slack() respects --dry-run
  only_new / save_state -> draft each post once
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from crustdata_api import Crustdata, day, load_env
from notify import ask_claude, claude_available, send_slack

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.headline",
    "social_handles",
    "skills.professional_network_skills",
    "experience.employment_details.current",
]
POST_FIELDS = [
    "text", "date_posted", "post_type", "hashtags", "share_url",
    "actor.professional_network_url", "engagement.total_reactions",
]
URL_CHUNK = 50          # profile URLs per post search
MIN_WORDS = 12          # a post needs this many real words to quote from
MAX_QUOTE = 220         # characters kept from the post

URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"[#@]\w+")


def build_person_filters(title: str, city: str) -> Dict[str, Any]:
    """Shortlist: current title (exact stored value) in one city. Skills are checked in code."""
    conds = [{"field": "experience.employment_details.current.title", "type": "=", "value": title}]
    if city:
        conds.append({"field": "basic_profile.location.city", "type": "=", "value": city})
    return {"op": "and", "conditions": conds}


def build_post_filters(profile_urls: List[str], since: str) -> Dict[str, Any]:
    return {"op": "and", "conditions": [
        {"field": "actor.professional_network_url", "type": "in", "value": profile_urls},
        {"field": "date_posted", "type": "=>", "value": since},
    ]}


def profile_url(item: Dict[str, Any]) -> str:
    handles = item.get("social_handles") or {}
    return ((handles.get("professional_network_identifier") or {}).get("profile_url")) or ""


def shortlist_ok(item: Dict[str, Any], title: str, skills: List[str]) -> Optional[Dict[str, Any]]:
    """Return a flat candidate or None. Rules: has a profile URL; one CURRENT role holds the
    title (nested filters can match different roles); lists every required skill."""
    url = profile_url(item)
    if not url:
        return None
    current = ((item.get("experience") or {}).get("employment_details") or {}).get("current") or []
    if not any(title.lower() in (role.get("title") or "").lower() for role in current):
        return None
    have = {s.lower() for s in ((item.get("skills") or {}).get("professional_network_skills") or [])}
    if any(s.lower() not in have for s in skills):
        return None
    profile = item.get("basic_profile") or {}
    return {"person_id": item.get("crustdata_person_id"), "name": profile.get("name", ""),
            "headline": profile.get("headline", ""), "profile_url": url}


def clean_text(text: str) -> str:
    """Drop links, then tidy whitespace."""
    return re.sub(r"\s+", " ", URL_RE.sub("", text or "")).strip()


def real_words(text: str) -> int:
    return len([w for w in TAG_RE.sub("", clean_text(text)).split() if any(c.isalpha() for c in w)])


def usable_post(post: Dict[str, Any], since: str) -> bool:
    """Quotable means: inside the window, the person's own words (a plain repost has none),
    and enough real text once links, hashtags and mentions are stripped."""
    if post.get("post_type") == "repost_without_thoughts":
        return False
    if day(post.get("date_posted")) < since:
        return False
    return real_words(post.get("text", "")) >= MIN_WORDS


def pick_post(posts: Iterable[Dict[str, Any]], since: str) -> Optional[Dict[str, Any]]:
    """One post per person: originals over quote-reposts, then the newest."""
    ok = [p for p in posts if usable_post(p, since)]
    if not ok:
        return None
    return max(ok, key=lambda p: (p.get("post_type") == "original", p.get("date_posted") or ""))


def quote_from(text: str, limit: int = MAX_QUOTE) -> str:
    """The opening of the post, cut at a sentence end (or a word) under `limit`."""
    text = TAG_RE.sub("", clean_text(text)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", cut)]
    if ends and ends[-1] >= limit // 2:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].strip() + "..."


def month_label(date: str) -> str:
    try:
        return dt.date.fromisoformat(day(date)).strftime("%B %Y")
    except ValueError:
        return ""


def fallback_opener(first_name: str, quote: str, date: str) -> str:
    when = month_label(date)
    return (f"Hi {first_name}, your post in {when} stuck with me: \"{quote}\"" if when
            else f"Hi {first_name}, your recent post stuck with me: \"{quote}\"")


def opener_prompt(name: str, role: str, quote: str, date: str) -> str:
    return (
        f"Write the first line of a recruiting message to {name} for a {role} role.\n"
        f"Build it on this one thing they wrote on {day(date)}:\n\"{quote}\"\n\n"
        "Rules: one or two sentences, under 40 words. Refer to a specific idea in their words. "
        "No flattery, no 'I came across your profile', no mention of the role or company yet, "
        "no emoji. Output the line only."
    )


def draft_opener(cand: Dict[str, Any], role: str, use_claude: bool) -> Dict[str, str]:
    first = (cand["name"].split() or ["there"])[0]
    if use_claude:
        line = ask_claude(opener_prompt(cand["name"], role, cand["quote"], cand["post_date"]), max_tokens=400)
        if line and len(line) <= 400:
            return {"opener": line.strip().strip('"'), "opener_source": "claude"}
    return {"opener": fallback_opener(first, cand["quote"], cand["post_date"]), "opener_source": "template"}


def run(client: Crustdata, title: str, city: str, skills: List[str], since: str,
        shortlist_size: int, max_posts: int, use_claude: bool = False):
    """Returns (rows, shortlist). Rows are candidates with a usable post and a drafted opener."""
    shortlist: List[Dict[str, Any]] = []
    seen = set()
    for item in client.person_search(build_person_filters(title, city), PERSON_FIELDS, max_results=shortlist_size):
        cand = shortlist_ok(item, title, skills)
        if cand and cand["profile_url"] not in seen:
            seen.add(cand["profile_url"])
            shortlist.append(cand)

    by_url: Dict[str, List[Dict[str, Any]]] = {}
    urls = [c["profile_url"] for c in shortlist]
    for i in range(0, len(urls), URL_CHUNK):
        chunk = urls[i:i + URL_CHUNK]
        for post in client.social_post_search(build_post_filters(chunk, since), POST_FIELDS,
                                              max_results=max_posts,
                                              sorts=[{"field": "date_posted", "order": "desc"}]):
            author = (post.get("actor") or {}).get("professional_network_url")
            by_url.setdefault(author, []).append(post)

    rows = []
    for cand in shortlist:
        posts = by_url.get(cand["profile_url"], [])
        cand["posts_found"] = len(posts)
        post = pick_post(posts, since)
        if not post:
            continue
        row = dict(cand, quote=quote_from(post["text"]), post_date=day(post.get("date_posted")),
                   post_url=post.get("share_url", ""), post_type=post.get("post_type", ""))
        row["id"] = f"{cand['person_id']}:{row['post_date']}"
        row.update(draft_opener(row, title, use_claude))
        rows.append(row)
    return rows, shortlist


def summary(rows: List[Dict[str, Any]], shortlist: List[Dict[str, Any]]) -> Dict[str, Any]:
    posted = [c for c in shortlist if c.get("posts_found")]
    months = Counter(r["post_date"][:7] for r in rows)
    kinds = Counter(r["post_type"] for r in rows)
    return {
        "shortlist": len(shortlist),
        "with_any_post_in_window": len(posted),
        "with_quotable_post": len(rows),
        "no_post_in_window": len(shortlist) - len(posted),
        "quotable_share": round(len(rows) / len(shortlist), 2) if shortlist else 0,
        "picked_post_types": dict(sorted(kinds.items())),
        "picked_post_months": dict(sorted(months.items())),
    }


def slack_text(rows: List[Dict[str, Any]], role: str, limit: int = 10) -> str:
    lines = [f":writing_hand: {len(rows)} openers drafted for {role}"]
    for r in rows[:limit]:
        lines.append(f"• *{r['name']}* ({r['post_date']}): {r['opener']}\n  {r['post_url']}")
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more in out/openers.json")
    return "\n".join(lines)


def load_seen(state_path):
    if not os.path.exists(state_path):
        return set()
    with open(state_path) as fh:
        return set(json.load(fh))


def only_new(rows, state_path):
    seen = load_seen(state_path)
    return [r for r in rows if str(r["id"]) not in seen]


def save_state(rows, state_path):
    seen = load_seen(state_path)
    seen.update(str(r["id"]) for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Outreach Opener Writer (Crustdata data layer)")
    p.add_argument("--title", default=os.environ.get("ROLE_TITLE", "Product Designer"))
    p.add_argument("--city", default=os.environ.get("ROLE_CITY", "London"))
    p.add_argument("--skills", default=os.environ.get("ROLE_SKILLS", ""), help="comma-separated, all required")
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "180")))
    p.add_argument("--shortlist-size", type=int, default=50)
    p.add_argument("--max-posts", type=int, default=200)
    p.add_argument("--claude", action="store_true", help="let Claude write the opener (needs ANTHROPIC_API_KEY)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    skills = [s.strip() for s in args.skills.split(",") if s.strip()]
    use_claude = args.claude and claude_available()
    rows, shortlist = run(Crustdata(), args.title, args.city, skills, since,
                          args.shortlist_size, args.max_posts, use_claude)
    stats = summary(rows, shortlist)
    if not args.all:
        rows = only_new(rows, args.state)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "openers.json"), "w") as fh:
        json.dump({"summary": stats, "rows": rows}, fh, indent=1)
    print(json.dumps(stats, indent=1))
    if rows:
        print(send_slack(slack_text(rows, args.title), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(rows, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
