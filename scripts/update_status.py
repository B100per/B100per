#!/usr/bin/env python3
"""Rewrite the numbers in assets/status.svg from real GitHub data.

Counts PRIVATE work too, which for this account is nearly all of it. That needs
a PAT with `read:user` in GH_PAT -- the REST search API only indexes public
repos, so it reported 0 commits/month against ~300 real ones. Contribution
totals come from the GraphQL contributionsCollection instead.

Two things must both be true or private commits stay invisible:
  1. GH_PAT is a PAT with `read:user` (the built-in GITHUB_TOKEN cannot read
     contributions across other orgs' private repos).
  2. "Include private contributions on my profile" is ON in GitHub settings.
     If it is off, GitHub itself returns zeros and no token can fix it.

If the API can't answer for a given stat, that stat keeps whatever is already
in the SVG; a bad API day must never blank the card to zeros.

    python3 scripts/update_status.py            # fetch + rewrite
    python3 scripts/update_status.py --demo     # offline self-check, no network

STAT MAP
    Zombies Killed  commits authored, all time (public + private)
    HEALTH          repos owned (public + private)
    ADRENALINE      commits in the last 30 days
    MORALE          contributions this year
    Accuracy        PR reviews given
    Reload Speed    commits in the last 7 days
    Endurance       account age, months
    Strength        repos contributed to
    Agility         distinct languages x 11
    Wits            PRs opened
    Morale (skill)  followers
    Luck            issues opened
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

USER = os.environ.get("GH_USER", "B100per")
SVG = os.path.join(os.path.dirname(__file__), "..", "assets", "status.svg")
API = "https://api.github.com"

# bar track geometry, copied from the hand-drawn card
BAR_X, BAR_W, TAIL_W = 220, 360, 40

# scaled for private commits being counted -- roughly 300/month for this account
RANKS = [(10000, "LEGEND"), (5000, "VETERAN"), (2000, "HARDENED"),
         (500, "SURVIVOR"), (100, "SCAVENGER"), (0, "GREENHORN")]


def token():
    return os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or ""


def gql(query, **variables):
    """POST a GraphQL query, or None if GitHub won't answer."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API + "/graphql", data=body, headers={
        "Authorization": "Bearer " + token(),
        "User-Agent": USER,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        print("  ! graphql -> %s" % e, file=sys.stderr)
        return None
    if data.get("errors"):
        print("  ! graphql -> %s" % data["errors"], file=sys.stderr)
        return None
    return data.get("data", {}).get("viewer")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# contributionsCollection accepts at most a 1-year window, so long spans are
# walked a year at a time and summed.
WINDOW = """
query($from:DateTime!,$to:DateTime!){ viewer { contributionsCollection(from:$from,to:$to){
  totalCommitContributions restrictedContributionsCount
  totalPullRequestContributions totalIssueContributions
  totalPullRequestReviewContributions
  contributionCalendar { totalContributions } } } }
"""

PROFILE = """
query{ viewer {
  createdAt
  followers { totalCount }
  repositories(ownerAffiliations:OWNER) { totalCount }
  repositoriesContributedTo(includeUserRepositories:true,
    contributionTypes:[COMMIT,PULL_REQUEST,ISSUE,REPOSITORY]) { totalCount }
  topRepositories(orderBy:{field:PUSHED_AT,direction:DESC}, first:100) {
    nodes { primaryLanguage { name } } } } }
"""


def window(start, end):
    """Contribution totals for one span, or None. Private commits land in
    restrictedContributionsCount when the PAT can't see the repo itself."""
    total = {}
    while start < end:
        stop = min(start.replace(year=start.year + 1), end)
        c = gql(WINDOW, **{"from": iso(start), "to": iso(stop)})
        if c is None:
            return None
        c = c["contributionsCollection"]
        total["commits"] = total.get("commits", 0) + \
            c["totalCommitContributions"] + c["restrictedContributionsCount"]
        total["prs"] = total.get("prs", 0) + c["totalPullRequestContributions"]
        total["issues"] = total.get("issues", 0) + c["totalIssueContributions"]
        total["reviews"] = total.get("reviews", 0) + \
            c["totalPullRequestReviewContributions"]
        total["calendar"] = total.get("calendar", 0) + \
            c["contributionCalendar"]["totalContributions"]
        start = stop
    return total


def collect():
    """Return {stat: value}, omitting anything the API refused."""
    s = {}
    now = datetime.now(timezone.utc)

    profile = gql(PROFILE)
    if profile:
        born = datetime.strptime(profile["createdAt"], "%Y-%m-%dT%H:%M:%SZ")
        born = born.replace(tzinfo=timezone.utc)
        s["months"] = (now - born).days // 30
        s["followers"] = profile["followers"]["totalCount"]
        s["repos"] = profile["repositories"]["totalCount"]
        s["contributed"] = profile["repositoriesContributedTo"]["totalCount"]
        s["langs"] = len({n["primaryLanguage"]["name"]
                          for n in profile["topRepositories"]["nodes"]
                          if n["primaryLanguage"]})

        lifetime = window(born, now)
        if lifetime:
            s["commits"] = lifetime["commits"]
            s["prs"] = lifetime["prs"]
            s["issues"] = lifetime["issues"]
            s["reviews"] = lifetime["reviews"]

    year = window(now.replace(month=1, day=1, hour=0, minute=0, second=0), now)
    if year:
        s["contrib_year"] = year["calendar"]
    for key, days in (("c30", 30), ("c7", 7)):
        recent = window(now - timedelta(days=days), now)
        if recent:
            s[key] = recent["commits"]

    return {k: v for k, v in s.items() if v is not None}


def ceiling(v):
    """Next round number above v, so a bar never reads as completely full."""
    for n in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 50000):
        if v < n:
            return n
    return v * 2


