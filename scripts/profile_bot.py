#!/usr/bin/env python3
"""Refresh the marker blocks in the profile README.

Sources: the portfolio RSS, the GitHub GraphQL/REST APIs, the public Lichess
daily puzzle and the public Sanity dataset. Every source is optional: if one
fails, the existing block is left alone.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
GENERATED = os.path.join(ROOT, "assets", "generated")

LOGIN = os.environ.get("GH_LOGIN", "Olamidipupo-favour")
TOKEN = (
    os.environ.get("PROFILE_TOKEN")
    or os.environ.get("GH_TOKEN")
    or os.environ.get("GITHUB_TOKEN")
    or ""
)
RSS_URL = "https://olamidipupo-favour.github.io/rss.xml"
SANITY_URL = "https://5qubhndt.api.sanity.io/v2021-10-21/data/query/production"
NOW_FALLBACK = "Building backend systems at Autogon AI, studying medicine, and taking on select remote work."
ACCENT = "#22C55E"

# GitHub's language detection picks up build files and mislabels a few repos.
# Keep the card about code people actually wrote.
LANGUAGE_NOISE = {
    "Dockerfile", "Makefile", "Mako", "Csound", "HTML", "CSS", "Shell",
    "Batchfile", "Procfile", "Nix", "Just", "CMake", "CoffeeScript",
}

GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        contributions { totalCount }
        repository { nameWithOwner isPrivate stargazerCount primaryLanguage { name } }
      }
    }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false,
                 privacy: PUBLIC, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name
        description
        stargazerCount
        pushedAt
        primaryLanguage { name }
        languages(first: 10) { edges { size node { name } } }
      }
    }
  }
}
"""


def log(message):
    print("[profile-bot] %s" % message, file=sys.stderr)


def get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "profile-bot/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def get_json(url, headers=None, timeout=20):
    return json.loads(get(url, headers=headers, timeout=timeout))


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "User-Agent": "profile-bot/1.0",
            "Authorization": "bearer " + TOKEN,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read())
    if payload.get("errors"):
        raise RuntimeError(payload["errors"][0].get("message", "GraphQL error"))
    return payload["data"]


def md(text):
    """Keep RSS titles from breaking the surrounding markdown."""
    return re.sub(r"([\[\]\\`*_<>])", r"\\\1", " ".join(text.split()))


# ---------------------------------------------------------------- blocks

def posts_block():
    try:
        root = ElementTree.fromstring(get(RSS_URL))
    except Exception as exc:  # noqa: BLE001 - any failure keeps the old block
        log("posts: %s" % exc)
        return None

    items = root.findall("./channel/item")[:5]
    if not items:
        return "_No notes published yet. The feed is live at [rss.xml](%s)._" % RSS_URL

    lines = []
    for item in items:
        title = md(item.findtext("title") or "Untitled")
        link = (item.findtext("link") or "").strip()
        published = item.findtext("pubDate")
        when = ""
        if published:
            try:
                when = " · " + datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %z").strftime("%d %b %Y")
            except ValueError:
                pass
        lines.append("- [%s](%s)%s" % (title, link, when))
    return "\n".join(lines)


def now_block():
    try:
        query = '*[_type == "siteSettings"][0].nowBuilding'
        data = get_json(SANITY_URL + "?" + urllib.parse.urlencode({"query": query}))
        value = (data.get("result") or "").strip()
        if value and not value.upper().startswith("[PLACEHOLDER"):
            return value
        log("now: Sanity still has a placeholder; using fallback")
    except Exception as exc:  # noqa: BLE001
        log("now: %s" % exc)
        return None
    return NOW_FALLBACK


