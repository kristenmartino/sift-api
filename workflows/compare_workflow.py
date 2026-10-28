from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import NotRequired, TypedDict

import anthropic
from langgraph.graph import StateGraph, END

from app.config import settings
from app.db import get_pool
from services.cost_guard import budget_snapshot
from services.paid_calls import BudgetUnavailable, budgeted_message, budget_scope, message_bound
from services.model_registry import resolve
from services.usage_tracker import count_web_searches, log_usage, prices_for
from services.compare_coverage import indexed_coverage, publisher_article_hints
from services.compare_search import normalize_evidence_text, publisher_domains, publisher_search_domains, publisher_search_excerpts
from services.compare_publisher_feed import PUBLIC_DESCRIPTION_PREFIX, complete_description, public_feed_excerpt, public_feed_excerpts, supports_public_feed
from services.compare_claim_support import verify_claim_support
from services.compare_public_article import public_article_excerpt, public_article_candidates
from services.compare_syndication import PARTNER_DOMAINS, reuters_partner_excerpts, reuters_archive_candidates

logger = logging.getLogger("sift-api.compare")

# Two operations live in this file and they are not interchangeable.
# `compare.search_sources` uses Anthropic's server-side web_search_20250305
# tool, which has no portable equivalent — model_registry.CAPABILITIES refuses
# to move it off a model that provides one. `compare.extract_and_compare` is an
# ordinary completion over the text those searches returned, and is swappable.
SEARCH_OPERATION = "compare.search_sources"
EXTRACT_OPERATION = "compare.extract_and_compare"
PER_SOURCE_TIMEOUT = 20  # seconds per source search

# Safe fallback allowlist — used ONLY when the curated set can't be loaded from
# the DB (local dev, startup race, or an outage). The production allowlist is
# data-backed (see load_allowed_sources): it tracks the curated outlet_profiles
# set + source_name_aliases, so the compare source pool stays in sync with the
# outlets Sift actually ingests instead of drifting from a hand-maintained list.
FALLBACK_ALLOWED_SOURCES = {
    "reuters", "bbc", "associated press", "ap news", "npr", "cnn", "fox news",
    "nbc news", "abc news", "cbs news", "the new york times", "washington post",
    "the guardian", "al jazeera", "politico", "the hill", "axios", "bloomberg",
    "cnbc", "financial times", "the economist", "the wall street journal",
    "techcrunch", "the verge", "wired", "ars technica", "nature", "science",
}


