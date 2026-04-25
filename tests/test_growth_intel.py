"""
Phase 10.4 tests — Growth Intelligence Layer.

Covers:
  * loaders return ``[]`` on missing / malformed / unreadable logs;
  * conversion + share funnels aggregate correctly;
  * grouping by ``reason``, ``endpoint``, and ``src`` produces the
    documented per-bucket shape;
  * cross-funnel attribution joins on ``api_key_hash``;
  * headlines (top trigger, top insight, best source, overall rate)
    apply the min-impressions / min-inbound floors;
  * raw API keys never appear in the summary surface (leak guard);
  * CLI ``--summary`` / ``--json`` paths render successfully.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from trading_bot.api import growth_intel as gi
from trading_bot.api.growth_intel import (
    DEFAULT_MIN_IMPRESSIONS,
    DEFAULT_MIN_INBOUND,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    REC_AMPLIFY_TOP_TRIGGER,
    REC_DOUBLE_DOWN_SOURCE,
    REC_FEATURE_TOP_INSIGHT,
    REC_INSUFFICIENT_DATA,
    REC_TIGHTEN_FREE_LIMIT,
    VALID_PRIORITIES,
    VALID_RECOMMENDATION_IDS,
    format_recommendations_text,
    format_summary_text,
    generate_recommendations,
    load_share_events,
    load_upgrade_events,
    summarize,
)
from trading_bot.api.share_events import (
    EVENT_INBOUND_VISIT,
    EVENT_SHARE_GENERATED,
    SHARE_EVENTS_LOG_ENV_VAR,
)
from trading_bot.api.upgrade_events import (
    EVENT_UPGRADE_CLICKED,
    EVENT_UPGRADE_COMPLETED,
    EVENT_UPGRADE_SHOWN,
    UPGRADE_EVENTS_LOG_ENV_VAR,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


@pytest.fixture()
def upgrade_log(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "upgrade.jsonl"
    monkeypatch.setenv(UPGRADE_EVENTS_LOG_ENV_VAR, str(p))
    return p


@pytest.fixture()
def share_log(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "share.jsonl"
    monkeypatch.setenv(SHARE_EVENTS_LOG_ENV_VAR, str(p))
    return p


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _funnel_row(
    *,
    event: str,
    api_key_hash: str,
    reason: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict:
    return {
        "timestamp": "2026-04-25T12:00:00.000000Z",
        "api_key_hash": api_key_hash,
        "event": event,
        "tier": "free",
        "request_id": None,
        "reason": reason,
        "endpoint": endpoint,
    }


def _share_row(
    *,
    event: str,
    api_key_hash: str,
    src: Optional[str] = None,
    endpoint: str = "/reports/latest",
) -> dict:
    return {
        "timestamp": "2026-04-25T12:00:00.000000Z",
        "api_key_hash": api_key_hash,
        "event": event,
        "endpoint": endpoint,
        "src": src,
        "request_id": None,
    }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_missing_files_return_empty(self, tmp_path: Path):
        missing = tmp_path / "absent.jsonl"
        assert load_upgrade_events(missing) == []
        assert load_share_events(missing) == []

    def test_blank_lines_and_malformed_rows_are_skipped(
        self, upgrade_log: Path,
    ):
        upgrade_log.write_text(
            "\n".join([
                "",
                "garbage",
                "[\"not a dict\"]",
                json.dumps(_funnel_row(
                    event=EVENT_UPGRADE_SHOWN,
                    api_key_hash=_hash("u1"),
                    reason="usage_limit",
                    endpoint="/reports/latest",
                )),
                "{ broken json",
                "",
            ]),
            encoding="utf-8",
        )
        rows = load_upgrade_events(upgrade_log)
        assert len(rows) == 1
        assert rows[0]["event"] == EVENT_UPGRADE_SHOWN

    def test_default_path_resolves_via_env(
        self, monkeypatch, tmp_path: Path,
    ):
        target = tmp_path / "alt.jsonl"
        _write_jsonl(target, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash("u1"), reason="x", endpoint="/y",
            ),
        ])
        monkeypatch.setenv(UPGRADE_EVENTS_LOG_ENV_VAR, str(target))
        # No explicit path → resolves through the env var.
        assert len(load_upgrade_events()) == 1


# ---------------------------------------------------------------------------
# summarize on an empty pair of logs
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_logs_produce_zero_funnel(
        self, upgrade_log: Path, share_log: Path,
    ):
        # Files don't exist yet — every loader returns [].
        s = summarize()
        assert s["totals"] == {"upgrade_rows": 0, "share_rows": 0}
        assert s["conversion_funnel"]["shown"] == 0
        assert s["conversion_funnel"]["shown_to_completed"] == 0.0
        assert s["share_funnel"]["generated"] == 0
        assert s["share_funnel"]["inbound_per_share"] == 0.0
        assert s["by_reason"] == {}
        assert s["by_insight"] == {}
        assert s["by_src"] == {}
        assert s["attribution_by_src"] == {}
        # Headlines all gracefully degrade.
        h = s["headlines"]
        assert h["top_converting_trigger"] is None
        assert h["top_performing_insight"] is None
        assert h["best_source"] is None
        assert h["overall_conversion_rate"] == 0.0

    def test_empty_format_summary_text_renders(
        self, upgrade_log: Path, share_log: Path,
    ):
        out = format_summary_text(summarize())
        assert "GROWTH INTELLIGENCE SUMMARY" in out
        assert "(insufficient data)" in out


# ---------------------------------------------------------------------------
# Funnel aggregation
# ---------------------------------------------------------------------------


class TestConversionFunnel:
    def test_overall_counts(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = []
        for i in range(10):
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ))
        for i in range(4):
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_CLICKED,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ))
        for i in range(2):
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ))
        _write_jsonl(upgrade_log, rows)

        s = summarize()
        f = s["conversion_funnel"]
        assert f["shown"] == 10
        assert f["clicked"] == 4
        assert f["completed"] == 2
        assert f["shown_to_clicked"] == 0.4
        assert f["clicked_to_completed"] == 0.5
        assert f["shown_to_completed"] == 0.2
        assert s["headlines"]["overall_conversion_rate"] == 0.2


class TestByReason:
    def test_groups_by_reason_and_drops_missing(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = [
            # usage_limit: 5 shown, 2 completed → 40%.
            *(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ) for i in range(5)),
            *(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ) for i in range(2)),
            # feature_locked: 8 shown, 1 completed → 12.5%.
            *(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"v{i}"),
                reason="feature_locked",
                endpoint="/reports/history",
            ) for i in range(8)),
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash("v0"),
                reason="feature_locked",
                endpoint="/reports/history",
            ),
            # Row with missing reason — must be skipped from
            # by_reason but still counted in the overall funnel.
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash("orphan"),
                reason=None,
                endpoint="/reports/latest",
            ),
        ]
        _write_jsonl(upgrade_log, rows)

        s = summarize()
        assert set(s["by_reason"].keys()) == {
            "usage_limit", "feature_locked",
        }
        assert s["by_reason"]["usage_limit"]["shown"] == 5
        assert s["by_reason"]["usage_limit"]["completed"] == 2
        assert s["by_reason"]["usage_limit"]["shown_to_completed"] == 0.4
        assert s["by_reason"]["feature_locked"]["shown_to_completed"] == 0.125

    def test_top_converting_trigger_respects_min_impressions(
        self, upgrade_log: Path, share_log: Path,
    ):
        # tiny: 1/1 = 100 % but only 1 impression.
        # bulk: 4/10 = 40 % with 10 impressions.
        rows = [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash("tiny"),
                reason="experimental",
                endpoint="/x",
            ),
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash("tiny"),
                reason="experimental",
                endpoint="/x",
            ),
        ]
        for i in range(10):
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"b{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ))
        for i in range(4):
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash(f"b{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ))
        _write_jsonl(upgrade_log, rows)

        # Default min_impressions = 5 → tiny is excluded.
        s = summarize()
        top = s["headlines"]["top_converting_trigger"]
        assert top is not None
        assert top["value"] == "usage_limit"
        assert top["shown"] == 10
        assert top["completed"] == 4

    def test_top_converting_trigger_none_when_below_floor(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            ) for i in range(2)
        ]
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_COMPLETED,
            api_key_hash=_hash("u0"),
            reason="usage_limit",
            endpoint="/reports/latest",
        ))
        _write_jsonl(upgrade_log, rows)

        s = summarize(min_impressions=DEFAULT_MIN_IMPRESSIONS)
        assert s["headlines"]["top_converting_trigger"] is None


class TestByInsight:
    def test_groups_by_endpoint_as_insight(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = [
            *(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"u{i}"),
                reason="limited_access",
                endpoint="/reports/latest",
            ) for i in range(6)),
            *(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash(f"u{i}"),
                reason="limited_access",
                endpoint="/reports/latest",
            ) for i in range(3)),
            *(_funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"v{i}"),
                reason="feature_locked",
                endpoint="/experiments/recent",
            ) for i in range(6)),
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=_hash("v0"),
                reason="feature_locked",
                endpoint="/experiments/recent",
            ),
        ]
        _write_jsonl(upgrade_log, rows)

        s = summarize()
        assert set(s["by_insight"].keys()) == {
            "/reports/latest", "/experiments/recent",
        }
        assert s["by_insight"]["/reports/latest"]["shown_to_completed"] == 0.5
        top = s["headlines"]["top_performing_insight"]
        assert top is not None
        assert top["value"] == "/reports/latest"


# ---------------------------------------------------------------------------
# Share funnel + src grouping + attribution
# ---------------------------------------------------------------------------


class TestShareFunnel:
    def test_overall_counts(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = [
            *(_share_row(
                event=EVENT_SHARE_GENERATED,
                api_key_hash=_hash(f"u{i}"),
                src=None,
            ) for i in range(10)),
            *(_share_row(
                event=EVENT_INBOUND_VISIT,
                api_key_hash=_hash(f"v{i}"),
                src="twitter",
            ) for i in range(3)),
        ]
        _write_jsonl(share_log, rows)
        s = summarize()
        assert s["share_funnel"]["generated"] == 10
        assert s["share_funnel"]["inbound"] == 3
        assert s["share_funnel"]["inbound_per_share"] == 0.3

    def test_by_src_groups_inbound_and_collapses_missing_src(
        self, upgrade_log: Path, share_log: Path,
    ):
        rows = [
            _share_row(
                event=EVENT_SHARE_GENERATED,
                api_key_hash=_hash("u1"),
                src=None,  # collapses to "(none)"
            ),
            _share_row(
                event=EVENT_INBOUND_VISIT,
                api_key_hash=_hash("v1"),
                src="twitter",
            ),
            _share_row(
                event=EVENT_INBOUND_VISIT,
                api_key_hash=_hash("v2"),
                src="twitter",
            ),
            _share_row(
                event=EVENT_INBOUND_VISIT,
                api_key_hash=_hash("v3"),
                src="hn",
            ),
        ]
        _write_jsonl(share_log, rows)
        s = summarize()
        assert s["by_src"]["twitter"]["inbound"] == 2
        assert s["by_src"]["hn"]["inbound"] == 1
        assert s["by_src"]["(none)"]["generated"] == 1


class TestAttributionAndBestSource:
    def test_inbound_users_joined_to_conversion_via_key_hash(
        self, upgrade_log: Path, share_log: Path,
    ):
        # 4 users arrived via twitter; 3 of them later showed
        # the upgrade prompt; 2 completed.
        twitter_users = [_hash(f"twitter_{i}") for i in range(4)]
        # 2 users via hn; 1 shown; 0 completed.
        hn_users = [_hash(f"hn_{i}") for i in range(2)]

        share_rows = [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="twitter",
            )
            for h in twitter_users
        ] + [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="hn",
            )
            for h in hn_users
        ]
        _write_jsonl(share_log, share_rows)

        upgrade_rows = []
        for h in twitter_users[:3]:
            upgrade_rows.append(_funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=h,
                reason="limited_access", endpoint="/reports/latest",
            ))
        for h in twitter_users[:2]:
            upgrade_rows.append(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED, api_key_hash=h,
                reason="limited_access", endpoint="/reports/latest",
            ))
        upgrade_rows.append(_funnel_row(
            event=EVENT_UPGRADE_SHOWN, api_key_hash=hn_users[0],
            reason="limited_access", endpoint="/reports/latest",
        ))
        _write_jsonl(upgrade_log, upgrade_rows)

        s = summarize(min_inbound=2)
        twitter = s["attribution_by_src"]["twitter"]
        assert twitter["inbound_users"] == 4
        assert twitter["shown_users"] == 3
        assert twitter["completed_users"] == 2
        assert twitter["completion_rate"] == 0.5
        hn = s["attribution_by_src"]["hn"]
        assert hn["inbound_users"] == 2
        assert hn["completed_users"] == 0

        best = s["headlines"]["best_source"]
        assert best is not None
        assert best["src"] == "twitter"

    def test_best_source_respects_min_inbound(
        self, upgrade_log: Path, share_log: Path,
    ):
        # Only 1 inbound visitor, but they completed → 100 %.
        h = _hash("solo")
        _write_jsonl(share_log, [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="single",
            ),
        ])
        _write_jsonl(upgrade_log, [
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED,
                api_key_hash=h,
                reason="limited_access",
                endpoint="/reports/latest",
            ),
        ])
        s = summarize(min_inbound=DEFAULT_MIN_INBOUND)
        # Floor (3) excludes the single-visitor src.
        assert s["headlines"]["best_source"] is None


# ---------------------------------------------------------------------------
# Privacy: raw API keys never appear in the summary surface.
# ---------------------------------------------------------------------------


class TestNoRawKeyLeak:
    def test_summary_serialisation_contains_no_raw_marker(
        self, upgrade_log: Path, share_log: Path,
    ):
        """Plant a unique marker as if it were the raw key, hash it,
        and assert only the hash makes it into the summary surface."""
        marker = "RAW_API_KEY_PHASE_10_4_DO_NOT_LEAK"
        h = _hash(marker)
        _write_jsonl(upgrade_log, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=h,
                reason="usage_limit", endpoint="/reports/latest",
            ),
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED, api_key_hash=h,
                reason="usage_limit", endpoint="/reports/latest",
            ),
        ])
        _write_jsonl(share_log, [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="hn",
            ),
        ])
        s = summarize()
        rendered = json.dumps(s, default=str)
        assert marker not in rendered
        text = format_summary_text(s)
        assert marker not in text

    def test_summary_does_not_surface_individual_hashes(
        self, upgrade_log: Path, share_log: Path,
    ):
        """The summary only exposes aggregated dimensions
        (reason / endpoint / src). Individual ``api_key_hash``
        values stay at the per-user level inside the loaders;
        none of them reach the summary surface."""
        h = _hash("user_a")
        _write_jsonl(upgrade_log, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=h,
                reason="usage_limit", endpoint="/reports/latest",
            ),
        ])
        _write_jsonl(share_log, [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="hn",
            ),
        ])
        s = summarize()
        rendered = json.dumps(s, default=str)
        # No api_key_hash key in the summary structure.
        assert '"api_key_hash"' not in rendered
        # The hash value itself doesn't appear either.
        assert h not in rendered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_no_args_prints_help_returns_two(self, capsys):
        rc = gi.main([])
        assert rc == 2
        out = capsys.readouterr().out
        assert "growth_intel" in out

    def test_cli_summary_text(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        _write_jsonl(upgrade_log, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=_hash("u1"),
                reason="usage_limit", endpoint="/reports/latest",
            ),
        ])
        rc = gi.main(["--summary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "GROWTH INTELLIGENCE SUMMARY" in out
        assert "Conversion funnel:" in out
        assert "Share funnel:" in out

    def test_cli_summary_json(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        rc = gi.main(["--summary", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "conversion_funnel" in parsed
        assert "share_funnel" in parsed
        assert "headlines" in parsed

    def test_cli_explicit_paths(
        self, tmp_path: Path, capsys,
    ):
        upath = tmp_path / "u.jsonl"
        spath = tmp_path / "s.jsonl"
        _write_jsonl(upath, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=_hash("u1"),
                reason="usage_limit", endpoint="/reports/latest",
            ),
        ])
        rc = gi.main([
            "--summary",
            "--upgrade-path", str(upath),
            "--share-path", str(spath),
            "--json",
        ])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["totals"]["upgrade_rows"] == 1
        assert parsed["totals"]["share_rows"] == 0


# ---------------------------------------------------------------------------
# Boundary: the module reads only — no file is written by summarize.
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_summarize_does_not_create_files(
        self, tmp_path: Path,
    ):
        upath = tmp_path / "u.jsonl"
        spath = tmp_path / "s.jsonl"
        # No files exist.
        s = summarize(upgrade_path=upath, share_path=spath)
        assert s["totals"] == {"upgrade_rows": 0, "share_rows": 0}
        # Loaders don't materialise the inputs.
        assert not upath.exists()
        assert not spath.exists()

    def test_module_does_not_import_core(self):
        src = (
            Path(__file__).resolve().parent.parent
            / "trading_bot" / "api" / "growth_intel.py"
        ).read_text()
        for forbidden in (
            "from trading_bot.core.alpha",
            "from trading_bot.execution",
            "from trading_bot.portfolio",
            "from trading_bot.risk",
            "from trading_bot.scanners",
            "from trading_bot.strategies",
            "from trading_bot.main",
        ):
            assert forbidden not in src, (
                f"growth_intel.py reaches into Core: {forbidden!r}"
            )


# ===========================================================================
# Phase 10.5 — Optimization loop: recommendations
# ===========================================================================


def _seed_strong_funnel(
    upgrade_log: Path, share_log: Path, *,
    usage_lift: bool = True,
    top_endpoint: str = "/reports/latest",
    best_src: str = "twitter",
) -> None:
    """
    Seed a synthetic dataset that reliably triggers the four
    primary recommendation rules.

      * usage_limit converts at ~50 % (10/20 if lift, else 4/20)
      * limited_access converts at ~10 % (2/20)
      * top endpoint matches ``top_endpoint``
      * best source has 5 inbound + 3 conversions
    """
    completed_for_usage = 10 if usage_lift else 4

    rows = []
    for i in range(20):
        u = _hash(f"usage_{i}")
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_SHOWN, api_key_hash=u,
            reason="usage_limit", endpoint=top_endpoint,
        ))
    for i in range(completed_for_usage):
        u = _hash(f"usage_{i}")
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_COMPLETED, api_key_hash=u,
            reason="usage_limit", endpoint=top_endpoint,
        ))
    for i in range(20):
        u = _hash(f"limited_{i}")
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_SHOWN, api_key_hash=u,
            reason="limited_access", endpoint=top_endpoint,
        ))
    for i in range(2):
        u = _hash(f"limited_{i}")
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_COMPLETED, api_key_hash=u,
            reason="limited_access", endpoint=top_endpoint,
        ))
    # A second, lower-converting endpoint so the headline isn't
    # the only one in the by_insight grouping.
    for i in range(8):
        u = _hash(f"hist_{i}")
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_SHOWN, api_key_hash=u,
            reason="feature_locked", endpoint="/reports/history",
        ))

    # Inbound visitors to attribute conversions back to.
    src_users = [_hash(f"{best_src}_v{i}") for i in range(5)]
    share_rows = [
        _share_row(
            event=EVENT_INBOUND_VISIT, api_key_hash=u, src=best_src,
        )
        for u in src_users
    ]
    # 3 of the inbound users complete the funnel.
    for u in src_users[:3]:
        rows.append(_funnel_row(
            event=EVENT_UPGRADE_COMPLETED, api_key_hash=u,
            reason="usage_limit", endpoint=top_endpoint,
        ))

    _write_jsonl(upgrade_log, rows)
    _write_jsonl(share_log, share_rows)


class TestRecommendationsBasics:
    def test_id_and_priority_constants_pinned(self):
        """Public constants are stable identifiers operators / dashboards
        can pin against; renaming any of them is a breaking change."""
        assert REC_AMPLIFY_TOP_TRIGGER == "amplify_top_trigger"
        assert REC_FEATURE_TOP_INSIGHT == "feature_top_insight"
        assert REC_DOUBLE_DOWN_SOURCE == "double_down_best_source"
        assert REC_TIGHTEN_FREE_LIMIT == "tighten_free_limit"
        assert REC_INSUFFICIENT_DATA == "insufficient_data"
        assert VALID_RECOMMENDATION_IDS == {
            REC_AMPLIFY_TOP_TRIGGER,
            REC_FEATURE_TOP_INSIGHT,
            REC_DOUBLE_DOWN_SOURCE,
            REC_TIGHTEN_FREE_LIMIT,
            REC_INSUFFICIENT_DATA,
        }
        assert VALID_PRIORITIES == {
            PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW,
        }

    def test_each_recommendation_has_documented_schema(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        recs = generate_recommendations(summarize())
        assert recs, "expected at least one recommendation"
        for rec in recs:
            assert set(rec.keys()) == {
                "id", "priority", "title", "rationale", "action",
            }
            assert rec["id"] in VALID_RECOMMENDATION_IDS
            assert rec["priority"] in VALID_PRIORITIES
            for field in ("title", "rationale", "action"):
                assert isinstance(rec[field], str)
                assert rec[field].strip() != ""

    def test_deterministic_repeated_calls(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        s = summarize()
        first = generate_recommendations(s)
        second = generate_recommendations(s)
        assert first == second


class TestRecommendationRules:
    def test_strong_funnel_emits_all_four_rules(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log, usage_lift=True)
        ids = {r["id"] for r in generate_recommendations(summarize())}
        assert REC_AMPLIFY_TOP_TRIGGER in ids
        assert REC_FEATURE_TOP_INSIGHT in ids
        assert REC_DOUBLE_DOWN_SOURCE in ids
        assert REC_TIGHTEN_FREE_LIMIT in ids
        # The "insufficient data" fallback must NOT appear when other
        # rules fired.
        assert REC_INSUFFICIENT_DATA not in ids

    def test_amplify_trigger_cites_top_reason(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rec = next(
            r for r in generate_recommendations(summarize())
            if r["id"] == REC_AMPLIFY_TOP_TRIGGER
        )
        assert "usage_limit" in rec["title"]
        assert "usage_limit" in rec["rationale"]
        # High priority because rate >= 25%.
        assert rec["priority"] == PRIORITY_HIGH

    def test_top_insight_cites_endpoint(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(
            upgrade_log, share_log, top_endpoint="/reports/latest",
        )
        rec = next(
            r for r in generate_recommendations(summarize())
            if r["id"] == REC_FEATURE_TOP_INSIGHT
        )
        assert "/reports/latest" in rec["title"]
        assert "/reports/latest" in rec["rationale"]

    def test_best_source_cites_src(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(
            upgrade_log, share_log, best_src="hn-launch",
        )
        rec = next(
            r for r in generate_recommendations(summarize())
            if r["id"] == REC_DOUBLE_DOWN_SOURCE
        )
        assert "hn-launch" in rec["title"]
        assert "hn-launch" in rec["rationale"]
        assert "?src=hn-launch" in rec["action"]

    def test_tighten_free_limit_only_when_usage_lift_present(
        self, upgrade_log: Path, share_log: Path,
    ):
        # usage_limit at the same baseline rate as the overall funnel
        # → no tightening recommendation (the rule needs a lift).
        rows = []
        for i in range(20):
            u = _hash(f"u{i}")
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=u,
                reason="usage_limit", endpoint="/reports/latest",
            ))
        for i in range(4):  # 20 % rate
            u = _hash(f"u{i}")
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED, api_key_hash=u,
                reason="usage_limit", endpoint="/reports/latest",
            ))
        # Every other reason converts at the same 20 %, so there's
        # no lift on usage_limit specifically.
        for i in range(20):
            u = _hash(f"v{i}")
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=u,
                reason="feature_locked", endpoint="/reports/latest",
            ))
        for i in range(4):
            u = _hash(f"v{i}")
            rows.append(_funnel_row(
                event=EVENT_UPGRADE_COMPLETED, api_key_hash=u,
                reason="feature_locked", endpoint="/reports/latest",
            ))
        _write_jsonl(upgrade_log, rows)

        ids = {r["id"] for r in generate_recommendations(summarize())}
        assert REC_TIGHTEN_FREE_LIMIT not in ids
        # Other rules still fire on the same data.
        assert REC_AMPLIFY_TOP_TRIGGER in ids

    def test_tighten_free_limit_skipped_when_overall_zero(
        self, upgrade_log: Path, share_log: Path,
    ):
        """No completions anywhere → no baseline → no recommendation."""
        rows = [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN,
                api_key_hash=_hash(f"u{i}"),
                reason="usage_limit",
                endpoint="/reports/latest",
            )
            for i in range(10)
        ]
        _write_jsonl(upgrade_log, rows)
        ids = {r["id"] for r in generate_recommendations(summarize())}
        assert REC_TIGHTEN_FREE_LIMIT not in ids


class TestEmptyLogs:
    def test_empty_logs_emit_insufficient_data(
        self, upgrade_log: Path, share_log: Path,
    ):
        recs = generate_recommendations(summarize())
        assert len(recs) == 1
        assert recs[0]["id"] == REC_INSUFFICIENT_DATA
        assert recs[0]["priority"] == PRIORITY_LOW

    def test_non_dict_input_does_not_raise(self):
        # Defensive: pure function should never raise on bad input.
        recs = generate_recommendations(None)  # type: ignore[arg-type]
        assert len(recs) == 1
        assert recs[0]["id"] == REC_INSUFFICIENT_DATA


class TestRecommendationOrdering:
    def test_high_priority_recommendations_come_first(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        recs = generate_recommendations(summarize())
        priorities = [r["priority"] for r in recs]
        # HIGH must come before MEDIUM must come before LOW. We
        # assert via an index check rather than equality so the
        # specific mix of priorities can change without breaking
        # the contract.
        rank = {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2}
        ranks = [rank[p] for p in priorities]
        assert ranks == sorted(ranks)


class TestRecommendationsLeakGuard:
    def test_no_raw_api_key_in_recommendations(
        self, upgrade_log: Path, share_log: Path,
    ):
        marker = "RAW_API_KEY_PHASE_10_5_DO_NOT_LEAK"
        h = _hash(marker)
        _write_jsonl(upgrade_log, [
            _funnel_row(
                event=EVENT_UPGRADE_SHOWN, api_key_hash=h,
                reason="usage_limit", endpoint="/reports/latest",
            ),
            _funnel_row(
                event=EVENT_UPGRADE_COMPLETED, api_key_hash=h,
                reason="usage_limit", endpoint="/reports/latest",
            ),
        ])
        _write_jsonl(share_log, [
            _share_row(
                event=EVENT_INBOUND_VISIT, api_key_hash=h, src="hn",
            ),
        ])
        recs = generate_recommendations(summarize())
        rendered = json.dumps(recs, default=str)
        assert marker not in rendered
        text = format_recommendations_text(recs)
        assert marker not in text
        # Individual hashes also stay out of the recommendation text.
        assert h not in rendered
        assert h not in text


class TestRecommendationsRendering:
    def test_format_recommendations_numbers_them(
        self, upgrade_log: Path, share_log: Path,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        recs = generate_recommendations(summarize())
        out = format_recommendations_text(recs)
        for idx in range(1, len(recs) + 1):
            assert f"{idx}. [" in out
        assert "GROWTH OPTIMIZATION RECOMMENDATIONS" in out

    def test_format_handles_empty_input(self):
        out = format_recommendations_text([])
        assert "(no recommendations)" in out


class TestRecommendCli:
    def test_cli_recommend_text(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = gi.main(["--recommend"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "GROWTH OPTIMIZATION RECOMMENDATIONS" in out
        # At least one numbered item was rendered.
        assert "1. [" in out

    def test_cli_recommend_json_emits_bare_list(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        """``--recommend --json`` alone emits a bare JSON array of
        recommendations (the Phase 10.5 surface). Combine with
        ``--summary`` to get the documented envelope shape."""
        _seed_strong_funnel(upgrade_log, share_log)
        rc = gi.main(["--recommend", "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert isinstance(parsed, list)
        assert parsed
        for rec in parsed:
            assert set(rec.keys()) == {
                "id", "priority", "title", "rationale", "action",
            }

    def test_cli_summary_and_recommend_combined(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = gi.main(["--summary", "--recommend"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "GROWTH INTELLIGENCE SUMMARY" in out
        assert "GROWTH OPTIMIZATION RECOMMENDATIONS" in out

    def test_cli_summary_and_recommend_combined_json(
        self, upgrade_log: Path, share_log: Path, capsys,
    ):
        _seed_strong_funnel(upgrade_log, share_log)
        rc = gi.main(["--summary", "--recommend", "--json"])
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert "summary" in parsed
        assert "recommendations" in parsed

    def test_cli_recommend_on_empty_logs_returns_zero(
        self, tmp_path: Path, capsys,
    ):
        rc = gi.main([
            "--recommend",
            "--upgrade-path", str(tmp_path / "absent.jsonl"),
            "--share-path", str(tmp_path / "absent.jsonl"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "insufficient_data" in out