def describe_event(event):
    kind = event.get("type")
    repo = (event.get("repo") or {}).get("name")
    payload = event.get("payload") or {}
    if not repo:
        return None

    if kind == "PushEvent":
        action = "pushed"
    elif kind == "PullRequestEvent":
        action = "%s a pull request in" % payload.get("action", "updated")
    elif kind == "PullRequestReviewEvent":
        action = "reviewed a pull request in"
    elif kind == "IssuesEvent":
        action = "%s an issue in" % payload.get("action", "updated")
    elif kind == "IssueCommentEvent":
        action = "commented in"
    elif kind == "CreateEvent":
        action = "created a %s in" % (payload.get("ref_type") or "ref")
    elif kind == "ReleaseEvent":
        action = "released %s in" % ((payload.get("release") or {}).get("tag_name") or "a release")
    elif kind == "ForkEvent":
        action = "forked"
    elif kind == "PublicEvent":
        action = "open-sourced"
    else:
        return None

    try:
        when = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).strftime("%d %b")
    except (KeyError, ValueError):
        when = ""
    return "- [%s](https://github.com/%s) — %s%s" % (
        md(repo), repo, action, (" · " + when) if when else ""
    )


def activity_block():
    if not TOKEN:
        log("activity: no token")
        return None
    try:
        events = get_json(
            "https://api.github.com/users/%s/events/public?per_page=30" % LOGIN,
            headers={"Authorization": "bearer " + TOKEN},
        )
    except Exception as exc:  # noqa: BLE001
        log("activity: %s" % exc)
        return None

    lines = []
    for event in events:
        line = describe_event(event)
        if line and line not in lines:
            lines.append(line)
        if len(lines) == 5:
            break
    return "\n".join(lines) or "_No public activity in the last 90 days._"


# ---------------------------------------------------------------- chess

PIECES = {
    "k": "\u265a", "q": "\u265b", "r": "\u265c", "b": "\u265d", "n": "\u265e", "p": "\u265f",
    "K": "\u265a", "Q": "\u265b", "R": "\u265c", "B": "\u265d", "N": "\u265e", "P": "\u265f",
}


def chess_svg(fen, rating, themes):
    try:
        placement, turn = fen.split(" ")[0], fen.split(" ")[1]
    except (IndexError, ValueError):
        raise ValueError("bad FEN")

    size, margin, top = 34, 16, 46
    board = size * 8
    width, height = board + margin * 2, board + top + 42
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-labelledby="title">' % (width, height, width, height),
        '<title id="title">Lichess daily puzzle. %s to move. Rating %s.</title>' % ("White" if turn == "w" else "Black", rating),
        '<rect width="%d" height="%d" rx="6" fill="#0B0F0B"/>' % (width, height),
        '<text x="%d" y="22" font-family="Arial, Helvetica, sans-serif" font-size="11" font-weight="700" letter-spacing="2" fill="%s">LICHESS DAILY</text>' % (margin, ACCENT),
        '<text x="%d" y="22" text-anchor="end" font-family="\'Courier New\', monospace" font-size="11" fill="#9CA3AF">RATING %s</text>' % (width - margin, rating),
        '<rect x="%d" y="%d" width="%d" height="%d" fill="#0B0F0B" stroke="%s" stroke-width="2"/>' % (margin - 2, top - 2, board + 4, board + 4, ACCENT),
    ]

    for rank in range(8):
        for file_index in range(8):
            dark = (rank + file_index) % 2 == 0
            parts.append(
                '<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                % (margin + file_index * size, top + rank * size, size, size,
                   "#2E5B43" if dark else "#DDE6D8")
            )

    rank_index = 0
    for row in placement.split("/"):
        file_index = 0
        for char in row:
            if char.isdigit():
                file_index += int(char)
                continue
            piece = PIECES.get(char)
            if piece:
                white = char.isupper()
                x = margin + file_index * size + size / 2
                y = top + rank_index * size + size * 0.72
                parts.append(
                    '<text x="%.1f" y="%.1f" text-anchor="middle" font-size="27" '
                    'font-family="\'Segoe UI Symbol\', \'Noto Sans Symbols 2\', sans-serif" '
                    'fill="%s" stroke="%s" stroke-width="1.2" paint-order="stroke">%s</text>'
                    % (x, y, "#F7F8F3" if white else "#101410",
                       "#101410" if white else "#F7F8F3", piece)
                )
            file_index += 1
        rank_index += 1

    files = "abcdefgh"
    for index, letter in enumerate(files):
        parts.append(
            '<text x="%d" y="%d" text-anchor="middle" font-family="\'Courier New\', monospace" font-size="9" fill="#6B7280">%s</text>'
            % (margin + index * size + size / 2, top + board + 14, letter)
        )

    theme = themes[0] if themes else "puzzle"
    parts.append(
        '<text x="%d" y="%d" font-family="Arial, Helvetica, sans-serif" font-size="11" fill="#E9EFE8">%s to move</text>'
        % (margin, top + board + 32, "White" if turn == "w" else "Black")
    )
    parts.append(
        '<text x="%d" y="%d" text-anchor="end" font-family="\'Courier New\', monospace" font-size="10" fill="%s">%s</text>'
        % (width - margin, top + board + 32, ACCENT, escape(str(theme)))
    )
    parts.append("</svg>")
    return "\n".join(parts)