async def load_allowed_sources() -> set[str]:
    """Allowed compare source names (lowercased), derived from curated outlets.

    Sources are validated against the curated outlet set rather than a static
    constant: canonical names and slugs from ``outlet_profiles`` plus the raw
    RSS names in ``source_name_aliases``. This keeps the compare allowlist in
    sync with the outlets Sift ingests — including curated right-leaning outlets
    the old hardcoded list omitted. Falls back to ``FALLBACK_ALLOWED_SOURCES``
    (small, safe) if the DB is unavailable or unseeded, so validation stays
    strict and deterministic rather than failing open.
    """
    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            SELECT LOWER(name) AS s FROM outlet_profiles
            UNION
            SELECT LOWER(slug) AS s FROM outlet_profiles
            UNION
            SELECT LOWER(raw_source_name) AS s FROM source_name_aliases
            """
        )
        allowed = {r["s"].strip() for r in rows if r["s"] and r["s"].strip()}
        if allowed:
            return allowed
        logger.warning(
            "outlet_profiles/source_name_aliases empty; using fallback allowlist"
        )
    except Exception as e:
        logger.warning(
            "Could not load compare allowlist from DB (%s); using fallback", e
        )
    return set(FALLBACK_ALLOWED_SOURCES)


def filter_allowed_sources(sources: list[str], allowed: set[str]) -> list[str]:
    """Keep only sources whose normalized (lowercased, trimmed) name is allowed.

    Matching is case-/whitespace-insensitive; the original source string is
    preserved for the downstream search prompt. Anything not explicitly on the
    allowlist (arbitrary domains, injected strings) is rejected.
    """
    return [s for s in sources if s.lower().strip() in allowed]


def _sanitize_text(text: str) -> str:
    """Strip control characters and collapse whitespace to mitigate prompt injection."""
    # Remove control characters except normal whitespace
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Collapse multiple whitespace to single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class CompareState(TypedDict):
    topic: str
    sources: list[str]
    search_results: dict[str, str]  # source_name -> coverage text
    claims: list[dict]
    comparison: str
    errors: list[str]
    analyzed_sources: NotRequired[list[str]]
    capacity_limited: NotRequired[bool]


def comparison_provenance(state: CompareState) -> dict:
    """Source availability comes from workflow state, never generated prose."""
    requested = list({s.strip().casefold(): s.strip() for s in state["sources"]}.values())
    found = {s.strip().casefold() for s in state.get("search_results", {})}
    completed = {s.strip().casefold() for s in state.get("analyzed_sources", [])}
    retrieved = [s for s in requested if s.casefold() in found]
    analyzed = [s for s in retrieved if s.casefold() in completed]
    return {
        "source_provenance_version": 1,
        "sources_requested": requested,
        "sources_retrieved": retrieved,
        "sources_analyzed": analyzed,
        "sources_unavailable": [s for s in requested if s not in retrieved],
    }


async def compare_search_parallelism(source_count: int) -> int:
    """Reuse capacity as searches settle rather than requiring five max holds.

    This snapshot chooses concurrency only; atomic per-call admission remains
    authoritative when requests race. Keep extraction headroom where possible.
    """
    if not settings.ai_cost_guard_enabled:
        return max(1, source_count)
    try:
        budget = await budget_snapshot()
    except Exception as exc:
        raise BudgetUnavailable("Budget cannot be verified", "COST_GUARD_UNAVAILABLE") from exc
    cap = budget["daily_limit_usd"]
    if not budget["enabled"] or cap <= 0:
        return max(1, source_count)
    if budget_scope.get() != "compare":
        cap *= 1 - budget["compare_share"]
    search_bound = message_bound({"model": resolve(SEARCH_OPERATION).model, "max_tokens": 2048,
        "messages": [], "tools": [{"name": "web_search", "max_uses": 1}]})
    rates = prices_for(resolve(EXTRACT_OPERATION).model)
    extract_headroom = (200_000 * max(rates.input_per_m, rates.cache_write_per_m) + 4096 * rates.output_per_m) / 1_000_000
    available = cap - budget["spent_usd"] - budget["held_usd"] - extract_headroom
    return max(1, min(source_count, int(max(0, available) / search_bound)))


# --- Node functions ---


async def search_one_source(
    client: "anthropic.AsyncAnthropic", topic: str, source: str
) -> tuple[str, str | None]:
    """Search a single source. Returns (source_name, result_text | None).

    Module-level (not a closure inside the node) so the SSE stream endpoint
    can fan these out with as_completed and emit a per-source event the
    moment each one finishes — the same work, reported live.
    """
    stored = await indexed_coverage(topic, source)
    if stored and stored["excerpt"]:
        logger.info("compare indexed coverage: using publisher excerpt for %s", source)
        return (source, stored["excerpt"])
    if stored:
        public_excerpt = (await public_article_excerpt(stored['url'], source)
                          or await public_feed_excerpt(stored['url'], source))
        if public_excerpt:
            logger.info('compare public publisher text: using exact original for %s', source)
            return source, public_excerpt
    article_hint = (f"\nA matching indexed article is {stored['title']} at {stored['url']}. "
                    "Retrieve its reporting; the title alone is not evidence.\n" if stored else "")
    domains = publisher_domains(source)
    search_domains = publisher_search_domains(source)
    discovery_context = None
    if search_domains and domains != ('nationalreview.com',) and not stored:
        # An editorial headline may hide the actual event (e.g. Colorado
        # funding behind a generic religious-rights headline). Original NR
        # text locates the subject only; it cannot supply counterpart claims.
        origin = await indexed_coverage(topic, 'national review')
        if origin:
            context = origin['excerpt'] or await public_feed_excerpt(origin['url'], 'national review')
            if context:
                discovery_context = context
                article_hint += ('\nContext from a different publisher to identify the requested event only. '
                                 'This is NOT evidence from the selected outlet; never borrow its facts or quotes:\n'
                                 + context + '\n')
    reuters = search_domains == ('reuters.com',)
    archive_hints = []
    if reuters and not stored:
        archive_hints = await reuters_archive_candidates(topic, context=discovery_context) if discovery_context else await reuters_archive_candidates(topic)
    public_candidates = await public_article_candidates(topic, source) if not stored else []
    feed_hints = not stored and not public_candidates and not archive_hints and supports_public_feed(source)
    if feed_hints:
        discovered = (await publisher_article_hints(topic) if domains == ('nationalreview.com',)
                      else await publisher_article_hints(topic, source))
    else:
        discovered = []
    hints = public_candidates or archive_hints or discovered
    originals_only = bool(archive_hints or public_candidates)
    if hints and not archive_hints:
        descriptions = await public_feed_excerpts([h['url'] for h in hints if not h.get('excerpt')], source)
        for hint in hints:
            if hint['url'] in descriptions:
                hint['excerpt'] = descriptions[hint['url']]
    if feed_hints and domains != ('nationalreview.com',):
        # A loose title match is only discovery. Do not redirect a compatible
        # publisher search to unrelated or inaccessible metadata-only articles.
        hints = [hint for hint in hints if hint.get('excerpt')]
    verified = [h for h in hints if h.get('excerpt')]
    if verified and not originals_only and settings.ai_cost_guard_enabled:
        try:
            budget = await budget_snapshot()
            ceiling = budget['daily_limit_usd']
            if budget_scope.get() != 'compare':
                ceiling *= 1 - budget['compare_share']
            bound = message_bound({'model': resolve(SEARCH_OPERATION).model, 'max_tokens': 2048,
                'messages': [], 'tools': [{'name': 'web_search', 'max_uses': 1}]})
            if budget['enabled'] and ceiling > 0 and ceiling - budget['spent_usd'] - budget['held_usd'] < bound:
                # Available originals can still be assessed within the cap.
                # Unretrieved metadata cannot become a candidate in this mode.
                hints = verified
                originals_only = True
        except Exception as exc:
            logger.info('compare candidate budget preflight unavailable (%s); retaining atomic admission', type(exc).__name__)
    if hints:
        article_hint += ('\nPossible publisher articles (discovery hints only; verify the subject and timing):\n'
                         + '\n'.join(f"Candidate {i}: {h['title']} at {h['url']}\n{h.get('excerpt', '')}" for i, h in enumerate(hints))
                         + '\nDo not treat a suggested title or URL as reporting.\n')
        article_hint += (
            'First assess the provided original publisher excerpts. A missing web result or blocked full '
            'page does not erase usable reporting already supplied here. '
            'When a headline asks why or how, reporting about the SAME event/action and timing may '
            'contribute its own observations without answering that question. Never infer a cause or '
            'claim that the publisher explains it when the excerpt does not. '
            'Some candidates include original publisher excerpts. If such a candidate genuinely covers '
            'the requested subject and timing, its excerpt can supply limited reporting or an attributed '
            'opinion even when the page is blocked. Do not select a merely related event or passing reference. '
            'Confirm selection with a standalone line SELECTED_STORED_CANDIDATE=N using its candidate number. '
            'Never select a candidate without an excerpt.\n'
        )
        if feed_hints and domains != ('nationalreview.com',):
            article_hint += ('For a selected original, also return a standalone SUPPORTING_PASSAGE= line '
                             'with one contiguous verbatim self-contained passage of 6-60 words from its '
                             'body after Excerpt, up to 300 words if context requires it. This passage '
                             'must substantively address the same requested event or practice. Do not '
                             'stitch sentences, complete fragments, or borrow a headline. If none exists, '
                             'do not select that candidate.\n')
    search_tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}
    if domains:
        search_tool['allowed_domains'] = list(domains)
    target = topic
    if stored:
        target = stored['title']
    elif hints and (not feed_hints or domains == ('nationalreview.com',)):
        target = hints[0]['title']
    # Strip only a recognizable explanatory headline's rhetorical tail. The
    # full request below still governs subject, event and timing relevance.
    broad_subject = re.match(r"^([\w -]{3,60}?)\s+(?:don['’]t|doesn['’]t|do not|does not)\s+just\s+", topic, re.I) if not stored else None
    if broad_subject and 2 <= len(broad_subject[1].split()) <= 6:
        target = '"' + broad_subject[1].strip() + '"'
    query = f'site:{search_domains[0]} {target}' if search_domains else f'{source} {target}'
    query_instruction = f"Use one targeted search query: {query}\n"
    if search_domains and domains != ('nationalreview.com',):
        query_instruction = (f"Use one query restricted to site:{search_domains[0]}. Topic cue: {target}\n"
                             "Choose concise neutral subject/event terms, omitting rhetorical headline wording. "
                             "Preserve the distinguishing event/action from context, such as a court ruling "
                             "rather than an older policy announcement. "
                             "The requested topic may be another outlet's headline: do not require the selected "
                             "publisher to use the same headline or viewpoint.\n")
        if domains:
            query_instruction = (f'Use exactly one targeted search query: {target}\n'
                                 f'The search tool already restricts results to site:{search_domains[0]}. '
                                 "The requested topic may be another outlet's headline: do not require the selected "
                                 'publisher to use the same headline or viewpoint. Preserve the distinguishing '
                                 'event/action and timing from the full request.\n')
        if broad_subject:
            query_instruction += ('This is an explanatory topic about the named practice, not a request for an '
                                  'identically titled article. Match actual reporting about that practice.\n')
    if reuters and not stored:
        # Broad requests often put several competing options after a colon.
        # AND-ing all of them excludes reporting that uses different terms.
        # Search the subject; the model still checks the entire request below.
        subject = target.partition(':')[0].strip()
        if len(subject.split()) < 2:
            subject = target
        query_instruction = (
            f'Use exactly one targeted search query: Reuters {subject}\n'
            'Do not restrict the query to reuters.com: it would exclude Reuters republications. '
            f'Only reuters.com and verified Reuters-byline articles at {PARTNER_DOMAINS} can supply evidence. '
            'Search results from other hosts cannot supply Reuters claims. '
            'Reuters reporting can be republished by KSL or WTVB with a Reuters article byline. '
            'Cite the actual Reuters-authored article passages; a Reuters photo credit or a mention of '
            'Reuters in another writer\'s article is not Reuters reporting. The topic may be editorial: '
            'do not require the selected publisher to use the same headline or viewpoint. '
            'For a broad ongoing issue, relevant reporting from the past year '
            'may be useful; keep its publication date and do not present it as a new event. '
            'For a specific event, require the same event and timing.\n'
        )
    call_tools = {'tools': [search_tool]}
    selection_prompt = None
    if originals_only:
        # Public retrieval already supplied verified originals. Reuse the
        # search-stage call solely for relevance selection, without reserving
        # or billing a web-tool round trip that is not needed.
        call_tools = {}
        query_instruction = (
            f'Assess the verified {source} original excerpts above against the full requested topic. '
            'Select a candidate only if it substantively covers the same subject and, for a specific '
            'event, the same event/action and timing. Related background or shared keywords alone '
            'do not match a specific event. For broad ongoing issues, older reporting within the '
            'provided date window may be relevant; never imply it reports a new event. '
            f'Return only SELECTED_STORED_CANDIDATE=N, or No relevant coverage found from {source}. '
            'Do not search, infer missing facts or use outside knowledge.\n'
        )
        if public_candidates:
            query_instruction = (
                f'Assess only the verified {source} originals against the full requested topic. '
                'Do not search or use outside knowledge. '
                'For a broad explanatory topic, a concrete case study can match when the supplied '
                'reporting explicitly documents the mechanism or practice named in the request. '
                'The article need not be a general overview or make that mechanism its headline or '
                'primary focus. A product example that explicitly demonstrates the requested practice '
                'can contribute on that narrow point. Judge the practice actually described rather than '
                'requiring the publisher to use the headline terminology. For example, a company generating '
                'AI characterizations or personal judgments from assembled records is a concrete example '
                'of inference about people; a guide to opting out of record collection alone is not. '
                'Shared keywords or inferred connections still '
                'do not qualify, and a specific event still requires the same event/action and timing. '
                'Before selecting, locate a verbatim self-contained passage of 6-60 words explicitly '
                'supporting the requested subject. Prefer a short quotation; longer contiguous passages up to '
                '300 words are accepted when needed for context. If none exists, return no relevant coverage. '
                'For a match, return exactly two lines: SELECTED_STORED_CANDIDATE=N and '
                'SUPPORTING_PASSAGE=the verbatim passage. Copy ONE CONTIGUOUS substring from the '
                'article body after Excerpt, with its original words and punctuation. Never stitch '
                'separate passages, insert ellipses, paraphrase, or add surrounding quotation marks. '
                f'Otherwise return No relevant coverage found from {source}. Do not add reasoning.\n'
            )
        selection_prompt = f'Requested topic: {json.dumps(topic)}\n{article_hint}\n{query_instruction}'
    request_messages = [{"role": "user", "content": selection_prompt or (
        f'Retrieve coverage from {source} about: {json.dumps(topic)}\n'
        f'Current date (UTC): {datetime.now(timezone.utc).date().isoformat()}\n{article_hint}\n{query_instruction}'
        'Match the subject and timing. For a broad ongoing issue, dated reporting within the past year may be relevant; '
        'never substitute a related event for a specific event. Return one or two brief verbatim publisher passages '
        '(8-30 words each) with citations, preserving attribution and uncertainty. Do not paraphrase quotations. '
        'Public search-index passages may supply reporting when a direct page is blocked. '
        'A blocked page alone does not establish that no coverage exists. '
        'Headlines, URLs, access messages and another publisher are not evidence. '
        f'If no relevant publisher reporting is accessible, return No relevant coverage found from {source}.') }]
    max_output = 2048
    if originals_only:
        max_output = 1024
    elif settings.ai_cost_guard_enabled:
        try:
            budget = await budget_snapshot()
            ceiling = budget['daily_limit_usd']
            if budget_scope.get() != 'compare':
                ceiling *= 1 - budget['compare_share']
            available = ceiling - budget['spent_usd'] - budget['held_usd']
            if budget['enabled'] and ceiling > 0:
                for size in (2048, 1024, 512):
                    params = {'model': resolve(SEARCH_OPERATION).model, 'max_tokens': size,
                              'messages': request_messages, **call_tools}
                    if message_bound(params) <= available:
                        max_output = size
                        break
        except Exception as exc:
            logger.info('compare output preflight unavailable (%s); retaining atomic admission', type(exc).__name__)
    try:
        response = await asyncio.wait_for(
            budgeted_message(client, SEARCH_OPERATION,
                stream_response=True,
                model=resolve(SEARCH_OPERATION).model,
                max_tokens=max_output,
                temperature=0,
                **call_tools,
                messages=request_messages,
            ),
            timeout=PER_SOURCE_TIMEOUT,
        )
        log_usage(
            SEARCH_OPERATION,
            response,
            model=resolve(SEARCH_OPERATION).model,
            web_searches=count_web_searches(response),
        )

        # Extract text blocks from response
        text_parts = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)

        result = "\n".join(text_parts).strip()
        if not result:
            return (source, None)

        if originals_only:
            stop = getattr(response, 'stop_reason', None)
            logger.info('compare relevance selection: source=%s stop=%s', source, stop)
            if stop == 'max_tokens':
                return source, None

        selected = re.search(r'^[`*_]{0,3}SELECTED_STORED_CANDIDATE=(\d+)[`*_]{0,3}\s*$', result, re.M)
        if hints and selected and source_found_coverage(result):
            index = int(selected[1])
            if index < len(hints) and hints[index].get('excerpt'):
                if public_candidates or feed_hints and domains != ('nationalreview.com',):
                    passage = re.search(r'^[`*_]{0,3}SUPPORTING_PASSAGE=[`*_]{0,3}\s*(.+?)[`*_]{0,3}\s*$', result, re.M)
                    quote = passage[1].strip().strip('\"“”') if passage else ''
                    body = hints[index]['excerpt'].partition('\nExcerpt:\n')[2]
                    if (not 6 <= len(quote.split()) <= 300
                            or normalize_evidence_text(quote) not in normalize_evidence_text(body)):
                        logger.info('compare relevance selection: source=%s missing original support', source)
                        return source, None
                logger.info('search_sources: verified original publisher candidate for %s (%s)', source, hints[index]['url'])
                return source, hints[index]['excerpt']

        if search_domains:
            relevant = source_found_coverage(result)
            result = (publisher_search_excerpts(response, search_domains, stored['url'] if stored else None)
                      if relevant else None)
            if not result and relevant and reuters and not stored:
                result = await reuters_partner_excerpts(response)
            if not result:
                logger.info("search_sources: no cited publisher reporting from %s", source)
                return (source, None)

        logger.info("search_sources: got %d chars from %s", len(result), source)
        return (source, result)

    except BudgetUnavailable:
        raise
    except asyncio.TimeoutError:
        logger.warning("search_sources: timed out for %s after %ds", source, PER_SOURCE_TIMEOUT)
        return (source, None)
    except Exception as e:
        logger.error("search_sources: failed for %s: %s", source, e)
        return (source, None)


def source_found_coverage(text: str | None) -> bool:
    """Whether a search_one_source result counts as real coverage."""
    if not text or not text.strip():
        return False
    missing = r"no (?:relevant )?(?:coverage|articles?) (?:was |were )?found|coverage (?:was |is )?unavailable"
    return re.search(missing, text, re.I) is None


async def search_sources_node(state: CompareState) -> dict:
    """Search each source in parallel using Claude web_search."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)
    topic = _sanitize_text(state["topic"])
    # Validate sources against the curated allowlist; reject unknown names.
    allowed = await load_allowed_sources()
    sources = filter_allowed_sources(state["sources"], allowed)
    if not sources:
        return {
            "search_results": {},
            "errors": ["No valid sources provided"],
        }

    gate = asyncio.Semaphore(await compare_search_parallelism(len(sources)))
    async def search(source):
        async with gate:
            return await search_one_source(client, topic, source)
    results = await asyncio.gather(
        *[search(s) for s in sources],
        return_exceptions=True,
    )

    search_results: dict[str, str] = {}
    errors: list[str] = list(state.get("errors", []))
    capacity_errors = []

    for result in results:
        if isinstance(result, BudgetUnavailable):
            if result.code != "AI_BUDGET_EXCEEDED":
                raise result
            capacity_errors.append(result)
            continue
        if isinstance(result, Exception):
            errors.append(f"search: {result}")
            continue
        source_name, text = result
        if source_found_coverage(text):
            search_results[source_name] = text  # type: ignore[assignment]
        else:
            errors.append(f"No coverage found from {source_name}")

    if capacity_errors and not search_results:
        raise capacity_errors[0]

    logger.info(
        "search_sources: got results from %d/%d sources",
        len(search_results),
        len(sources),
    )

    return {"search_results": search_results, "errors": errors,
            "capacity_limited": bool(capacity_errors)}


