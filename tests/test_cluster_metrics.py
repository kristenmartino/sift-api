"""Tests for services.cluster_metrics.

Tier 1 of the clustering eval: pure math, no LLM, no network, milliseconds.

The metric implementations are hand-rolled to keep numpy/scipy out of the
Railway image, which means THEY need verifying rather than trusting. The values
in SKLEARN_GOLDEN below were produced once, offline, in a throwaway venv:

    python3 -m venv /tmp/sk_verify
    /tmp/sk_verify/bin/pip install scikit-learn        # 1.9.0
    /tmp/sk_verify/bin/python -c '
    from sklearn.metrics import adjusted_rand_score, homogeneity_completeness_v_measure
    t, p = [0,0,0,1,1,2,3,4,5], [0,0,1,1,1,2,3,4,5]
    print(adjusted_rand_score(t,p), homogeneity_completeness_v_measure(t,p))'

Best of both: sklearn-verified numbers, zero runtime dependency.

MUTATION SCORE (mutmut, config in setup.cfg)
    2026-07-30: 376 mutants, 308 killed, 68 survived -> 82% kill rate

That run found a real gap: `fn += 1` -> `fn = 1` survived everything, because
the aggregate rates came out identical on every case asserted and no test
checked a raw count above 1. Fixed by
TestPairwiseScores::test_raw_counts_accumulate_rather_than_latch. Re-run with
`.venv/bin/mutmut run` and update this number when it moves.
"""

from __future__ import annotations

import pytest

from services.cluster_metrics import (
    adjusted_rand_index,
    evaluate,
    multi_outlet_partition,
    pairwise_scores,
    partition_from_event_ids,
    partition_from_groups,
    topic_conflation_rate,
    v_measure,
)

# (true, pred) -> (ari, homogeneity, completeness, v_measure)
SKLEARN_GOLDEN = {
    "realistic": (
        ([0, 0, 0, 1, 1, 2, 3, 4, 5], [0, 0, 1, 1, 1, 2, 3, 4, 5]),
        (0.4375, 0.8734806581894542, 0.8734806581894542, 0.8734806581894542),
    ),
    "identical": (
        ([0, 0, 1, 1, 2], [0, 0, 1, 1, 2]),
        (1.0, 1.0, 1.0, 1.0),
    ),
    "all_singletons": (
        ([0, 0, 0, 1, 1, 2], [0, 1, 2, 3, 4, 5]),
        (0.0, 1.0, 0.5644754678724234, 0.7216162598446754),
    ),
    "all_one_cluster": (
        ([0, 0, 1, 1, 2, 2], [0, 0, 0, 0, 0, 0]),
        (0.0, 0.0, 1.0, 0.0),
    ),
    "worked_2x2": (
        ([0, 0, 0, 0, 1, 1, 1, 1], [0, 0, 0, 1, 1, 1, 1, 1]),
        (0.4948453608247423, 0.5487949406953986, 0.5749951688786841, 0.5615896365639194),
    ),
    "perfect_split": (
        ([0, 0, 0, 0], [0, 0, 1, 1]),
        (0.0, 1.0, 0.0, 0.0),
    ),
}


class TestAgainstSklearn:
    @pytest.mark.parametrize("name", list(SKLEARN_GOLDEN))
    def test_ari_matches_sklearn(self, name):
        (true, pred), (ari, _, _, _) = SKLEARN_GOLDEN[name]
        assert adjusted_rand_index(true, pred) == pytest.approx(ari, abs=1e-9)

    @pytest.mark.parametrize("name", list(SKLEARN_GOLDEN))
    def test_v_measure_matches_sklearn(self, name):
        (true, pred), (_, h, c, v) = SKLEARN_GOLDEN[name]
        got = v_measure(true, pred)
        assert got.homogeneity == pytest.approx(h, abs=1e-9)
        assert got.completeness == pytest.approx(c, abs=1e-9)
        assert got.v_measure == pytest.approx(v, abs=1e-9)


