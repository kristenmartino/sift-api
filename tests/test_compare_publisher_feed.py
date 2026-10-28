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
    monkeypatch.setattr(workflow, 'verify_claim_support', AsyncMock(side_effect=lambda _client, claims, _reporting, **_kwargs: (claims, set())))
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


def test_axios_keeps_complete_reporting_but_not_a_trailing_document_caption():
    from services.compare_publisher_feed import AXIOS_FEED, _feed_article_excerpt
    body = ('<p>The committee approved the measure after a public hearing on Monday.</p>'
            '<p>A spokesperson said the new rules take effect next month.</p>'
            '<p>Unclassified document from the committee</p>')
    feed = f'''<rss version="2.0"><channel><item><title>A committee vote</title>
      <link>https://www.axios.com/2026/10/24/a-vote</link>
      <pubDate>Sat, 24 Oct 2026 00:16:19 +0000</pubDate>
      <description><![CDATA[{body}]]></description></item></channel></rss>'''.encode()
    result = _feed_article_excerpt(feed, 'https://www.axios.com/2026/10/24/a-vote',
        datetime(2026, 10, 27, tzinfo=timezone.utc), feed_url=AXIOS_FEED)
    assert 'The committee approved the measure' in result
    assert 'Unclassified document' not in result
    assert AXIOS_FEED in result
    assert 'nationalreview.com/feed' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'url', 'feed_url'), [
    ('Financial Times', 'https://www.ft.com/content/e2e82393-c7ca-4bc1-b270-022b3702d9e7?syn-25a6b1a6=1', 'https://www.ft.com/rss/home/international'),
    ('The Hill', 'https://thehill.com/policy/international/a-memo/', 'https://thehill.com/news/feed/'),
    ('The Verge', 'https://www.theverge.com/tech/123/a-report', 'https://www.theverge.com/rss/index.xml'),
    ('Ars Technica', 'https://arstechnica.com/tech-policy/2026/10/a-report/', 'https://feeds.arstechnica.com/arstechnica/index'),
])
async def test_verified_publisher_feeds_recover_only_the_exact_article(monkeypatch, source, url, feed_url):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/rss+xml'},
                              content=rss(url=url, stamp=datetime.now(timezone.utc) - timedelta(days=1)))
    mock_http(monkeypatch, handler)
    excerpt = await feed.public_feed_excerpt(url, source)
    assert DESCRIPTION in excerpt and feed_url in excerpt
    assert CAPTION not in excerpt
    assert calls == [feed_url]
    assert await feed.public_feed_excerpt(url.replace('a-report', 'a-different-report').replace('a-memo', 'a-different-memo').replace('e2e82393', 'aaaaaaaa'), source) is None


@pytest.mark.parametrize('domain', ['theverge.com', 'arstechnica.com'])
def test_public_feed_content_can_supply_complete_paragraphs_after_a_truncated_teaser(domain):
    url = f'https://www.{domain}/a-report/'
    body = '<p>The researchers found a fault in the hinge after testing six phones.</p><p>Read the full article for more.</p><p>Photograph from the laboratory</p>'
    data = rss(url=url, description='The researchers found a problem but...', title='A hinge report').replace(CAPTION.encode(), body.encode())
    excerpt = feed._feed_article_excerpt(data, url, NOW, feed_url=feed._FEEDS[domain])
    assert excerpt.endswith('The researchers found a fault in the hinge after testing six phones.')
    assert 'Photograph' not in excerpt and 'Read the full' not in excerpt
    assert feed._feed_article_excerpt(data.replace(body.encode(), b'<p>A problem was found but...</p>'), url, NOW, feed_url=feed._FEEDS[domain]) is None


@pytest.mark.asyncio
async def test_cnbc_does_not_call_a_feed_that_rejected_public_access(monkeypatch):
    def handler(request):
        raise AssertionError('Blocked feed must not be requested')
    mock_http(monkeypatch, handler)
    assert await feed.public_feed_excerpt('https://www.cnbc.com/a-report/', 'CNBC') is None