async def extract_and_compare_node(state: CompareState) -> dict:
    """Extract claims from search results and compare across sources."""
    search_results = state.get("search_results", {})

    if not search_results:
        return {
            "comparison": "Could not find relevant coverage from any source.",
            "claims": [],
        }

    # A complete short public standfirst is safe to quote, but too limited to
    # turn into inferred article facts or a purported dispute. Keep it out of
    # model grouping and append its checked literal quotation below.
    model_reporting = {s: text for s, text in search_results.items()
                       if not (text.startswith(PUBLIC_DESCRIPTION_PREFIX) and _complete_short_excerpt(text))}
    if not model_reporting:
        claims = _retain_outlet_passages([], None, search_results)
        return {"comparison": _grounded_comparison_summary(claims, list(search_results)),
                "claims": claims, "analyzed_sources": list(search_results)}

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=2)

    # Format source texts for the prompt
    source_texts = ""
    for source, text in model_reporting.items():
        source_texts += f"\n--- {source.upper()} ---\n{text}\n"

    sources_list = list(model_reporting)

    response = await budgeted_message(client, EXTRACT_OPERATION,
        model=resolve(EXTRACT_OPERATION).model,
        max_tokens=4096,
        temperature=0,
        messages=[{
            "role": "user",
            "content": f"""Analyze the following news coverage of "{_sanitize_text(state['topic'])}" from multiple sources.

{source_texts}

Your task:
Extract only claims directly relevant to the requested subject. A case study should contribute its evidence about that subject, not tangential facts or statistics from the rest of the article. For each outlet, prefer its own narrow, named example over an unsupported industry-wide paraphrase.
Only compare the {len(sources_list)} successfully retrieved sources in Available sources below.
Do not mention or count any other outlet. If only one source is available, there is no multi-source agreement.
Stored publisher excerpts may be incomplete. Use only facts or explicitly attributed opinions present in the supplied reporting. Missing details in an excerpt do not prove an outlet omitted them from its full article.
For a concrete case study, scope the claim to that named example rather than generalizing to an entire industry. Resolve relative publication times from the provided publication date; do not present an older launch as happening today.
Preserve attribution, uncertainty, timing and scope: an economist's estimate is not an official result; a forecast is not a released measurement; an allegation is not an established act. Retain words such as estimated, forecast, expected, could, only, remaining and up to when they qualify the proposition. Include the qualifier in both the supporting quotation and the claim; never trim it away to make a stronger statement.
First assess EACH available outlet independently in outlet_passages: select one verbatim passage of 6-30 words expressing a relevant, self-contained fact or opinion. Use null when none exists. A complete subject-and-predicate clause within a truncated excerpt is usable; select only that intact clause. Never return an unfinished word or trailing ellipsis, complete missing words, or infer the missing context. Do not use headline/URL metadata, teasers, or fragments without a complete proposition. This assessment comes BEFORE grouping claims, so richer articles do not crowd out a short editorial excerpt.
An article's headline may establish the topic of its excerpt, but cannot supply evidence for a claim. An editorial view about the underlying issue is a relevant contribution even when its excerpt does not repeat the event name. Retain it as that outlet's view without inferring a stance on specific policy details.
For EVERY outlet attributed to a grouped claim, provide a verbatim supporting quotation of 6-30 words from that outlet's supplied reporting. The quotation must support that specific claim. Never borrow evidence from another outlet or from headline/URL metadata. If an excerpt does not support that fact or attributed opinion, do not credit that outlet with it, even when other outlets confirm it.
Use atomic claims. Do not blend different publishers’ descriptions using alternatives such as A or B. Each credited publisher must explicitly support BOTH alternatives; one observed symptom does not justify adding another. A crease is not a crack. For a shared claim, EVERY credited outlet must support every named law, list member, number and substantive clause. If support differs, split the claims rather than combining one outlet’s details into a common statement.
Select the verbatim supporting passage first, then write only the claim it supports. Do not invent or paraphrase a quotation to fit a claim already written. Where the text supports it, include a factual claim or clearly attributed position from each available outlet; do not silently omit an outlet's supported perspective. An outlet with no supportable passage may be omitted.
1. Extract up to 8 supportable claims. A short excerpt may support only one claim; that is valid. Do not pad to a minimum. Keep an opinion or interpretation explicitly attributed to its outlet instead of presenting it as an established fact. Return no claims only when none is supported by the supplied reporting.
2. For each claim, determine the agreement level:
   - "unanimous": every available source reports this claim (at least two sources)
   - "majority": more than half of available sources report this claim
   - "partial": at least two, but no more than half, report this claim
   - "disputed": sources contradict each other on this point
   - "unique": only one source reports this
3. Include an evidence map for every claim. Unsupported outlet attributions will be removed.

Return ONLY a JSON object with this structure:
{{
  "outlet_passages": {{"source1": "verbatim self-contained passage from source1", "source2": null}},
  "claims": [
    {{
      "evidence": {{"source1": "verbatim supporting quotation from source1 reporting", "source2": "verbatim supporting quotation from source2 reporting"}},
      "claim": "specific factual statement",
      "agreement": "unanimous",
      "sources": ["source1", "source2"]
    }},
    {{
      "evidence": {{"source1": "verbatim quotation supporting the claim", "source2": "verbatim quotation contradicting the claim"}},
      "claim": "a disputed point",
      "agreement": "disputed",
      "sources_for": ["source1"],
      "sources_against": ["source2"]
    }}
  ]
}}

Use EXACT keys from Available sources for outlet_passages, evidence and source lists.
Available sources: {json.dumps(sources_list)}
Return ONLY the JSON, no other text.""",
        }],
    )
    log_usage(EXTRACT_OPERATION, response, model=resolve(EXTRACT_OPERATION).model)

    # Extract text from response
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    # Parse JSON from response
    parsed = _extract_json_object(text)

    if not parsed:
        logger.error("Failed to parse comparison JSON: %s", text[:500])
        return {
            "comparison": "Comparison analysis could not be completed. Try again.",
            "claims": [],
            "errors": state.get("errors", []) + ["Failed to parse comparison JSON"],
        }

    claims = parsed.get("claims", [])
    if not isinstance(claims, list):
        return {"comparison": "", "claims": [], "analyzed_sources": []}

    original_count = len(claims)
    claims = _ground_claim_attributions(claims, model_reporting, include_evidence=True)
    claims = _retain_outlet_passages(claims, parsed.get("outlet_passages"), search_results,
                                    include_evidence=True, include_represented=True, max_claims=None)
    backups = [c for c in claims if c.get('_quote_backup')]
    unreviewed_sources = set()
    if claims:
        try:
            claims, unreviewed_sources = await asyncio.wait_for(
                verify_claim_support(client, claims, search_results, topic=_sanitize_text(state['topic'])), timeout=10)
        except BudgetUnavailable as exc:
            if exc.code != 'AI_BUDGET_EXCEEDED':
                raise
            logger.info('compare claim support review: capacity unavailable; using original quotations')
            claims = []
            unreviewed_sources = set(search_results)
        except Exception as exc:
            logger.warning('compare claim support review unavailable (%s); using original quotations', type(exc).__name__)
            claims = []
            unreviewed_sources = set(search_results)
    # Only failed reviewers may fall back to unreviewed literal originals.
    # Successfully rejected passages cannot re-enter through this path.
    claims.extend(c for c in backups if set(c.get('sources', [])) & unreviewed_sources and c not in claims)
    represented = set().union(*(set(c.get('sources', []) + c.get('sources_for', []) + c.get('sources_against', []))
                                for c in claims if not c.get('_quote_backup')))
    claims = [c for c in claims if not c.get('_quote_backup') or not set(c.get('sources', [])) <= represented]
    for claim in claims:
        claim.pop('_quote_backup', None)
    claims = _retain_outlet_passages(claims, None, {})
    review_limited = bool(unreviewed_sources)
    comparison = _grounded_comparison_summary(claims, list(search_results))
    for source in search_results:
        count = sum(source in c.get('sources', []) + c.get('sources_for', []) + c.get('sources_against', []) for c in claims)
        logger.info('compare grounded contributions: source=%s claims=%d', source, count)
    logger.info("extract_and_compare: %d grouped candidates, %d retained contributions", original_count, len(claims))
    return {"comparison": comparison, "claims": claims, "analyzed_sources": list(search_results),
            "capacity_limited": state.get("capacity_limited", False) or review_limited}


