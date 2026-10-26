"""Fail-closed membership review before moving articles into a story."""
from __future__ import annotations

import json
import logging
import re

from services.source_review import assess_copy, story_source_evidence

logger = logging.getLogger('sift-api.event_coherence')
_TEAMS = re.compile(r'\b(cardinals|falcons|ravens|bills|panthers|bears|bengals|browns|cowboys|broncos|lions|packers|texans|colts|jaguars|chiefs|raiders|chargers|rams|dolphins|vikings|patriots|saints|giants|jets|eagles|steelers|49ers|seahawks|buccaneers|titans|commanders)\b', re.I)


def conflicting_nfl_games(articles: list[dict]) -> bool:
    """Reject explicit different NFL matchups; ambiguity goes to semantic review."""
    games = []
    for article in articles:
        text = article.get('title', '') + ' ' + (article.get('summary') or '')
        teams = frozenset(m.casefold() for m in _TEAMS.findall(text))
        if len(teams) == 2 and re.search(r'\bNFL\b|\bWeek\s+\d+\b', text, re.I):
            week = re.search(r'\bWeek\s+(\d+)\b', text, re.I)
            games.append((teams, week.group(1) if week else None))
    return any(a != b or (wa and wb and wa != wb)
               for i, (a, wa) in enumerate(games) for b, wb in games[i + 1:])


async def same_event(articles: list[dict], *, client=None) -> bool | None:
    # No sampling: every proposed member must pass, including existing members
    # not returned by the candidate kNN query.
    if len(articles) < 2 or any(not (a.get('summary') or '').strip() for a in articles):
        return False
    if conflicting_nfl_games(articles):
        logger.info(json.dumps({'event': 'event_membership_rejected', 'reason': 'different NFL games', 'members': len(articles)}))
        return False
    verdict = (await assess_copy([{'id': 'membership', 'title': 'Proposed story membership',
        'source': story_source_evidence(articles), 'copy': 'Proposed event membership.',
        'review_mode': 'event_coherence'}], client=client)).get('membership', {})
    passed = verdict.get('v') == 'pass'
    if verdict.get('v') not in {'pass', 'retry', 'unavailable'}:
        return None
    logger.info(json.dumps({'event': 'event_membership_review', 'members': len(articles),
                           'approved': passed, 'reason': verdict.get('why', 'review unavailable')}))
    return passed