class TestChanceCorrection:
    """The property that makes ARI the headline metric.

    The system's real failure mode is producing no clusters at all — when a
    truncated response makes _parse_clusters return [], every article becomes a
    singleton. A metric that scores that highly is worse than no metric.
    """

    def test_all_singletons_scores_zero_not_high(self):
        true = [0, 0, 0, 1, 1, 2]
        pred = [0, 1, 2, 3, 4, 5]
        assert adjusted_rand_index(true, pred) == pytest.approx(0.0, abs=1e-9)

    @staticmethod
    def _purity(true: list, pred: list) -> float:
        """purity = (1/n) * sum over predicted clusters of the largest true
        class inside that cluster. Implemented here, in the test only, purely
        to demonstrate why it is NOT in cluster_metrics.py."""
        from collections import Counter, defaultdict

        members: dict[object, list] = defaultdict(list)
        for t, p in zip(true, pred, strict=True):
            members[p].append(t)
        return sum(Counter(v).most_common(1)[0][1] for v in members.values()) / len(true)

    def test_purity_would_have_scored_all_singletons_perfectly(self):
        """Documents why purity is deliberately not implemented as a gate: it
        is 1.0 for exactly the degenerate output we most need to catch, while
        the chance-corrected ARI correctly scores it ~0."""
        true = [0, 0, 0, 1, 1, 2]
        all_singletons = [0, 1, 2, 3, 4, 5]

        assert self._purity(true, all_singletons) == 1.0
        assert adjusted_rand_index(true, all_singletons) < 0.01

        # Sanity-check the purity helper itself against a case where it is NOT
        # 1.0, so the assertion above is not passing by construction.
        merged_all = [0, 0, 0, 0, 0, 0]
        assert self._purity(true, merged_all) == pytest.approx(3 / 6)

    def test_lumping_everything_together_also_scores_zero(self):
        assert adjusted_rand_index([0, 0, 1, 1, 2, 2], [0] * 6) == pytest.approx(0.0, abs=1e-9)


class TestHomogeneityVsCompleteness:
    """Why the two halves are reported separately: they name the direction."""

    def test_over_splitting_shows_as_low_completeness(self):
        true = [0, 0, 0, 0]
        pred = [0, 0, 1, 1]  # one event scattered across two clusters
        got = v_measure(true, pred)
        assert got.homogeneity == pytest.approx(1.0)
        assert got.completeness == pytest.approx(0.0)

    def test_over_merging_shows_as_low_homogeneity(self):
        true = [0, 0, 1, 1]
        pred = [0, 0, 0, 0]  # two events fused
        got = v_measure(true, pred)
        assert got.homogeneity == pytest.approx(0.0)
        assert got.completeness == pytest.approx(1.0)


class TestPartitionFromGroups:
    """The reconstruction that inflates every metric if it is wrong.

    cluster_articles returns ONLY groups of >= 2 and omits everything else.
    """

    def test_omitted_articles_become_distinct_singletons(self):
        labels = partition_from_groups(5, [[1, 2]])
        assert labels[0] == labels[1]
        # 3, 4, 5 were omitted -> three distinct singletons
        assert len({labels[2], labels[3], labels[4]}) == 3
        assert labels[2] != labels[0]

    def test_indices_are_treated_as_one_based(self):
        labels = partition_from_groups(3, [[1, 3]])
        assert labels[0] == labels[2]
        assert labels[1] != labels[0]

    def test_empty_group_list_yields_all_singletons(self):
        labels = partition_from_groups(4, [])
        assert len(set(labels)) == 4

    def test_two_groups_are_kept_distinct(self):
        labels = partition_from_groups(4, [[1, 2], [3, 4]])
        assert labels[0] == labels[1]
        assert labels[2] == labels[3]
        assert labels[0] != labels[2]

    def test_out_of_range_indices_are_ignored_not_crashing(self):
        # _parse_clusters already rejects these, but the metric must not be the
        # thing that explodes if one slips through.
        labels = partition_from_groups(3, [[1, 99]])
        assert len(labels) == 3

    def test_a_perfect_prediction_round_trips_to_ari_one(self):
        true = partition_from_event_ids(["a", "a", None, "b", "b"])
        pred = partition_from_groups(5, [[1, 2], [4, 5]])
        assert adjusted_rand_index(true, pred) == pytest.approx(1.0)