def _drops_numeric_estimate(claim: str, quote: str, body: str) -> bool:
    """Catch a narrow certainty reversal even when a quote trims its prefix.

    A verbatim subclause inside an attributed forecast cannot establish an
    unqualified numerical result. Only scope the forecast's own sentence;
    nearby actual figures remain usable. This is not general entailment QA.
    """
    if not re.search(r'\d', claim) or re.search(
        r'\b(?:estimate[ds]?|forecast(?:s|ed)?|projected|projections?|expect(?:s|ed)?|'
        r'predicted|predictions?|anticipate[ds]?|would|could|may|might)\b', claim, re.I
    ):
        return False
    for sentence in re.finditer(
        r'\b(?:economists?|analysts?|report|company|firm|it)\s+'
        r'(?:estimate[ds]?|forecast(?:s|ed)?|project(?:s|ed)?|expect(?:s|ed)?|predict(?:s|ed)?|anticipate[ds]?)\b'
        r'[^.!?]*(?:\.\d+[^.!?]*)*', body
    ):
        if quote in sentence[0]:
            return True
    return False


def _adds_unsupported_speech_act(claim: str, body: str) -> bool:
    # Reporting ongoing work is not an offer or promise by the speaker.
    if re.search(r'\boffer(?:s|ed|ing)?\s+to\b', claim, re.I):
        if not re.search(r'\b(?:offer(?:s|ed|ing)?|volunteer(?:s|ed|ing)?|willing|ready)\b|'
                         r'\b(?:can|could)\s+(?:provide|share|supply|help)\b', body, re.I):
            return True
    if re.search(r'\b(?:promise[ds]?|pledge[ds]?|vow(?:s|ed)?)\b', claim, re.I):
        if not re.search(r'\b(?:promise[ds]?|pledge[ds]?|vow(?:s|ed)?|commit(?:s|ted|ment)?)\b', body, re.I):
            return True
    return False