def test_hill_truncated_teaser_keeps_only_unchanged_complete_prefix():
    url = 'https://thehill.com/a-report/'
    intact = 'The committee approved the measure after a public hearing on Monday.'
    data = rss(url=url, description=intact + ' Its next step depends on...', title='A committee vote')
    excerpt = feed._feed_article_excerpt(data, url, NOW, feed_url=feed._FEEDS['thehill.com'])
    assert excerpt.endswith(intact)
    assert 'Its next step' not in excerpt
    abbreviation = 'The committee received testimony from officials in the U.S. The next step depends on...'
    assert feed._feed_article_excerpt(rss(url=url, description=abbreviation), url, NOW, feed_url=feed._FEEDS['thehill.com']) is None


def test_ft_unpunctuated_standfirst_is_verbatim_and_requires_claim_review():
    url = 'https://www.ft.com/content/e2e82393-c7ca-4bc1-b270-022b3702d9e7'
    text = 'The committee chair said the proposed measure could take effect next year if lawmakers approve it after a further round of public hearings'
    excerpt = feed._feed_article_excerpt(rss(url=url, description=text), url, NOW, feed_url=feed._FEEDS['ft.com'])
    assert excerpt.endswith(text)
    assert workflow._complete_short_excerpt(excerpt) is None
    assert workflow._retain_outlet_passages([], None, {'Financial Times': excerpt}) == []
    assert feed._feed_article_excerpt(rss(url=url, description=text + '...'), url, NOW, feed_url=feed._FEEDS['ft.com']) is None


def test_public_feed_complete_summary_does_not_hide_fuller_public_paragraphs():
    url = 'https://arstechnica.com/apple/2026/10/a-chip-launch/'
    summary = 'The new networking chip supports faster wireless connections in both models.'
    paragraph = 'Apple introduced the new processor in its desktop and laptop computers on Tuesday.'
    data = rss(url=url, description=summary, title='A chip launch').replace(CAPTION.encode(), f'<p>{paragraph}</p>'.encode())
    excerpt = feed._feed_article_excerpt(data, url, NOW, feed_url=feed._FEEDS['arstechnica.com'])
    assert excerpt.endswith(paragraph)
    assert CAPTION not in excerpt


FT_MEMO = 'https://www.ft.com/content/e2e82393-c7ca-4bc1-b270-022b3702d9e7?syn-25a6b1a6=1'
FT_IRAN = 'https://www.ft.com/iran?format=rss'
FT_DESCRIPTION = 'Plans against US citizens have reached ‘advanced stages’ as tensions rise in region, according to document'


@pytest.mark.asyncio
@pytest.mark.parametrize('topic', ['Intelligence memo warns Iran plotting against Americans', 'Iranian plots against US personnel'])
async def test_ft_topic_feed_recovers_short_exact_memo_standfirst(monkeypatch, topic):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'text/xml'},
            content=rss(url=FT_MEMO, title='Intelligence memo warns Iran is plotting to kill Americans',
                        description=FT_DESCRIPTION, stamp=datetime.now(timezone.utc) - timedelta(days=4)))
    mock_http(monkeypatch, handler)
    excerpt = await feed.public_feed_excerpt(FT_MEMO, 'Financial Times', topic=topic)
    assert excerpt.endswith(FT_DESCRIPTION)
    assert FT_IRAN in excerpt and CAPTION not in excerpt
    assert calls == [FT_IRAN]
    assert workflow._complete_short_excerpt(excerpt) is None
    assert workflow._retain_outlet_passages([], None, {'Financial Times': excerpt}) == []


@pytest.mark.asyncio
async def test_ft_home_feed_remains_fallback_when_topic_feed_lacks_exact_article(monkeypatch):
    calls = []
    def handler(request):
        url = str(request.url)
        calls.append(url)
        article = FT_MEMO if url == feed._FEEDS['ft.com'] else FT_MEMO.replace('e2e82393', 'aaaaaaaa')
        return httpx.Response(200, headers={'content-type': 'text/xml'},
            content=rss(url=article, description=FT_DESCRIPTION, stamp=datetime.now(timezone.utc) - timedelta(days=1)))
    mock_http(monkeypatch, handler)
    assert (await feed.public_feed_excerpt(FT_MEMO, 'Financial Times', topic='Iran memo')).endswith(FT_DESCRIPTION)
    assert calls == [FT_IRAN, feed._FEEDS['ft.com']]