def set_text(svg, el_id, value):
    return re.sub(r'(<text id="%s"[^>]*>)[^<]*(</text>)' % el_id,
                  lambda m: m.group(1) + str(value) + m.group(2), svg)


def set_attr(svg, el_id, attr, value):
    return re.sub(r'(<rect id="%s"[^>]*\b%s=")[^"]*(")' % (el_id, attr),
                  lambda m: m.group(1) + str(value) + m.group(2), svg)


def set_bar(svg, bar_id, clip_id, val_id, value, tail_id=None):
    """Fill one HUD bar: rect width, sheen clip, and the `n / max` readout."""
    top = ceiling(value)
    width = max(0, min(BAR_W, round(BAR_W * value / top)))
    svg = set_attr(svg, bar_id, "width", width)
    tail = 0
    if tail_id:
        tail = min(TAIL_W, BAR_W - width)
        svg = set_attr(svg, tail_id, "x", BAR_X + width)
        svg = set_attr(svg, tail_id, "width", tail)
    svg = set_attr(svg, clip_id, "width", width + tail)
    return set_text(svg, val_id, "{:,} / {:,}".format(value, top))


def render(svg, s):
    """Apply every stat we actually have. Missing stats leave the SVG alone."""
    if "commits" in s:
        svg = set_text(svg, "kills", "Zombies Killed  {:,}".format(s["commits"]))
        svg = set_text(svg, "rank", "RANK: " + next(
            name for floor, name in RANKS if s["commits"] >= floor))
    if "repos" in s:
        svg = set_bar(svg, "hpBar", "clipHPRect", "hpVal", s["repos"], "hpTail")
    if "c30" in s:
        svg = set_bar(svg, "adrBar", "clipADRRect", "adrVal", s["c30"])
    if "contrib_year" in s:
        svg = set_bar(svg, "morBar", "clipMORRect", "morVal", s["contrib_year"])

    skills = [
        ("sk1", s.get("reviews")),
        ("sk2", s.get("c7")),
        ("sk3", s.get("months")),
        ("sk4", s.get("contributed")),
        ("sk5", s["langs"] * 11 if "langs" in s else None),
        ("sk6", s.get("prs")),
        ("sk7", s.get("followers")),
        ("sk8", s.get("issues")),
    ]
    for el_id, value in skills:
        if value is not None:
            svg = set_text(svg, el_id, min(99, value))
    return svg


def demo():
    """Self-check: no network, asserts the substitutions actually land."""
    with open(SVG, encoding="utf-8") as f:
        before = f.read()
    s = {"commits": 3200, "repos": 32, "c30": 286, "c7": 40, "contrib_year": 900,
         "langs": 4, "prs": 8, "issues": 5, "reviews": 3, "followers": 7,
         "months": 50, "contributed": 6}
    out = render(before, s)

    assert "Zombies Killed  3,200" in out
    assert "RANK: HARDENED" in out                      # 3,200 commits
    assert '<text id="hpVal" x="580" y="163" dx="60">32 / 50</text>' in out
    assert '<text id="adrVal" x="580" y="201" dx="60">286 / 500</text>' in out
    assert '<text id="sk5" x="760" y="332" text-anchor="end" fill="#E8A33D">44</text>' in out
    assert '<text id="sk3" x="360" y="396" text-anchor="end" fill="#E8A33D">50</text>' in out
    # skills cap at 99 so two digits never overflow the column
    assert '<text id="sk2" x="360" y="364" text-anchor="end" fill="#E8A33D">40</text>' in out
    assert '99' in render(before, {"c7": 4000})

    # bar geometry: 32/50 of a 360px track, plus the blinking temp-health tail
    assert '<rect id="hpBar" x="220" y="150" width="230"' in out
    assert '<rect id="hpTail" x="450" y="150" width="40"' in out
    assert '<rect id="clipHPRect" x="220" y="150" width="270"' in out

    # a bad API day must change nothing at all
    assert render(before, {}) == before
    # ...but an honest zero is a value, not a missing fetch
    zeroed = render(before, {"reviews": 0})
    assert '<text id="sk1" x="360" y="332" text-anchor="end" fill="#E8A33D">0</text>' in zeroed

    # bar + tail must never run off the end of the track, at any repo count
    for n in (0, 1, 9, 10, 26, 99, 100, 251, 99999):
        out = render(before, {"repos": n})
        bar = int(re.search(r'<rect id="hpBar"[^>]*width="(\d+)"', out).group(1))
        tail = re.search(r'<rect id="hpTail" x="(\d+)"[^>]*width="(\d+)"', out)
        assert 0 <= bar <= BAR_W, n
        assert int(tail.group(1)) == BAR_X + bar, n
        assert bar + int(tail.group(2)) <= BAR_W, n

    print("demo ok")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
        sys.exit(0)

    stats = collect()
    print("stats: " + json.dumps(stats, sort_keys=True))
    if not stats:
        sys.exit("no stats retrieved; leaving the card untouched")

    # Private repos exist but nothing is being counted -- GitHub is hiding the
    # contributions, and no token can override that. Loud, but not fatal.
    if stats.get("repos", 0) > 3 and stats.get("c30") == 0:
        print("\n  WARNING: %d repos but 0 contributions counted.\n"
              "  Turn ON 'Include private contributions on my profile' at\n"
              "  https://github.com/settings/profile\n" % stats["repos"],
              file=sys.stderr)

    with open(SVG, encoding="utf-8") as f:
        original = f.read()
    updated = render(original, stats)
    if updated == original:
        print("no change")
    else:
        with open(SVG, "w", encoding="utf-8") as f:
            f.write(updated)
        print("updated " + SVG)