def _missing_named_law(claim: str, body: str) -> bool:
    aliases = (
        r'\b(?:hipaa|health insurance portability and accountability act)\b',
        r'\b(?:fcra|fair credit reporting act)\b',
        r'\b(?:ccpa|california consumer privacy act)\b',
        r'\b(?:gdpr|general data protection regulation)\b',
    )
    return any(re.search(law, claim, re.I) and not re.search(law, body, re.I) for law in aliases)


def _source_evidence_key(source):
    value = re.sub(r'[^a-z0-9]', '', re.sub(r'^the\s+', '', source.strip().casefold()))
    return {'ft': 'financialtimes', 'cbs': 'cbsnews', 'fox': 'foxnews'}.get(value, value)


def _ground_claim_attributions(claims: list, reporting: dict[str, str], *, include_evidence: bool = False) -> list[dict]:
    """Reject borrowed/invented quotes before counting an outlet for a claim.

    This checks provenance, not factual truth or semantic entailment. The model
    still interprets the quote, but can no longer silently attribute a fact to
    an excerpt without returning supporting text from that excerpt.
    """
    def normalize(text: str) -> str:
        return normalize_evidence_text(text)

    bodies = {}
    for source, text in reporting.items():
        if text.startswith(("Stored publisher excerpt", "Retrieved publisher excerpts", PUBLIC_DESCRIPTION_PREFIX)):
            text = text.partition("\nExcerpt:\n")[2]
        bodies[_source_evidence_key(source)] = (source, normalize(text))
    grounded = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim"), str):
            continue
        evidence = claim.get("evidence")
        if not isinstance(evidence, dict):
            continue
        quotes = {_source_evidence_key(s): q for s, q in evidence.items() if isinstance(s, str)}
        cleaned = {"claim": claim["claim"], "agreement": claim.get("agreement", "unique")}
        if include_evidence:
            cleaned["evidence"] = {}
        for field in ("sources", "sources_for", "sources_against"):
            credited = claim.get(field, [])
            cleaned[field] = []
            for source in credited if isinstance(credited, list) else []:
                if not isinstance(source, str):
                    continue
                source_key = _source_evidence_key(source)
                quote = quotes.get(source_key)
                if (source_key in bodies and isinstance(quote, str) and len(quote.split()) >= 6
                        and normalize(quote) in bodies[source_key][1]
                        and not _drops_numeric_estimate(claim['claim'], normalize(quote), bodies[source_key][1])
                        and not _missing_named_law(claim['claim'], bodies[source_key][1])
                        and not _adds_unsupported_speech_act(claim['claim'], bodies[source_key][1])):
                    name = bodies[source_key][0]
                    if name not in cleaned[field]:
                        cleaned[field].append(name)
                        if include_evidence:
                            cleaned["evidence"][name] = quote
        if cleaned["agreement"] == "disputed":
            if not cleaned["sources_for"] or not cleaned["sources_against"]:
                continue
        elif not cleaned["sources"]:
            continue
        grounded.append(cleaned)
    return grounded