def chess_block():
    try:
        data = get_json("https://lichess.org/api/puzzle/daily")
        puzzle = data["puzzle"]
        svg = chess_svg(puzzle["fen"], puzzle.get("rating", "?"), puzzle.get("themes") or [])
    except Exception as exc:  # noqa: BLE001
        log("chess: %s" % exc)
        return None

    write(os.path.join(GENERATED, "chess.svg"), svg)
    url = "https://lichess.org/training/%s" % puzzle["id"]
    turn = "White" if puzzle["fen"].split()[1] == "w" else "Black"
    return (
        '<a href="%s"><img src="assets/generated/chess.svg" '
        'alt="Chess board for the Lichess daily puzzle. %s to move. Rating %s." width="300"></a>\n\n'
        "Daily puzzle · rating %s · [Play on Lichess →](%s)"
        % (url, turn, puzzle.get("rating", "?"), puzzle.get("rating", "?"), url)
    )


# ---------------------------------------------------------------- stats

def flatten_days(calendar):
    return [day for week in calendar.get("weeks", []) for day in week.get("contributionDays", [])]


def streak_stats(days):
    today = datetime.now(timezone.utc).date()
    past = []
    for day in days:
        try:
            date = datetime.strptime(day["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if date <= today:
            past.append((date, int(day.get("contributionCount") or 0)))
    past.sort()

    current = 0
    expected = today
    if not past or past[-1][0] != today or past[-1][1] == 0:
        expected = today.fromordinal(today.toordinal() - 1)
    for date, count in reversed(past):
        if date > expected:
            continue
        if date == expected and count > 0:
            current += 1
            expected = expected.fromordinal(expected.toordinal() - 1)
        else:
            break

    longest = run = 0
    previous = None
    for date, count in past:
        if count > 0 and previous and (date - previous).days == 1:
            run += 1
        elif count > 0:
            run = 1
        else:
            run = 0
        longest = max(longest, run)
        previous = date
    return current, longest


def languages(repos):
    totals = {}
    for repo in repos:
        for edge in ((repo.get("languages") or {}).get("edges") or []):
            name = (edge.get("node") or {}).get("name")
            if not name or name in LANGUAGE_NOISE:
                continue
            totals[name] = totals.get(name, 0) + int(edge.get("size") or 0)
    grand = sum(totals.values()) or 1
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:5]
    return [(name, size * 100.0 / grand) for name, size in ranked]


def top_repos(contributions):
    owned = []
    for item in contributions or []:
        repo = item.get("repository") or {}
        name = repo.get("nameWithOwner") or ""
        if repo.get("isPrivate") or not name.startswith(LOGIN + "/"):
            continue
        owned.append((name.split("/", 1)[1], int((item.get("contributions") or {}).get("totalCount") or 0)))
    owned.sort(key=lambda item: item[1], reverse=True)
    return owned[:4]


def stats_svg(theme, commit_count, total_contributions, current_streak, longest_streak, langs, repos):
    if theme == "dark":
        bg, panel, line, ink, muted = "#0B0F0B", "#111711", "#233026", "#E9EFE8", "#9CA3AF"
    else:
        bg, panel, line, ink, muted = "#F7F8F3", "#FFFFFF", "#DDE3DA", "#101410", "#5B665C"

    width, height = 920, 300
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-labelledby="title">' % (width, height, width, height),
        '<title id="title">GitHub stats for %s: languages, commits, streak and most active repositories.</title>' % LOGIN,
        '<defs><pattern id="dots" width="12" height="12" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1.6" fill="%s" opacity="0.5"/></pattern></defs>' % ACCENT,
        '<rect width="%d" height="%d" fill="%s"/>' % (width, height, bg),
    ]

    def panel_open(index, label):
        x = 16 + index * 226
        return [
            '<rect x="%d" y="16" width="210" height="252" rx="6" fill="%s" stroke="%s"/>' % (x, panel, line),
            '<text x="%d" y="42" font-family="\'Courier New\', monospace" font-size="10" font-weight="700" letter-spacing="1.6" fill="%s">%s</text>' % (x + 14, ACCENT, label),
            '<line x1="%d" y1="54" x2="%d" y2="54" stroke="%s"/>' % (x + 14, x + 196, line),
        ]

    # Languages
    out += panel_open(0, "LANGUAGES")
    left, right = 30, 212
    for index, (name, pct) in enumerate(langs or [("No data", 0)]):
        y = 78 + index * 36
        out.append('<text x="%d" y="%d" font-family="Arial, Helvetica, sans-serif" font-size="11" font-weight="700" fill="%s">%s</text>' % (left, y, ink, escape(name)))
        out.append('<text x="%d" y="%d" text-anchor="end" font-family="\'Courier New\', monospace" font-size="10" fill="%s">%.0f%%</text>' % (right, y, muted, pct))
        out.append('<rect x="%d" y="%d" width="%d" height="4" rx="2" fill="%s"/>' % (left, y + 5, right - left, line))
        out.append('<rect x="%d" y="%d" width="%.1f" height="4" rx="2" fill="%s"/>' % (left, y + 5, (right - left) * pct / 100.0, ACCENT))

    # Commits
    out += panel_open(1, "COMMITS")
    left, right = 256, 438
    out.append('<text x="%d" y="124" font-family="Arial, Helvetica, sans-serif" font-size="46" font-weight="900" fill="%s">%s</text>' % (left, ink, format(commit_count, ",")))
    out.append('<text x="%d" y="146" font-family="Arial, Helvetica, sans-serif" font-size="11" fill="%s">public commits · 365 days</text>' % (left, muted))
    out.append('<line x1="%d" y1="164" x2="%d" y2="164" stroke="%s"/>' % (left, right, line))
    out.append('<text x="%d" y="190" font-family="Arial, Helvetica, sans-serif" font-size="11" fill="%s">%s total contributions</text>' % (left, ink, format(total_contributions, ",")))
    out.append('<text x="%d" y="208" font-family="Arial, Helvetica, sans-serif" font-size="10" fill="%s">public and private</text>' % (left, muted))
    out.append('<text x="%d" y="238" font-family="\'Courier New\', monospace" font-size="9" fill="%s">LAST 365 DAYS</text>' % (left, muted))

    # Streak
    out += panel_open(2, "STREAK")
    left, right = 482, 664
    out.append('<text x="%d" y="124" font-family="Arial, Helvetica, sans-serif" font-size="46" font-weight="900" fill="%s">%d</text>' % (left, ink, current_streak))
    out.append('<text x="%d" y="146" font-family="Arial, Helvetica, sans-serif" font-size="11" fill="%s">days, current</text>' % (left, muted))
    out.append('<line x1="%d" y1="164" x2="%d" y2="164" stroke="%s"/>' % (left, right, line))
    out.append('<text x="%d" y="196" font-family="Arial, Helvetica, sans-serif" font-size="26" font-weight="900" fill="%s">%d</text>' % (left, ACCENT, longest_streak))
    out.append('<text x="%d" y="214" font-family="Arial, Helvetica, sans-serif" font-size="11" fill="%s">days, longest</text>' % (left, muted))

    # Top repos
    out += panel_open(3, "TOP REPOS")
    left = 708
    for index, (name, count) in enumerate(repos or [("No data", 0)]):
        y = 78 + index * 42
        short = "portfolio" if name.endswith(".github.io") else name
        short = short if len(short) <= 24 else short[:23] + "…"
        out.append('<text x="%d" y="%d" font-family="\'Courier New\', monospace" font-size="10" font-weight="700" fill="%s">%s</text>' % (left, y, ink, escape(short)))
        out.append('<text x="%d" y="%d" font-family="Arial, Helvetica, sans-serif" font-size="9" fill="%s">%s commits</text>' % (left, y + 14, muted, format(count, ",")))

    out.append('<rect x="0" y="%d" width="%d" height="14" fill="url(#dots)" opacity="0.35"/>' % (height - 14, width))
    out.append("</svg>")
    return "\n".join(out)


