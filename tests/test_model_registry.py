"""The registry's contract, and the guards against it failing silently.

The failure this module must not have is a *silently ignored override*. If a
typo'd operation id, an unknown model, or a capability mismatch drops an
override without anyone noticing, the A/B still runs, still reports a number,
and the number describes the incumbent — you conclude a candidate is
indistinguishable from Haiku because it *was* Haiku. Every test here exists to
make that outcome loud instead.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from services import model_registry, usage_tracker
from services.model_registry import (
    INCUMBENT_BY_OPERATION,
    MODELS,
    OPERATIONS,
    assignments,
    non_default_assignments,
    resolve,
)

REPO_ROOT = pathlib.Path(__file__).parent.parent
SOURCE_DIRS = ("services", "workflows", "app")

# What each operation ran on before the registry existed. Hardcoded here rather
# than read from INCUMBENT_BY_OPERATION, so this is an independent record of the
# pre-refactor state and not a restatement of the thing under test.
PRE_REGISTRY_MODEL = {
    "summarizer.batch": "claude-haiku-4-5-20251001",
    "context_generator.batch": "claude-haiku-4-5-20251001",
    "primer_generator.batch": "claude-haiku-4-5-20251001",
    "entity_extractor.batch": "claude-haiku-4-5-20251001",
    "entity_linker_llm.link_text": "claude-haiku-4-5-20251001",
    "story_clusterer.cluster": "claude-haiku-4-5-20251001",
    "story_synthesizer.synthesize": "claude-haiku-4-5-20251001",
    "story_confirmer.confirm": "claude-haiku-4-5-20251001",
    "compare.search_sources": "claude-haiku-4-5-20251001",
    "compare.extract_and_compare": "claude-haiku-4-5-20251001",
    "judge.batch": "claude-sonnet-4-6",
}


@pytest.fixture(autouse=True)
def _clear_override_cache():
    """`_parse_overrides` is lru_cached on the raw string; tests vary it."""
    model_registry._parse_overrides.cache_clear()
    yield
    model_registry._parse_overrides.cache_clear()


def _logged_operations() -> set[str]:
    """Every literal first argument to log_usage / log_batch_usage in the app.

    AST rather than grep so a multi-line call is found too — two of them are.
    """
    found: set[str] = set()
    for directory in SOURCE_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in {"log_usage", "log_batch_usage"}:
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
                elif isinstance(first, ast.Name):
                    # `log_usage(OPERATION, ...)` — resolve the module constant.
                    for stmt in ast.walk(tree):
                        if (
                            isinstance(stmt, ast.Assign)
                            and any(
                                isinstance(t, ast.Name) and t.id == first.id
                                for t in stmt.targets
                            )
                            and isinstance(stmt.value, ast.Constant)
                        ):
                            found.add(stmt.value.value)
    return found


class TestOperationVocabulary:
    def test_every_logged_operation_is_in_the_registry(self):
        """A stage that logs spend under an id the registry does not know is a
        stage nobody can override — and an override naming it is silently
        dropped, so the experiment runs the incumbent and reports it as the
        candidate."""
        missing = _logged_operations() - set(OPERATIONS)
        assert not missing, (
            f"these operations are logged but not in INCUMBENT_BY_OPERATION: "
            f"{sorted(missing)}"
        )

    def test_the_registry_has_no_operations_that_nothing_logs(self):
        """The other direction: a registry entry with no call site is a stage
        you can 'override' with no effect at all."""
        extra = set(OPERATIONS) - _logged_operations()
        assert not extra, (
            f"these operations are in the registry but nothing logs them: "
            f"{sorted(extra)}"
        )

    def test_every_operation_names_a_model_that_exists(self):
        for operation, catalog_id in INCUMBENT_BY_OPERATION.items():
            assert catalog_id in MODELS, f"{operation} -> unknown model {catalog_id}"


class TestDefaultsReproducePriorBehaviour:
    def test_no_override_resolves_to_exactly_what_was_hardcoded(self):
        """The whole refactor is a no-op at the default, or it is a change
        nobody asked for."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(model_registry.settings, "llm_model_overrides", "")
            for operation, expected in PRE_REGISTRY_MODEL.items():
                assert resolve(operation).model == expected, operation

    def test_judge_is_the_only_non_haiku_stage(self):
        """An adjudicator has to be better than the thing it judges. If this
        ever fails because another stage moved to Sonnet, that is a 3x cost
        change and should be a deliberate one."""
        sonnet = {
            op for op in OPERATIONS if resolve(op).model.startswith("claude-sonnet")
        }
        assert sonnet == {"judge.batch"}

    def test_non_default_assignments_is_empty_by_default(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(model_registry.settings, "llm_model_overrides", "")
            assert non_default_assignments() == {}


class TestOverrides:
    def test_a_valid_override_takes_effect(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "summarizer.batch=sonnet-4-6",
            )
            assert resolve("summarizer.batch").model == "claude-sonnet-4-6"
            # and nothing else moved
            assert resolve("primer_generator.batch").model.startswith("claude-haiku")

    def test_several_overrides_parse(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                " summarizer.batch=sonnet-4-6 , judge.batch=haiku-4-5 ",
            )
            assert resolve("summarizer.batch").catalog_id == "sonnet-4-6"
            assert resolve("judge.batch").catalog_id == "haiku-4-5"

    @pytest.mark.parametrize(
        "raw",
        [
            "summarizer.bath=sonnet-4-6",  # typo'd operation
            "summarizer.batch=sonnet-4.6",  # typo'd model
            "summarizer.batch",  # no '='
            "=sonnet-4-6",  # no operation
            "garbage",
        ],
    )
    def test_a_bad_override_keeps_the_incumbent_and_logs_an_error(self, raw, caplog):
        """Never raise (a malformed env var must not take the service down at
        import) and never stay quiet (a dropped override is the failure this
        module exists to prevent)."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(model_registry.settings, "llm_model_overrides", raw)
            with caplog.at_level("ERROR", logger="sift-api.model_registry"):
                assert resolve("summarizer.batch").model == (
                    PRE_REGISTRY_MODEL["summarizer.batch"]
                )
        assert caplog.records, f"{raw!r} was dropped silently"

    def test_an_unknown_operation_falls_back_to_haiku_and_says_so(self, caplog):
        with caplog.at_level("ERROR", logger="sift-api.model_registry"):
            spec = resolve("stage.that.does.not.exist")
        assert spec.catalog_id == "haiku-4-5"
        assert caplog.records


class TestCapabilityRefusals:
    """A capability mismatch is not a cost difference, it is a broken stage.

    Both refusals guard against a swap that would look like it worked: the
    batch stages would silently lose their async completion path *and* the 50%
    discount, and compare.search_sources would lose the server-side web search
    it is built entirely around.
    """

    @pytest.mark.parametrize(
        "operation",
        [
            "context_generator.batch",
            "primer_generator.batch",
            "entity_extractor.batch",
            "compare.search_sources",
        ],
    )
    def test_operations_with_requirements_declare_them(self, operation):
        assert model_registry.CAPABILITIES[operation]

    def test_an_override_lacking_a_required_capability_is_refused(self, caplog):
        incapable = model_registry.ModelSpec(
            catalog_id="no-batch",
            provider="openai_compatible",
            model="some-open-weight-model",
            supports_batch=False,
            supports_prompt_cache=False,
            supports_server_web_search=False,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(MODELS, "no-batch", incapable)
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "context_generator.batch=no-batch,compare.search_sources=no-batch",
            )
            with caplog.at_level("ERROR", logger="sift-api.model_registry"):
                batch_spec = resolve("context_generator.batch")
                search_spec = resolve("compare.search_sources")

        assert batch_spec.catalog_id == "haiku-4-5"
        assert search_spec.catalog_id == "haiku-4-5"
        assert len(caplog.records) == 2

    def test_a_capable_model_is_not_refused(self):
        """The refusal must key on the capability, not on the provider — or no
        candidate is ever testable on those stages."""
        capable = model_registry.ModelSpec(
            catalog_id="has-batch",
            provider="openai_compatible",
            model="some-other-model",
            supports_batch=True,
            supports_prompt_cache=False,
            supports_server_web_search=False,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(MODELS, "has-batch", capable)
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "context_generator.batch=has-batch",
            )
            assert resolve("context_generator.batch").catalog_id == "has-batch"


class TestPricingAgreement:
    def test_every_registry_model_has_a_price_row(self):
        """A model the registry can select but usage_tracker cannot price would
        be billed at the default model's rates — the exact bug this program
        started by fixing, reintroduced one layer up."""
        unpriced = sorted(
            spec.model for spec in MODELS.values() if spec.model not in usage_tracker.PRICES
        )
        assert not unpriced, f"selectable but unpriced: {unpriced}"


class TestAssignmentsAreObservable:
    def test_assignments_covers_every_operation(self):
        """Echoed on /health so 'which model is this stage on' is answerable
        from outside the process rather than inferred from a merge."""
        assert set(assignments()) == set(OPERATIONS)

    def test_assignments_reports_wire_ids_not_catalog_aliases(self):
        """The wire id is what lands in ai_usage_daily, so it is the one that
        lets you join a health check against the ledger."""
        assert assignments()["judge.batch"] == "claude-sonnet-4-6"

    def test_non_default_assignments_names_only_what_moved(self):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                model_registry.settings,
                "llm_model_overrides",
                "summarizer.batch=sonnet-4-6",
            )
            assert non_default_assignments() == {"summarizer.batch": "sonnet-4-6"}


class TestNoModuleLevelFreeze:
    """A `MODEL = resolve(...)` at import time would freeze the value for the
    life of the process: an override would need a restart to take effect, and
    the constant would lie in any test that varies the setting. The four stages
    that take a `model=` kwarg had exactly this shape as a default argument.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "services.summarizer",
            "services.context_generator",
            "services.primer_generator",
            "services.entity_extractor",
            "services.entity_linker_llm",
            "services.story_clusterer",
            "services.story_synthesizer",
            "services.story_confirmer",
            "services.judge",
            "services.batch_client",
            "workflows.compare_workflow",
        ],
    )
    def test_no_hardcoded_claude_model_constant_survives(self, module):
        path = REPO_ROOT / (module.replace(".", "/") + ".py")
        tree = ast.parse(path.read_text())
        offenders = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value.startswith("claude-")
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert not offenders, (
            f"{module} still hardcodes a model id ({offenders}) — it should call "
            f"model_registry.resolve(OPERATION) in the function body"
        )

    @pytest.mark.parametrize(
        ("module", "function"),
        [
            ("services.story_clusterer", "cluster_articles"),
            ("services.story_synthesizer", "synthesize_story"),
            ("services.story_confirmer", "confirm"),
            ("services.judge", "judge_lines"),
        ],
    )
    def test_the_model_kwarg_defaults_to_none_not_a_frozen_string(
        self, module, function
    ):
        path = REPO_ROOT / (module.replace(".", "/") + ".py")
        tree = ast.parse(path.read_text())
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == function
        )
        names = [a.arg for a in fn.args.kwonlyargs]
        default = fn.args.kw_defaults[names.index("model")]
        assert isinstance(default, ast.Constant) and default.value is None, (
            f"{module}.{function}'s model default is evaluated at import — "
            f"use None and resolve in the body"
        )