def _complete_short_excerpt(text: str) -> str | None:
    body = text.partition("\nExcerpt:\n")[2].strip()
    if 6 <= len(body.split()) <= 30 and complete_description(body):
        return body
    return None


def _retain_outlet_passages(claims: list[dict], passages, reporting: dict[str, str], *,
                            include_evidence: bool = False, include_represented: bool = False,
                            max_claims: int | None = 8) -> list[dict]:
    """Retain a checked quotation when grouping omitted an outlet entirely.

    Quote the passage itself, rather than inventing a fallback paraphrase or
    treating editorial opinion as established fact. This uses the same model
    call and provenance guard; null, metadata and unfinished text add nothing.
    Reserve coverage before filling the eight slots with additional claims.
    """
    candidates = list(claims)

    def credited(claim):
        return set(claim.get("sources", []) + claim.get("sources_for", []) + claim.get("sources_against", []))

    represented = set() if include_represented else set().union(*(credited(c) for c in candidates))
    passages = ({_source_evidence_key(s): q for s, q in passages.items() if isinstance(s, str)}
                if isinstance(passages, dict) else {})
    for source, text in reporting.items():
        quote = passages.get(_source_evidence_key(source))
        # Original excerpts already passed event-relevance retrieval.
        # Preserve a whole short sentence even if the model overlooks opinion.
        if text.startswith(("Stored publisher excerpt", "Retrieved publisher excerpts", PUBLIC_DESCRIPTION_PREFIX)):
            quote = _complete_short_excerpt(text) or quote
        if (source in represented or not isinstance(quote, str)
                or not 6 <= len(quote.split()) <= 30 or quote.rstrip().endswith(("...", "…"))):
            continue
        fallback = {"claim": f'{source} writes: “{quote}”', "agreement": "unique",
                    "sources": [source], "evidence": {source: quote}}
        additions = _ground_claim_attributions([fallback], reporting, include_evidence=include_evidence)
        if include_represented:
            for addition in additions:
                addition['_quote_backup'] = True
        candidates.extend(additions)

    if max_claims is None:
        return candidates
    selected = set()
    covered = set()
    # Keep at least one supported contribution per outlet, then disputes.
    for i, claim in enumerate(candidates):
        sources = credited(claim)
        if len(selected) < max_claims and sources - covered:
            selected.add(i)
            covered.update(sources)
    for i, claim in enumerate(candidates):
        if len(selected) < max_claims and claim.get("agreement") == "disputed":
            selected.add(i)
    for i in range(len(candidates)):
        if len(selected) >= max_claims:
            break
        selected.add(i)
    return [c for i, c in enumerate(candidates) if i in selected]


