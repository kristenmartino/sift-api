"""Deterministic clustering-quality metrics.

Scores a predicted article partition against human-labeled ground truth. Pure
arithmetic — no network, no LLM, no randomness — so the same inputs always
produce the same numbers and the eval can gate CI for free.

**Why hand-implemented rather than scikit-learn:** sift-api has no numeric
stack at all (see requirements.txt). sklearn pulls numpy + scipy, ~90 MB, into
a Railway image — and since prod and dev deps were only just split, it would
have shipped to production purely to serve an offline eval. All of this is
~60 lines of integer arithmetic plus math.log. The golden values in
tests/test_cluster_metrics.py were cross-checked against sklearn once, offline,
with the exact command recorded there.

**Partition representation.** A partition is a list of labels, one per article,
positionally aligned: `labels[i]` is the cluster id of article i. Label values
are arbitrary and compared only for equality, so `[0,0,1]` and `["a","a","b"]`
are the same partition.

**Metric choices, and two deliberate omissions:**

- ARI is the headline because it is *chance-corrected*. An all-singletons
  predictor — this system's actual failure mode when clustering silently
  returns [] — scores ~0.0 rather than looking respectable.
- V-measure is reported with homogeneity and completeness *separately*,
  because the pair tells you which direction a regression went and ARI alone
  cannot.
- **NMI is not computed.** V-measure is identically NMI under arithmetic-mean
  normalization. Two names for one number is padding, not coverage.
- **Purity is not computed as a gate.** It is 1.0 for an all-singletons
  prediction, i.e. trivially maximized by the exact failure this eval exists
  to detect.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict


# ─── partition construction ───────────────────────────────

def partition_from_groups(n_articles: int, groups: list[list[int]]) -> list[int]:
    """Build a full partition from `cluster_articles`-style output.

    `services.story_clusterer.cluster_articles` returns ONLY groups of >= 2 and
    omits every ungrouped article. Each omitted index is its own singleton
    cluster. Getting this reconstruction wrong silently inflates every metric
    below, so it is isolated here and tested directly.

    Args:
        n_articles: total articles that were offered to the clusterer.
        groups: 1-based article indices per group, as returned by the clusterer.

    Returns:
        A 0-indexed label list of length n_articles.
    """
    labels = [-1] * n_articles
    next_label = 0
    for group in groups:
        for idx in group:
            pos = idx - 1  # clusterer emits 1-based indices
            if 0 <= pos < n_articles:
                labels[pos] = next_label
        next_label += 1
    # Everything the clusterer did not place becomes its own singleton.
    for i in range(n_articles):
        if labels[i] == -1:
            labels[i] = next_label
            next_label += 1
    return labels


def partition_from_event_ids(event_ids: list[str | None]) -> list[int]:
    """Build ground truth from corpus labels. `None` means singleton."""
    labels: list[int] = []
    seen: dict[str, int] = {}
    next_label = 0
    for eid in event_ids:
        if eid is None:
            labels.append(next_label)
            next_label += 1
        else:
            if eid not in seen:
                seen[eid] = next_label
                next_label += 1
            labels.append(seen[eid])
    return labels


# ─── helpers ──────────────────────────────────────────────

def _comb2(n: int) -> int:
    """n choose 2."""
    return n * (n - 1) // 2


def _contingency(true: list, pred: list) -> dict[tuple, int]:
    table: dict[tuple, int] = defaultdict(int)
    for t, p in zip(true, pred, strict=True):
        table[(t, p)] += 1
    return table


def _check(true: list, pred: list) -> None:
    if len(true) != len(pred):
        raise ValueError(f"partition length mismatch: {len(true)} vs {len(pred)}")


# ─── pairwise precision / recall / F1 ─────────────────────

@dataclass(frozen=True)
class PairwiseScores:
    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int


def pairwise_scores(true: list, pred: list) -> PairwiseScores:
    """Precision/recall over all C(n,2) unordered article pairs.

    Product meaning:
      precision — when a story card says "how 4 outlets covered this", are
                  those 4 really one event? (false positives are visible errors)
      recall    — how many genuine cross-outlet stories did we fail to group?

    Convention: with no predicted pairs at all (an all-singletons prediction)
    precision is defined as 1.0. That is flattering on its own, which is why
    `evaluate` always reports n_clusters_pred alongside it.
    """
    _check(true, pred)
    tp = fp = fn = 0
    n = len(true)
    for i in range(n):
        for j in range(i + 1, n):
            same_true = true[i] == true[j]
            same_pred = pred[i] == pred[j]
            if same_pred and same_true:
                tp += 1
            elif same_pred and not same_true:
                fp += 1
            elif same_true and not same_pred:
                fn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return PairwiseScores(precision, recall, f1, tp, fp, fn)


# ─── adjusted Rand index ──────────────────────────────────

def adjusted_rand_index(true: list, pred: list) -> float:
    """Hubert & Arabie (1985) ARI.

        index     = sum_ij C(n_ij, 2)
        expected  = [sum_i C(a_i,2) * sum_j C(b_j,2)] / C(n,2)
        max_index = 0.5 * [sum_i C(a_i,2) + sum_j C(b_j,2)]
        ARI       = (index - expected) / (max_index - expected)

    Range is (-1, 1]: 1.0 is a perfect match, ~0.0 is chance-level agreement,
    negative is worse than chance.
    """
    _check(true, pred)
    n = len(true)
    if n < 2:
        return 1.0

    table = _contingency(true, pred)
    a = Counter(true)
    b = Counter(pred)

    index = sum(_comb2(v) for v in table.values())
    sum_a = sum(_comb2(v) for v in a.values())
    sum_b = sum(_comb2(v) for v in b.values())
    total_pairs = _comb2(n)

    expected = (sum_a * sum_b) / total_pairs
    max_index = 0.5 * (sum_a + sum_b)

    denom = max_index - expected
    if denom == 0:
        # Both partitions are degenerate in the same way (all-singletons, or a
        # single cluster). Identical -> 1.0, otherwise no agreement to measure.
        return 1.0 if list(true) == list(pred) or sum_a == sum_b else 0.0
    return (index - expected) / denom


# ─── V-measure (homogeneity / completeness) ───────────────

def _entropy(labels: list) -> float:
    n = len(labels)
    if n == 0:
        return 0.0
    return -sum((c / n) * math.log(c / n) for c in Counter(labels).values() if c > 0)


def _conditional_entropy(a: list, b: list) -> float:
    """H(A|B) — uncertainty about A given B."""
    n = len(a)
    if n == 0:
        return 0.0
    table = _contingency(a, b)
    counts_b = Counter(b)
    total = 0.0
    for (_, bv), n_ab in table.items():
        if n_ab > 0:
            total += (n_ab / n) * math.log(n_ab / counts_b[bv])
    return -total


@dataclass(frozen=True)
class VMeasureScores:
    homogeneity: float
    completeness: float
    v_measure: float


def v_measure(true: list, pred: list) -> VMeasureScores:
    """Rosenberg & Hirschberg (2007).

        homogeneity  = 1 - H(True|Pred)/H(True)   each predicted cluster holds
                                                  articles from one real event
        completeness = 1 - H(Pred|True)/H(Pred)   each real event lands in one
                                                  predicted cluster
        V            = harmonic mean of the two

    Reported separately on purpose: low homogeneity means over-merging
    (different events fused), low completeness means over-splitting (one event
    scattered). ARI collapses both into one number.
    """
    _check(true, pred)
    h_true = _entropy(true)
    h_pred = _entropy(pred)

    # Convention (matches sklearn): with no uncertainty to explain, the score
    # is perfect rather than undefined.
    homogeneity = 1.0 if h_true == 0 else 1.0 - _conditional_entropy(true, pred) / h_true
    completeness = 1.0 if h_pred == 0 else 1.0 - _conditional_entropy(pred, true) / h_pred

    denom = homogeneity + completeness
    v = 0.0 if denom == 0 else 2 * homogeneity * completeness / denom
    return VMeasureScores(homogeneity, completeness, v)


# ─── Sift-specific metrics ────────────────────────────────

def multi_outlet_partition(labels: list, outlets: list[str]) -> list:
    """Collapse every cluster with fewer than 2 distinct outlets to a singleton.

    Mirrors the `len(unique_outlets) >= 2` gate in
    workflows/story_workflow.py:196, which runs after clustering and decides
    what actually reaches the feed. Applying it to both partitions before
    scoring measures what users see rather than raw clusterer output — this is
    the metric that would have caught the "4x 9to5Mac posts rendered as *how 4
    outlets covered this*" bug in data instead of at render time.
    """
    if len(labels) != len(outlets):
        raise ValueError("labels and outlets must align")
    by_cluster: dict[object, set[str]] = defaultdict(set)
    for lab, outlet in zip(labels, outlets, strict=True):
        by_cluster[lab].add(outlet)

    out: list = []
    next_singleton = 0
    for lab in labels:
        if len(by_cluster[lab]) >= 2:
            out.append(("story", lab))
        else:
            out.append(("solo", next_singleton))
            next_singleton += 1
    return out


def topic_conflation_rate(
    true: list,
    pred: list,
    hard_pairs: list[tuple[int, int]],
) -> float:
    """Fraction of known same-topic/different-event pairs wrongly grouped.

    Directly measures the central claim of the clustering prompt in
    services/story_clusterer.py:48-49 — that "EU votes on AI Act" and "US
    issues AI executive order" are the same topic but must NOT be grouped.
    Generic metrics average this away; the distractor pairs are the whole point.

    `hard_pairs` are 0-based index pairs that ground truth says are apart.
    Lower is better. Returns 0.0 when there are no distractor pairs.
    """
    if not hard_pairs:
        return 0.0
    conflated = 0
    for i, j in hard_pairs:
        if true[i] == true[j]:
            raise ValueError(f"hard pair ({i},{j}) is grouped in ground truth")
        if pred[i] == pred[j]:
            conflated += 1
    return conflated / len(hard_pairs)


# ─── top-level report ─────────────────────────────────────

@dataclass(frozen=True)
class ClusteringReport:
    n_articles: int
    n_clusters_true: int
    n_clusters_pred: int
    singleton_rate_true: float
    singleton_rate_pred: float
    ari: float
    v_measure: float
    homogeneity: float
    completeness: float
    pairwise_precision: float
    pairwise_recall: float
    pairwise_f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    multi_outlet_precision: float | None = None
    multi_outlet_recall: float | None = None
    topic_conflation_rate: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _singleton_rate(labels: list) -> float:
    if not labels:
        return 0.0
    sizes = Counter(labels)
    return sum(1 for lab in sizes if sizes[lab] == 1) / len(sizes)


def evaluate(
    true: list,
    pred: list,
    *,
    outlets: list[str] | None = None,
    hard_pairs: list[tuple[int, int]] | None = None,
) -> ClusteringReport:
    """Compute the full metric set for one batch."""
    _check(true, pred)
    pw = pairwise_scores(true, pred)
    vm = v_measure(true, pred)

    mo_precision = mo_recall = None
    if outlets is not None:
        mo = pairwise_scores(
            multi_outlet_partition(true, outlets),
            multi_outlet_partition(pred, outlets),
        )
        mo_precision, mo_recall = mo.precision, mo.recall

    return ClusteringReport(
        n_articles=len(true),
        n_clusters_true=len(set(true)),
        n_clusters_pred=len(set(pred)),
        singleton_rate_true=_singleton_rate(true),
        singleton_rate_pred=_singleton_rate(pred),
        ari=adjusted_rand_index(true, pred),
        v_measure=vm.v_measure,
        homogeneity=vm.homogeneity,
        completeness=vm.completeness,
        pairwise_precision=pw.precision,
        pairwise_recall=pw.recall,
        pairwise_f1=pw.f1,
        true_positives=pw.true_positives,
        false_positives=pw.false_positives,
        false_negatives=pw.false_negatives,
        multi_outlet_precision=mo_precision,
        multi_outlet_recall=mo_recall,
        topic_conflation_rate=(
            topic_conflation_rate(true, pred, hard_pairs) if hard_pairs is not None else None
        ),
    )
