"""clean_topic — the daily example's title-to-topic step (#195).

The first production run picked "WATCH LIVE: Trump signs order changing
schedule for children's vaccinations" as the standing topic. The comparison
was fine; the broadcast prefix on the marketing surface was not.
"""

from services.daily_compare import clean_topic


def test_plain_title_unchanged():
    assert clean_topic("Fed holds rates steady for a fourth meeting") == (
        "Fed holds rates steady for a fourth meeting"
    )


def test_watch_live_prefix_stripped():
    assert clean_topic(
        "WATCH LIVE: Trump signs order changing schedule for children's vaccinations"
    ) == "Trump signs order changing schedule for children's vaccinations"


def test_prefix_matching_is_case_insensitive():
    assert clean_topic("Breaking News: Senate passes stopgap bill") == (
        "Senate passes stopgap bill"
    )


def test_stacked_prefixes_all_stripped():
    assert clean_topic("BREAKING: WATCH: Court blocks tariff order") == (
        "Court blocks tariff order"
    )


def test_dash_and_pipe_separators():
    assert clean_topic("LIVE — Election results roll in") == "Election results roll in"
    assert clean_topic("Video | Inside the flooded delta") == "Inside the flooded delta"


def test_live_updates_variant():
    assert clean_topic("Live updates: Wildfire crews gain ground") == (
        "Wildfire crews gain ground"
    )


def test_prefix_word_without_separator_survives():
    # Only label-colon/dash forms are furniture; these are real sentences.
    assert clean_topic("Breaking with tradition, the court published audio") == (
        "Breaking with tradition, the court published audio"
    )
    assert clean_topic("Live music venues sue over noise rules") == (
        "Live music venues sue over noise rules"
    )


def test_all_furniture_title_kept_rather_than_emptied():
    assert clean_topic("LIVE:") == "LIVE:"


def test_length_cap_applies_after_stripping():
    long_tail = "x" * 200
    assert clean_topic(f"WATCH LIVE: {long_tail}") == "x" * 120