@pytest.mark.asyncio
async def test_ft_topic_profile_never_fetches_a_caller_supplied_url(monkeypatch):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(403)
    mock_http(monkeypatch, handler)
    assert await feed.public_feed_excerpt(FT_MEMO, 'Financial Times', topic='Iran https://evil.example/private') is None
    assert calls == [FT_IRAN, feed._FEEDS['ft.com']]


@pytest.mark.asyncio
async def test_ft_keeps_first_verified_article_when_second_feed_fails(monkeypatch):
    other = FT_MEMO.replace('e2e82393', 'aaaaaaaa')
    def handler(request):
        if str(request.url) != FT_IRAN:
            raise httpx.ReadTimeout('Public feed unavailable', request=request)
        return httpx.Response(200, headers={'content-type': 'text/xml'},
            content=rss(url=FT_MEMO, description=FT_DESCRIPTION, stamp=datetime.now(timezone.utc) - timedelta(days=1)))
    mock_http(monkeypatch, handler)
    result = await feed.public_feed_excerpts([FT_MEMO, other], 'Financial Times', topic='Iran')
    assert list(result) == [FT_MEMO]


@pytest.mark.asyncio
async def test_ft_topic_and_home_share_one_byte_budget(monkeypatch):
    data = rss(url=FT_MEMO, description=FT_DESCRIPTION, stamp=datetime.now(timezone.utc) - timedelta(days=1))
    monkeypatch.setattr(feed, 'MAX_FEED_BYTES', len(data) + 10)
    def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/xml'},
                              content=data if str(request.url) == FT_IRAN else b' ' * 20)
    mock_http(monkeypatch, handler)
    result = await feed.public_feed_excerpts([FT_MEMO, FT_MEMO.replace('e2e82393', 'aaaaaaaa')], 'Financial Times', topic='Iran')
    assert list(result) == [FT_MEMO]


@pytest.mark.asyncio
async def test_workflow_uses_own_ft_headline_for_free_feed_recovery(monkeypatch):
    original = 'Public publisher description (not full article text).\nExcerpt:\n' + FT_DESCRIPTION
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value={
        'title': 'Intelligence memo warns Iran plotting against Americans', 'url': FT_MEMO, 'excerpt': None}))
    monkeypatch.setattr(workflow, 'public_article_excerpt', AsyncMock(return_value=None))
    reader = AsyncMock(return_value=original)
    monkeypatch.setattr(workflow, 'public_feed_excerpt', reader)
    paid = AsyncMock(side_effect=AssertionError('Exact public RSS should avoid a paid search'))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    assert await workflow.search_one_source(Mock(), 'Iran memo', 'Financial Times') == ('Financial Times', original)
    reader.assert_awaited_once_with(FT_MEMO, 'Financial Times', topic='Intelligence memo warns Iran plotting against Americans')


ARS_REPORT = 'https://arstechnica.com/tech-policy/2026/01/data-broker-hoarding-is-rampant-new-law-lets-consumers-fight-back/'
ARS_PUBLIC = ('Californians are getting a new, supercharged way to stop data brokers from hoarding '
              'and selling their personal information, as a recently enacted law that’s among the '
              'strictest in the nation took effect at the beginning of the year. '
              'According to the California Privacy Protection Agency, more than 500 companies actively '
              'scour all sorts of sources for scraps of information about individuals, then package and '
              'store it to sell to marketers, private investigators, and others.')


def ars_topic_rss(**kwargs):
    data = rss(url=ARS_REPORT, title='California data broker privacy law', description='Californians can now submit demands requiring 500 brokers to delete their data.', **kwargs)
    return data.replace(CAPTION.encode(), f'<p>{ARS_PUBLIC}</p><p>Read full article</p>'.encode())