class TestPartitionFromEventIds:
    def test_none_means_singleton(self):
        labels = partition_from_event_ids(["a", "a", None, None])
        assert labels[0] == labels[1]
        assert labels[2] != labels[3]
        assert labels[2] != labels[0]

    def test_repeated_event_ids_group(self):
        labels = partition_from_event_ids(["x", "y", "x"])
        assert labels[0] == labels[2]
        assert labels[1] != labels[0]


class TestPairwiseScores:
    def test_perfect_prediction(self):
        s = pairwise_scores([0, 0, 1, 1], [0, 0, 1, 1])
        assert (s.precision, s.recall, s.f1) == (1.0, 1.0, 1.0)

    def test_counts_are_right_on_a_hand_checked_case(self):
        # true: {0,1} together, {2,3} together. pred merges all four.
        # pred pairs = C(4,2) = 6; of those, 2 are truly together.
        s = pairwise_scores([0, 0, 1, 1], [0, 0, 0, 0])
        assert s.true_positives == 2
        assert s.false_positives == 4
        assert s.false_negatives == 0
        assert s.precision == pytest.approx(2 / 6)
        assert s.recall == pytest.approx(1.0)

    def test_raw_counts_accumulate_rather_than_latch(self):
        """Found by mutation testing (mutmut, 2026-07-30).

        The mutant `fn += 1` -> `fn = 1` survived every other test here: the
        aggregate rates (precision/recall/ARI) happened to come out the same on
        every case we asserted, and no test checked a raw count where the value
        exceeded 1. It reported false_negatives=1 for a case with 3.

        Asserting the counts on a case with several of each is what kills it.
        """
        # true: all three together. pred: all three apart. C(3,2) = 3 pairs,
        # every one a false negative.
        s = pairwise_scores([0, 0, 0], [0, 1, 2])
        assert s.false_negatives == 3
        assert s.true_positives == 0
        assert s.false_positives == 0

        # And the mirror image: all three apart, pred merges them -> 3 FPs.
        s = pairwise_scores([0, 1, 2], [0, 0, 0])
        assert s.false_positives == 3
        assert s.true_positives == 0
        assert s.false_negatives == 0

        # A case with several of all three at once.
        # true: {0,1,2} = event A, {3,4} = event B
        # pred: {0,1}   = cluster X, {2,3,4} = cluster Y
        # Enumerating all C(5,2)=10 pairs:
        #   TP (0,1),(3,4)            same in both
        #   FP (2,3),(2,4)            merged in pred, different events
        #   FN (0,2),(1,2)            same event, split by pred
        #   TN (0,3),(0,4),(1,3),(1,4)
        s = pairwise_scores([0, 0, 0, 1, 1], [0, 0, 1, 1, 1])
        assert s.true_positives == 2
        assert s.false_positives == 2
        assert s.false_negatives == 2
        assert s.true_positives + s.false_positives + s.false_negatives == 6

    def test_all_singletons_has_perfect_precision_by_convention(self):
        """Flattering on its own — which is why evaluate() always reports
        n_clusters_pred next to it."""
        s = pairwise_scores([0, 0, 1, 1], [0, 1, 2, 3])
        assert s.precision == 1.0
        assert s.recall == 0.0

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError, match="length mismatch"):
            pairwise_scores([0, 0], [0, 0, 1])


