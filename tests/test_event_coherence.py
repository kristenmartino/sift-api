from unittest.mock import AsyncMock

import pytest

from services import event_coherence as ec
from workflows import incremental_threading as it


def article(aid, title, outlet='AP', summary='Reported event.'):
    return {'id': aid, 'title': title, 'source_name': outlet, 'summary': summary,
            'source_url': 'https://example.com/'+aid, 'published_date': None, 'category': 'sports'}


@pytest.mark.asyncio
async def test_different_nfl_games_cannot_bridge_through_a_weekly_topic(monkeypatch):
    review=AsyncMock()
    monkeypatch.setattr(ec, 'assess_copy', review)
    a=article('a','Giants defeat Texans in NFL Week 7')
    b=article('b','Chiefs face Seahawks in NFL Week 7','Reuters')
    assert not await ec.same_event([a,b])
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_pair_in_different_weeks_is_not_the_same_event(monkeypatch):
    review=AsyncMock()
    monkeypatch.setattr(ec, 'assess_copy', review)
    assert not await ec.same_event([article('a','Giants face Texans in Week 6'),article('b','Giants face Texans in Week 7')])
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict',['retry','unavailable','error',None])
async def test_unproved_membership_fails_closed(monkeypatch,verdict):
    monkeypatch.setattr(ec,'assess_copy',AsyncMock(return_value={'membership':{'v':verdict}}))
    result = await ec.same_event([article('a','Giants defeat Texans'),article('b','Giants win in overtime','Reuters')])
    assert result is (None if verdict in ['error', None] else False)


@pytest.mark.asyncio
async def test_members_beyond_candidate_sample_are_all_reviewed(monkeypatch):
    review=AsyncMock(return_value={'membership':{'v':'pass'}})
    monkeypatch.setattr(ec,'assess_copy',review)
    members=[article(str(i),f'Coverage {i}') for i in range(12)]
    assert await ec.same_event(members)
    item=review.call_args.args[0][0]
    assert item['review_mode']=='event_coherence'
    assert 'Coverage 11' in item['source'] and item['source'].count('[AP]')==12


@pytest.mark.asyncio
async def test_bad_attach_does_not_move_article_or_mutate_healthy_story(monkeypatch):
    prior=[article('old','Patriots vs Bears touchdown')]
    pool=AsyncMock()
    pool.fetch.return_value=prior
    gate=AsyncMock(return_value=False)
    monkeypatch.setattr(it,'same_event',gate)
    synth=AsyncMock()
    incoming=article('new','Giants vs Texans result','Reuters')
    assert await it._attach(pool,incoming,'s',synth)=='attachment_rejected_event'
    assert gate.call_args.args[0]==prior+[incoming]
    assert all('UPDATE' not in c.args[0] for c in pool.fetch.call_args_list)
    pool.execute.assert_not_awaited()
    synth.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_creation_cannot_store_or_reassign_a_mixed_group(monkeypatch):
    members=[article('a','Giants vs Texans'),article('b','Chiefs vs Seahawks','Reuters')]
    pool=AsyncMock()
    pool.fetch.return_value=members
    monkeypatch.setattr(it,'same_event',AsyncMock(return_value=False))
    synth=AsyncMock()
    assert await it._create(pool,members[0],['b'],synth)=='creation_rejected_event'
    assert all('UPDATE' not in c.args[0] for c in pool.fetch.call_args_list)
    pool.execute.assert_not_awaited()
    synth.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_outage_defers_queue_item_without_any_membership_write(monkeypatch):
    incoming = article('new', 'Giants result', 'Reuters')
    old = article('old', 'Giants coverage')
    pool = AsyncMock()
    pool.fetch.return_value = [old]
    monkeypatch.setattr(it, 'same_event', AsyncMock(return_value=None))
    marker = AsyncMock()
    monkeypatch.setattr('services.story_matcher.mark_threaded', marker)
    monkeypatch.setattr(it, 'check_budget', AsyncMock(return_value=type('Budget', (), {'allowed': True})()))
    c = {'article': incoming, 'existing_stories': {'s': [old]}, 'loose_neighbours': [], 'near_misses': []}
    result = await it.run_incremental_threading(pool, candidates=[c],
        confirm=AsyncMock(return_value={'new': {'action': 'attach', 'story_id': 's'}}), sweep=False)
    assert result['deferred_event'] == 1
    assert marker.call_args.args[1] == []
    pool.execute.assert_not_awaited()


def test_confirmer_retains_late_candidate_details_and_dates():
    from services.story_confirmer import _prompt
    members = [article(str(i), 'Coverage '+str(i), summary='Full late detail '+str(i)) for i in range(7)]
    members[-1]['published_date'] = '2026-10-26T12:00:00Z'
    text = _prompt([{'article': article('new', 'New event'), 'existing_stories': {'s': members}, 'loose_neighbours': []}])
    assert 'Full late detail 6' in text and '2026-10-26T12:00:00Z' in text