def stats_block():
    if not TOKEN:
        log("stats: no token")
        return None
    try:
        user = graphql(GRAPHQL_QUERY, {"login": LOGIN})["user"]
        calendar = user["contributionsCollection"]
        days = flatten_days(calendar["contributionCalendar"])
        current, longest = streak_stats(days)
        langs = languages(user["repositories"]["nodes"])
        repos = top_repos(calendar.get("commitContributionsByRepository"))
        commits = int(calendar.get("totalCommitContributions") or 0)
        total = int(calendar["contributionCalendar"].get("totalContributions") or 0)
    except Exception as exc:  # noqa: BLE001
        log("stats: %s" % exc)
        return None

    for theme in ("light", "dark"):
        write(
            os.path.join(GENERATED, "stats-%s.svg" % theme),
            stats_svg(theme, commits, total, current, longest, langs, repos),
        )

    return (
        '<p align="center">\n'
        '  <picture>\n'
        '    <source media="(prefers-color-scheme: dark)" srcset="assets/generated/stats-dark.svg">\n'
        '    <img src="assets/generated/stats-light.svg" alt="GitHub stats: top languages, public commits in the last year, current and longest streak, and most active repositories." width="100%">\n'
        '  </picture>\n'
        '</p>'
    )


# ---------------------------------------------------------------- main

BLOCK_PATTERN = r"(<!-- BLOCK:%s -->)(.*?)(<!-- /BLOCK:%s -->)"


def replace_block(text, name, content):
    pattern = re.compile(BLOCK_PATTERN % (name, name), re.S)
    if not pattern.search(text):
        log("missing block: %s" % name)
        return text
    return pattern.sub(lambda match: match.group(1) + "\n" + content.strip() + "\n" + match.group(3), text, count=1)


def main():
    with open(README, encoding="utf-8") as handle:
        original = handle.read()

    text = original
    for name, builder in (
        ("posts", posts_block),
        ("now", now_block),
        ("activity", activity_block),
        ("chess", chess_block),
        ("stats", stats_block),
    ):
        try:
            content = builder()
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
            log("%s: %s" % (name, exc))
            content = None
        if content:
            text = replace_block(text, name, content)

    if text != original:
        write(README, text)
        log("README updated")
    else:
        log("README unchanged")


if __name__ == "__main__":
    main()