@pytest.mark.asyncio
async def test_sparse_topic_feed_recovers_old_public_paragraphs_but_preserves_exact_feed_recency(monkeypatch):
    data = ars_topic_rss(stamp=datetime.now(timezone.utc) - timedelta(days=295))
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, headers={'content-type': 'application/rss+xml'}, content=data)
    mock_http(monkeypatch, handler)
    candidates = await feed.public_topic_candidates("Data brokers don't just collect facts. They guess about you", 'Ars Technica')
    assert len(candidates) == 1
    assert candidates[0]['url'] == ARS_REPORT
    assert candidates[0]['excerpt'].endswith(ARS_PUBLIC)
    assert 'Read full article' not in candidates[0]['excerpt']
    assert 'Published:' in candidates[0]['excerpt']
    assert calls == [feed.ARS_DATA_BROKERS_FEED]
    assert feed._feed_article_excerpt(data, ARS_REPORT, datetime.now(timezone.utc)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('age', [366, -1])
async def test_topic_feed_does_not_make_ancient_or_future_reporting_current(monkeypatch, age):
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-type': 'application/rss+xml'},
        content=ars_topic_rss(stamp=datetime.now(timezone.utc) - timedelta(days=age))))
    assert await feed.public_topic_candidates('Data brokers', 'Ars Technica') == []


@pytest.mark.asyncio
@pytest.mark.parametrize(('source', 'topic'), [('CNBC', 'Data brokers'), ('The Verge', 'Data brokers'),
                                          ('Ars Technica', 'Interactive Brokers earnings')])
async def test_topic_feed_routes_only_verified_publisher_and_subject(monkeypatch, source, topic):
    mock_http(monkeypatch, lambda request: pytest.fail('Unmatched subject must not fetch a feed'))
    assert await feed.public_topic_candidates(topic, source) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://evil.example/article/', 'http://arstechnica.com/report/',
                                  'https://arstechnica.com/civis/threads/data-brokers/'])
async def test_topic_feed_cannot_credit_external_or_reader_forum_content(monkeypatch, url):
    data = ars_topic_rss(stamp=datetime.now(timezone.utc) - timedelta(days=1)).replace(ARS_REPORT.encode(), url.encode())
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-type': 'application/rss+xml'}, content=data))
    assert await feed.public_topic_candidates('Data brokers https://evil.example/private', 'Ars Technica') == []


@pytest.mark.asyncio
async def test_topic_feed_rejects_redirects_and_oversized_responses(monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(302, headers={'location': 'https://evil.example/private'}))
    assert await feed.public_topic_candidates('Data brokers', 'Ars Technica') == []
    monkeypatch.setattr(feed, 'MAX_FEED_BYTES', 20)
    mock_http(monkeypatch, lambda request: httpx.Response(200, headers={'content-type': 'application/rss+xml'},
        content=ars_topic_rss(stamp=datetime.now(timezone.utc) - timedelta(days=1))))
    assert await feed.public_topic_candidates('Data brokers', 'Ars Technica') == []


@pytest.mark.asyncio
async def test_topic_candidate_requires_the_existing_relevance_selection_even_with_indexed_metadata(monkeypatch):
    from types import SimpleNamespace
    candidate = {'title': 'California privacy law', 'url': ARS_REPORT,
                 'excerpt': 'Public publisher description (not full article text).\nPublished: 2026-01-05\nExcerpt:\n' + ARS_PUBLIC}
    monkeypatch.setattr(workflow, 'indexed_coverage', AsyncMock(return_value={**candidate, 'excerpt': None}))
    monkeypatch.setattr(workflow, 'public_article_excerpt', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_feed_excerpt', AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, 'public_topic_candidates', AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(workflow, 'public_feed_excerpts', AsyncMock(return_value={}))
    paid = AsyncMock(return_value=SimpleNamespace(stop_reason='end_turn', content=[
        SimpleNamespace(type='text', text='No relevant coverage found from Ars Technica.')]))
    monkeypatch.setattr(workflow, 'budgeted_message', paid)
    monkeypatch.setattr(workflow, 'log_usage', Mock())
    result = await workflow.search_one_source(Mock(), "Data brokers don't just collect facts. They guess about you", 'Ars Technica')
    assert result == ('Ars Technica', None)
    paid.assert_awaited_once()
    assert 'tools' not in paid.await_args.kwargs
    prompt = paid.await_args.kwargs['messages'][0]['content']
    assert ARS_PUBLIC in prompt
    assert 'opting out of record collection alone is not' in prompt
    assert 'SUPPORTING_PASSAGE=' in prompt