class TestMultiOutletPartition:
    """Scores what users actually see, per the >= 2 unique outlets gate in
    workflows/story_workflow.py:196."""

    def test_single_outlet_cluster_is_demoted_to_singletons(self):
        labels = [0, 0, 0]
        outlets = ["9to5Mac", "9to5Mac", "9to5Mac"]
        out = multi_outlet_partition(labels, outlets)
        assert len(set(out)) == 3, "a single-outlet cluster must not count as a story"

    def test_cross_outlet_cluster_survives(self):
        out = multi_outlet_partition([0, 0], ["NPR", "Reuters"])
        assert out[0] == out[1]

    def test_the_9to5mac_bug_is_visible_in_the_metric(self):
        """Regression for the real bug: four same-outlet posts rendered as
        'how 4 outlets covered this'. Raw pairwise precision looks perfect;
        multi-outlet precision correctly scores it 0."""
        true = [0, 1, 2, 3]  # ground truth: four unrelated single-outlet posts
        pred = [0, 0, 0, 0]  # clusterer merged them
        outlets = ["9to5Mac"] * 4

        assert pairwise_scores(true, pred).precision == 0.0
        mo = pairwise_scores(
            multi_outlet_partition(true, outlets),
            multi_outlet_partition(pred, outlets),
        )
        # After the outlet gate, the bogus story disappears entirely.
        assert mo.precision == 1.0
        assert mo.false_positives == 0

    def test_misaligned_inputs_raise(self):
        with pytest.raises(ValueError, match="must align"):
            multi_outlet_partition([0, 0], ["NPR"])


class TestTopicConflationRate:
    def test_zero_when_distractors_stay_apart(self):
        true = [0, 1]
        pred = [0, 1]
        assert topic_conflation_rate(true, pred, [(0, 1)]) == 0.0

    def test_one_when_distractors_are_wrongly_merged(self):
        true = [0, 1]
        pred = [0, 0]
        assert topic_conflation_rate(true, pred, [(0, 1)]) == 1.0

    def test_no_hard_pairs_is_zero_not_a_crash(self):
        assert topic_conflation_rate([0, 1], [0, 1], []) == 0.0

    def test_rejects_a_hard_pair_that_ground_truth_groups(self):
        """Guards the corpus: a 'distractor' pair labeled as the same event is
        a labeling error, not a model failure."""
        with pytest.raises(ValueError, match="grouped in ground truth"):
            topic_conflation_rate([0, 0], [0, 0], [(0, 1)])


class TestEvaluate:
    def test_reports_the_full_metric_set(self):
        true = [0, 0, 1, 1, 2]
        pred = [0, 0, 1, 2, 3]
        outlets = ["NPR", "Reuters", "AP", "BBC", "CNN"]
        rep = evaluate(true, pred, outlets=outlets, hard_pairs=[(0, 4)])

        assert rep.n_articles == 5
        assert rep.n_clusters_true == 3
        assert rep.n_clusters_pred == 4
        assert 0.0 <= rep.ari <= 1.0
        assert rep.multi_outlet_precision is not None
        assert rep.topic_conflation_rate == 0.0

    def test_optional_metrics_are_none_when_inputs_are_absent(self):
        rep = evaluate([0, 0, 1], [0, 0, 1])
        assert rep.multi_outlet_precision is None
        assert rep.topic_conflation_rate is None

    def test_singleton_rates_are_reported_so_degenerate_output_is_visible(self):
        rep = evaluate([0, 0, 0, 1, 1], [0, 1, 2, 3, 4])
        assert rep.singleton_rate_pred == 1.0
        assert rep.n_clusters_pred == 5

    def test_report_is_json_serializable_for_the_baseline_artifact(self):
        import json

        rep = evaluate([0, 0, 1], [0, 0, 1])
        assert json.loads(json.dumps(rep.to_dict()))["ari"] == 1.0
