"""Exercise event matching in an isolated local PostgreSQL schema."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from services import compare_coverage
from services.compare_coverage import INDEXED_COVERAGE_SQL


@pytest_asyncio.fixture
async def coverage_db():
    url = os.environ.get('AI_BUDGET_TEST_DATABASE_URL')
    if not url:
        pytest.skip('isolated test database not configured')
    assert urlparse(url).hostname in {'localhost', '127.0.0.1'}
    schema = 'compare_test_' + uuid4().hex
    c = await asyncpg.connect(url)
    await c.execute(f'CREATE SCHEMA "{schema}"')
    await c.execute(f'SET search_path TO "{schema}"')
    await c.execute('''
      CREATE TABLE outlet_profiles(slug text, name text);
      CREATE TABLE source_name_aliases(raw_source_name text, outlet_slug text);
      CREATE TABLE stories(id text, headline text, synthesis_status text, headline_aliases text[] NOT NULL DEFAULT '{}');
      CREATE TABLE articles(id bigserial PRIMARY KEY, title text, source_name text, source_url text, published_date timestamptz,
        story_id text, generation_raw_content text, from_search boolean DEFAULT false,
        is_roundup boolean DEFAULT false, generation_status text DEFAULT 'complete', summary text DEFAULT 'Reviewed');
      INSERT INTO outlet_profiles VALUES('fox-news','Fox News'),('bbc-news','BBC News');
      INSERT INTO source_name_aliases VALUES('BBC World','bbc-news');
      INSERT INTO stories(id,headline,synthesis_status) VALUES('event','The committee approved the measure','complete'),('held','An unrelated event','copy_hold');
      INSERT INTO articles(title,source_name,source_url,published_date,story_id,generation_raw_content) VALUES
       ('Lawmakers vote 10 to 2','Fox News','https://foxnews.com/event',NOW()-INTERVAL '1 day','event','Original Fox reporting.'),
       ('The vote passed','BBC World','https://bbc.com/event',NOW()-INTERVAL '1 day','event','Original BBC reporting.'),
       ('Another committee vote','Fox News','https://foxnews.com/unrelated',NOW()-INTERVAL '1 hour','held','Other reporting.');
    ''')
    try:
        yield c
    finally:
        await c.execute('SET search_path TO public')
        await c.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await c.close()


@pytest.mark.asyncio
async def test_same_event_and_publisher_aliases_are_required(coverage_db):
    c = coverage_db
    fox = await c.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'fox news')
    bbc = await c.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'bbc-news')
    assert fox['source_url'] == 'https://foxnews.com/event'
    assert bbc['source_url'] == 'https://bbc.com/event'
    assert await c.fetchrow(INDEXED_COVERAGE_SQL, 'committee', 'fox news') is None
    assert await c.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'Unknown publisher') is None
    assert await c.fetchrow(INDEXED_COVERAGE_SQL, 'An unrelated event', 'fox news') is None
    exact_title = await c.fetchrow(INDEXED_COVERAGE_SQL, 'Lawmakers vote 10 to 2', 'fox news')
    assert exact_title['source_url'] == fox['source_url']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    "published_date=NOW()+INTERVAL '1 day'", "published_date=NOW()-INTERVAL '15 days'",
    "from_search=true", "is_roundup=true", "source_url='javascript:alert(1)'",
])
async def test_unusable_records_never_supply_coverage(coverage_db, change):
    await coverage_db.execute('UPDATE articles SET '+change)
    assert await coverage_db.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'fox news') is None


@pytest.mark.asyncio
async def test_resuming_database_preserves_indexed_reporting(coverage_db, monkeypatch):
    # Production's first post-deployment lookup took 3.74s and the previous 3s
    # limit discarded all five sources. Exercise a real delayed PostgreSQL
    # response, not just a mocked successful fetchrow.
    body = ('The committee approved the measure by ten votes to two after hearing '
            'testimony from residents at a public meeting on Monday.')
    await coverage_db.execute('UPDATE articles SET generation_raw_content=$1', body)

    class ResumingDatabase:
        async def fetch(self, query, *args, timeout):
            async with asyncio.timeout(timeout):
                await coverage_db.execute('SELECT pg_sleep(3.2)')
                return await coverage_db.fetch(query, *args)

    async def get_resuming_database():
        return ResumingDatabase()

    monkeypatch.setattr(compare_coverage, 'get_pool', get_resuming_database)
    result = await compare_coverage.indexed_coverage('The committee approved the measure', 'fox news')
    assert body in result['excerpt']


@pytest.mark.asyncio
async def test_headline_rewrites_preserve_exact_event_without_merging(coverage_db):
    migration = (Path(__file__).resolve().parents[1] / 'migrations/037_compare_exact_events.sql').read_text()
    await coverage_db.execute(migration.replace('CREATE INDEX CONCURRENTLY', 'CREATE INDEX'))
    await coverage_db.execute("UPDATE stories SET headline='The measure passed after a committee vote' WHERE id='event'")
    old = await coverage_db.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'fox news')
    current = await coverage_db.fetchrow(INDEXED_COVERAGE_SQL, 'The measure passed after a committee vote', 'fox news')
    assert old['source_url'] == current['source_url'] == 'https://foxnews.com/event'
    assert await coverage_db.fetchrow(INDEXED_COVERAGE_SQL, 'committee vote', 'fox news') is None
    # Reapplying startup DDL must retain history, and held stories stay held.
    await coverage_db.execute(migration.replace('CREATE INDEX CONCURRENTLY', 'CREATE INDEX'))
    await coverage_db.execute("UPDATE stories SET headline='Committee passes measure', synthesis_status='copy_hold' WHERE id='event'")
    assert await coverage_db.fetchrow(INDEXED_COVERAGE_SQL, 'The committee approved the measure', 'fox news') is None
    aliases = await coverage_db.fetchval("SELECT headline_aliases FROM stories WHERE id='event'")
    assert set(aliases) == {'the committee approved the measure', 'the measure passed after a committee vote'}


@pytest.mark.asyncio
async def test_held_generation_is_a_metadata_hint_not_publisher_evidence(coverage_db, monkeypatch):
    await coverage_db.execute("UPDATE articles SET generation_status='copy_hold',summary=NULL")
    async def get_connection():
        return coverage_db
    monkeypatch.setattr(compare_coverage, 'get_pool', get_connection)
    result = await compare_coverage.indexed_coverage('The committee approved the measure', 'fox news')
    assert result['excerpt'] is None
    assert result['url'] == 'https://foxnews.com/event'


@pytest.mark.asyncio
async def test_newer_empty_record_does_not_mask_usable_same_event_text(coverage_db, monkeypatch):
    body = 'The committee approved the measure by ten votes to two after hearing testimony from residents at a public meeting on Monday.'
    await coverage_db.execute('UPDATE articles SET generation_raw_content=$1', body)
    await coverage_db.execute("""INSERT INTO articles(title,source_name,source_url,published_date,story_id)
      VALUES('Newer article without text','Fox News','https://foxnews.com/new',NOW(),'event')""")
    async def get_connection():
        return coverage_db
    monkeypatch.setattr(compare_coverage, 'get_pool', get_connection)
    result = await compare_coverage.indexed_coverage('The committee approved the measure', 'fox news')
    assert body in result['excerpt']
    assert result['url'] == 'https://foxnews.com/event'


@pytest.mark.asyncio
async def test_publisher_discovery_returns_only_recent_original_title_hints(coverage_db, monkeypatch):
    await coverage_db.execute("""INSERT INTO articles(title,source_name,source_url,published_date,generation_status,summary) VALUES
      ('Cornell fraternity party ban','National Review','https://www.nationalreview.com/cornell',NOW(),'unavailable',NULL),
      ('Cornell fraternity policy','Fox News','https://foxnews.com/cornell',NOW(),'complete','Reviewed'),
      ('Another ban','National Review','https://www.nationalreview.com/other-ban',NOW(),'complete','Reviewed'),
      ('Cornell fraternity old ban','National Review','https://www.nationalreview.com/old',NOW()-INTERVAL '20 days','complete','Reviewed'),
      ('Unrelated politics','National Review','https://www.nationalreview.com/unrelated',NOW(),'complete','Cornell fraternity party ban')""")
    async def get_connection():
        return coverage_db
    monkeypatch.setattr(compare_coverage, 'get_pool', get_connection)
    hints = await compare_coverage.publisher_article_hints('Cornell University fraternity party ban')
    assert hints == [{'title': 'Cornell fraternity party ban', 'url': 'https://www.nationalreview.com/cornell'}]
    body = 'The university announced that fraternity houses cannot host large social events during the school year after a review of campus safety rules.'
    await coverage_db.execute("UPDATE articles SET generation_status='complete',summary='Reviewed',generation_raw_content=$1 WHERE source_url='https://www.nationalreview.com/cornell'", body)
    hints = await compare_coverage.publisher_article_hints('Cornell University fraternity party ban')
    assert body in hints[0]['excerpt']
    assert 'Reviewed' not in hints[0]['excerpt']