def _grounded_comparison_summary(claims: list[dict], sources: list[str]) -> str:
    """Build the headline comparison from checked attribution counts.

    A separate free-form model summary could reintroduce unsupported 'all
    outlets confirm' claims even after their source lists had been corrected.
    """
    if not claims:
        return "No claims could be tied to supporting passages in the retrieved text."
    shared = [c for c in claims if c.get("agreement") != "disputed" and c.get("sources")]
    if not shared:
        return "Review the claims below for the differences supported by the retrieved reporting."
    broadest = max(shared, key=lambda c: len(c["sources"]))
    statement = broadest["claim"].rstrip(". ")

    def attributed_quotation(claim: dict) -> str | None:
        if (len(claim["sources"]) == 1
                and claim["claim"].startswith(f'{claim["sources"][0]} writes: “')):
            text = claim["claim"].strip()
            return text if text.rstrip('”').endswith((".", "!", "?")) else text + "."
        return None

    quotation = attributed_quotation(broadest)
    if len(sources) == 1:
        return quotation or f"{sources[0]} reports: {statement}."
    summary = quotation or f"{len(broadest['sources'])} of {len(sources)} retrieved outlets report: {statement}."
    unique = next((c for c in shared if len(c["sources"]) == 1 and c is not broadest), None)
    if unique:
        summary += " " + (attributed_quotation(unique)
                          or f"{unique['sources'][0]} also reports: {unique['claim'].rstrip('. ')}.")
    return summary


