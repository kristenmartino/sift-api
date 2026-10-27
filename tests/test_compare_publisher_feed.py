from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services import compare_publisher_feed as feed
from workflows import compare_workflow as workflow

URL = 'https://www.nationalreview.com/2026/10/abdul-el-sayed-is-no-jfk/'
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
TITLE = 'Abdul El-Sayed Is No JFK'
DESCRIPTION = ('Kennedy didn’t defend his faith as a way of distracting from his own extremism '
               'and that of people around him.')
CAPTION = ('Abdul El-Sayed speaks to the media following a U.S. Senate debate in Southfield, '
           'Mich., October 21, 2026 (left); President John F. Kennedy (right).')


def rss(*, url=URL, description=DESCRIPTION, stamp=NOW - timedelta(days=4), title=TITLE):
    date = f'<pubDate>{format_datetime(stamp)}</pubDate>' if stamp else ''
    return (f'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
            f'<item><title>{title}</title><link>{url}</link>{date}'
            f'<description><![CDATA[{description}<img src="image.jpg"/>]]></description>'
            f'<content:encoded><![CDATA[{CAPTION}]]></content:encoded></item></channel></rss>').encode()


def test_complete_description_survives_longer_photo_caption_and_amp_identity():
    result = feed._feed_article_excerpt(rss(), URL + 'amp/', NOW)
    assert result.endswith('Excerpt:\n' + DESCRIPTION)
    assert CAPTION not in result
    assert URL in result
    assert 'Public RSS:' in result


@pytest.mark.parametrize('kwargs', [
    {'url': 'https://www.nationalreview.com/2026/10/a-different-event/'},
    {'url': 'https://another.example/2026/10/abdul-el-sayed-is-no-jfk/'},
    {'description': ''}, {'description': TITLE}, {'description': 'Subscribe to read this story.'},
    {'stamp': NOW - timedelta(days=15)}, {'stamp': NOW + timedelta(hours=1)}, {'stamp': None},
])
def test_missing_unrelated_stale_or_metadata_description_adds_no_evidence(kwargs):
    assert feed._feed_article_excerpt(rss(**kwargs), URL, NOW) is None


def test_feed_metadata_and_photo_caption_cannot_support_a_claim():
    reporting = {'National Review': feed._feed_article_excerpt(rss(), URL, NOW)}
    for quote in [CAPTION, 'In 2012, he joined an open letter backing Mohamed Morsi.']:
        assert workflow._ground_claim_attributions([{'claim': quote, 'sources': ['National Review'],
            'evidence': {'National Review': quote}}], reporting) == []
    retained = workflow._retain_outlet_passages([], {'National Review': None}, reporting)
    assert retained[0]['claim'] == f'National Review writes: “{DESCRIPTION}”'


def mock_http(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(feed.httpx, 'AsyncClient', lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_reader_requests_only_fixed_public_feed_with_no_redirects(monkeypatch):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/rss+xml; charset=UTF-8'},
                              content=rss(stamp=datetime.now(timezone.utc) - timedelta(days=1)))
    mock_http(monkeypatch, handler)
    assert (await feed.public_feed_excerpt(URL, 'National Review')).endswith(DESCRIPTION)
    assert calls == [feed.PUBLIC_FEED]


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'content_type', 'body'), [
    (302, 'application/rss+xml', b''), (403, 'text/html', b'Access denied'),
    (200, 'text/html', b'Sign in'), (200, 'application/rss+xml', b'x' * (feed.MAX_FEED_BYTES + 1)),
], ids=['redirect', 'blocked', 'html', 'oversized'])
async def test_reader_rejects_redirects_restrictions_html_and_oversized_responses(monkeypatch, status, content_type, body):
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(status, headers={'content-type': content_type, 'location': 'https://elsewhere.example/'}, content=body)
    mock_http(monkeypatch, handler)
    assert await feed.public_feed_excerpt(URL, 'National Review') is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reader_failure_preserves_fallback(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout('Unavailable', request=request)
    mock_http(monkeypatch, handler)
    assert await feed.public_feed_excerpt(URL, 'National Review') is None


@pytest.mark.asyncio
@pytest.mark.parametrize(('url', 'source'), [
    ('https://another.example/story', 'National Review'), ('https://nationalreview.com/', 'National Review'),
    ('https://nationalreview.com/?p=42', 'National Review'), (URL, 'Reuters'),
    ('http://nationalreview.com/article', 'National Review'), ('https://[invalid', 'National Review'),
])
async def test_reader_never_requests_arbitrary_urls_or_other_publishers(monkeypatch, url, source):
    client = Mock(side_effect=AssertionError('No HTTP request expected'))
    monkeypatch.setattr(feed.httpx, 'AsyncClient', client)
    assert await feed.public_feed_excerpt(url, source) is None
    client.assert_not_called()


@pytest.mark.asyncio
async def test_exact_public_description_avoids_paid_search(monkeypatch):
    excerpt = feed._feed_article_excerpt(rss(), URL, NOW)
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value={'excerpt': None, 'title': TITLE, 'url': URL}))
    reader = AsyncMock(return_value=excerpt)
    monkeypatch.setattr(workflow, 'public_feed_excerpt', reader)
    paid = AsyncMock(side_effect=AssertionError('No paid search expected'))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    assert await workflow.search_one_source(Mock(), 'Michigan letter story', 'National Review') == ('National Review', excerpt)
    reader.assert_awaited_once_with(URL, 'National Review')
    paid.assert_not_awaited()


@pytest.mark.asyncio
async def test_short_description_cannot_be_grouped_into_inferred_facts_or_false_disputes(monkeypatch):
    import json
    from types import SimpleNamespace
    excerpt = feed._feed_article_excerpt(rss(), URL, NOW)
    other = 'President Trump called the candidate a threat to national security on Sunday.'
    invented = {'claim': 'NR and NPR disagree about whether the candidate signed a letter.',
                'agreement': 'disputed', 'sources_for': ['National Review'], 'sources_against': ['NPR'],
                'evidence': {'National Review': DESCRIPTION, 'NPR': other}}
    payload = {'claims': [invented, {'claim': 'Trump called the candidate a security threat.',
                'sources': ['NPR'], 'evidence': {'NPR': other}}]}
    paid = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    monkeypatch.setattr(workflow.anthropic, 'AsyncAnthropic', Mock())
    state = {'topic': 'Michigan letter story', 'sources': ['National Review', 'NPR'],
             'search_results': {'National Review': excerpt, 'NPR': other}}
    result = await workflow.extract_and_compare_node(state)
    formatted = await workflow.format_response_node({**state, **result})
    assert len(formatted['claims']) == 2
    assert all(c['agreement'] != 'disputed' for c in formatted['claims'])
    nr = next(c for c in formatted['claims'] if c.get('sources') == ['National Review'])
    assert nr['claim'] == f'National Review writes: “{DESCRIPTION}”'
    assert result['analyzed_sources'] == ['National Review', 'NPR']
    assert DESCRIPTION not in paid.await_args.kwargs['messages'][0]['content']
    paid.assert_awaited_once()
    assert paid.await_args.kwargs['max_tokens'] == 4096


@pytest.mark.asyncio
async def test_only_complete_public_description_needs_no_model_interpretation(monkeypatch):
    excerpt = feed._feed_article_excerpt(rss(), URL, NOW)
    paid = AsyncMock(side_effect=AssertionError('No model needed for literal quotation'))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    result = await workflow.extract_and_compare_node({'topic': TITLE, 'sources': ['National Review'],
        'search_results': {'National Review': excerpt}})
    assert result['comparison'] == f'National Review writes: “{DESCRIPTION}”'
    assert len(result['claims']) == 1
    paid.assert_not_awaited()


@pytest.mark.parametrize('description', [
    'Pursuing benefit cuts over tax hikes would grow the economy and reduce the national debt burden.',
    'A Colorado case shows how reliance on taxpayer funding can lead to religious discrimination.',
    'Registering at welfare agencies may be letting people on voter rolls illegally.',
    'California’s governor tried to outlaw oil production by executive fiat. A state judge has exposed the deception.',
])
def test_complete_short_public_positions_are_quoted_without_added_facts(description):
    excerpt = feed._feed_article_excerpt(rss(description=description), URL, NOW)
    assert excerpt and excerpt.endswith(description)
    result = workflow._retain_outlet_passages([], None, {'National Review': excerpt})
    assert result[0]['claim'] == f'National Review writes: “{description}”'
    assert result[0]['sources'] == ['National Review']


@pytest.mark.parametrize('description', [
    'Moral values without moral commitments.',
    'In our brave new tech world, there’s nothing to fear but . . . everything.',
    'The committee approved the measure but its next step...',
    'Read the full story to find out more.',
    'Already have an account? Sign in to read more.',
    'Get full access to this article by subscribing.',
])
def test_slogans_unfinished_text_and_access_teasers_stay_excluded(description):
    assert feed._feed_article_excerpt(rss(description=description), URL, NOW) is None


def test_html_headline_is_not_mistaken_for_substantive_description():
    headline = 'The committee approved a new policy after an overnight debate.'
    assert feed._feed_article_excerpt(rss(title=f'<i>{headline}</i>', description=headline), URL, NOW) is None


def test_generic_stored_reporting_keeps_its_twenty_word_minimum():
    from services.compare_coverage import _has_publisher_reporting
    body = 'A Colorado case shows how reliance on taxpayer funding can lead to religious discrimination.'
    assert not _has_publisher_reporting(body, TITLE)


@pytest.mark.asyncio
async def test_candidate_descriptions_share_one_bounded_public_feed_request(monkeypatch):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/rss+xml'},
            content=rss(description='The committee approved a new measure after a lengthy debate.',
                        stamp=datetime.now(timezone.utc) - timedelta(days=1)))
    mock_http(monkeypatch, handler)
    missing = 'https://www.nationalreview.com/a-different-event/'
    result = await feed.public_feed_excerpts([URL, URL, missing], 'National Review')
    assert list(result) == [URL]
    assert calls == [feed.PUBLIC_FEED]