async def format_response_node(state: CompareState) -> dict:
    """Validate and clean up the response (no LLM call)."""
    claims = state.get("claims", [])
    cleaned_claims: list[dict] = []
    provenance = comparison_provenance(state)
    analyzed = provenance["sources_analyzed"]
    names = {s.casefold(): s for s in analyzed}

    def known_sources(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            names[s.strip().casefold()] for s in value
            if isinstance(s, str) and s.strip().casefold() in names
        ))

    for claim in claims:
        if not isinstance(claim, dict) or "claim" not in claim:
            continue

        cleaned: dict = {
            "claim": str(claim["claim"]),
            "agreement": claim.get("agreement", "unique"),
        }

        # Ensure agreement is valid
        if cleaned["agreement"] not in ("unanimous", "majority", "partial", "disputed", "unique"):
            cleaned["agreement"] = "unique"

        # Normalize source fields
        if cleaned["agreement"] == "disputed":
            supporters = known_sources(claim.get("sources_for", []))
            opponents = known_sources(claim.get("sources_against", []))
            overlap = set(supporters) & set(opponents)
            cleaned["sources_for"] = [s for s in supporters if s not in overlap]
            cleaned["sources_against"] = [s for s in opponents if s not in overlap]
            cleaned["sources"] = []
            if not cleaned["sources_for"] or not cleaned["sources_against"]:
                continue
        else:
            cleaned["sources"] = known_sources(claim.get("sources", []))
            cleaned["sources_for"] = []
            cleaned["sources_against"] = []
            count = len(cleaned["sources"])
            if not count:
                continue
            cleaned["agreement"] = (
                "unique" if count == 1 else "unanimous" if count == len(analyzed)
                else "majority" if count > len(analyzed) / 2 else "partial"
            )

        cleaned_claims.append(cleaned)

    # Disputed first — cross-spectrum disagreement is the feature's whole
    # point, so it leads instead of sitting mid-list. Stable sort keeps the
    # model's ordering within each band.
    agreement_order = {"disputed": 0, "majority": 1, "unanimous": 2, "partial": 3, "unique": 4}
    cleaned_claims.sort(key=lambda c: agreement_order.get(c["agreement"], 4))

    comparison = state.get("comparison", "") if analyzed else ""
    unavailable = [s for s in provenance["sources_requested"] if s not in analyzed]
    numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    count_claims = re.findall(r"\ball\s+(one|two|three|four|five|\d+)\s+sources?\b", comparison, re.I)
    wrong_count = any(numbers.get(n.lower(), int(n) if n.isdigit() else 0) != len(analyzed)
                      for n in count_claims)
    mentions_unavailable = any(re.search(r"(?<!\w)" + re.escape(s) + r"(?!\w)", comparison, re.I)
                               for s in unavailable)
    if analyzed and (wrong_count or mentions_unavailable):
        comparison = f"Compared coverage from {', '.join(analyzed)}. Review the sourced claims below."
    return {"claims": cleaned_claims, "comparison": comparison}


# --- JSON parsing helpers ---


def _extract_json_object(text: str) -> dict | None:
    """Extract a JSON object from potentially messy LLM output."""
    text = text.strip()

    # Strategy 1: direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown fences
    cleaned = re.sub(r"```json\n?", "", text)
    cleaned = re.sub(r"```\n?", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 3: find { ... } brackets
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


# --- Build the graph ---


def build_compare_graph():
    """Build and compile the LangGraph comparison workflow."""
    graph = StateGraph(CompareState)

    graph.add_node("search_sources", search_sources_node)
    graph.add_node("extract_and_compare", extract_and_compare_node)
    graph.add_node("format_response", format_response_node)

    graph.set_entry_point("search_sources")
    graph.add_edge("search_sources", "extract_and_compare")
    graph.add_edge("extract_and_compare", "format_response")
    graph.add_edge("format_response", END)

    return graph.compile()
