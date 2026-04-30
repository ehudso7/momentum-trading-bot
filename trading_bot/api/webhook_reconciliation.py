"""
Phase 11.1 - Webhook Replay Protection and Event Reconciliation

Provides tools for:
1. Detecting replay attacks
2. Event reconciliation after outages
3. Automatic recovery from missed events
4. Audit trail for compliance
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


class WebhookReplayProtection:
    """
    Protects against webhook replay attacks using:
    1. Timestamp validation (events must be recent)
    2. Signature verification
    3. Nonce tracking
    4. Rate limiting per event type
    """

    def __init__(
        self,
        max_age_seconds: int = 300,
        events_log_path: Optional[Path] = None,
    ):
        self.max_age_seconds = max_age_seconds
        self.events_log_path = events_log_path or Path("data/webhook_events.jsonl")
        self._nonce_cache: set[str] = set()
        self._rate_limits: dict[str, list[float]] = {}
        self._load_recent_nonces()

    def _load_recent_nonces(self) -> None:
        """Load nonces from recent events to prevent replay after restart."""
        if not self.events_log_path.exists():
            return

        cutoff = time.time() - self.max_age_seconds
        try:
            with self.events_log_path.open() as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        timestamp = event.get("timestamp", 0)
                        if timestamp > cutoff:
                            nonce = event.get("nonce")
                            if nonce:
                                self._nonce_cache.add(nonce)
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            log.warning("replay_protection.load_nonces_failed", error=str(e))

    def validate_event(
        self,
        event_id: str,
        timestamp: int,
        signature: str,
        body: bytes,
        secret: str,
    ) -> tuple[bool, Optional[str]]:
        """
        Validate a webhook event for replay protection.

        Returns:
            (is_valid, error_reason)
        """
        # Check timestamp freshness
        current_time = int(time.time())
        if abs(current_time - timestamp) > self.max_age_seconds:
            return False, "timestamp_too_old_or_future"

        # Verify signature
        expected_sig = self._compute_signature(timestamp, body, secret)
        if not hmac.compare_digest(signature, expected_sig):
            return False, "invalid_signature"

        # Check for replay using nonce (event_id + timestamp)
        nonce = f"{event_id}:{timestamp}"
        if nonce in self._nonce_cache:
            return False, "duplicate_nonce_replay_detected"

        self._nonce_cache.add(nonce)

        # Prune old nonces periodically
        if len(self._nonce_cache) > 10000:
            self._prune_old_nonces()

        return True, None

    def _compute_signature(
        self,
        timestamp: int,
        body: bytes,
        secret: str,
    ) -> str:
        """Compute expected signature for verification."""
        signed_payload = f"{timestamp}.{body.decode('utf-8')}"
        return hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _prune_old_nonces(self) -> None:
        """Remove old nonces to prevent unbounded memory growth."""
        # In production, this would query the persistent store
        # For now, just clear oldest half
        if len(self._nonce_cache) > 5000:
            self._nonce_cache = set(list(self._nonce_cache)[-5000:])

    def check_rate_limit(
        self,
        event_type: str,
        max_per_minute: int = 100,
    ) -> bool:
        """
        Check if event type is within rate limits.

        Returns True if within limits, False if exceeded.
        """
        current_time = time.time()
        minute_ago = current_time - 60

        # Get or create rate limit bucket
        if event_type not in self._rate_limits:
            self._rate_limits[event_type] = []

        bucket = self._rate_limits[event_type]

        # Remove old entries
        bucket[:] = [t for t in bucket if t > minute_ago]

        # Check limit
        if len(bucket) >= max_per_minute:
            log.warning(
                "replay_protection.rate_limit_exceeded",
                event_type=event_type,
                count=len(bucket),
                limit=max_per_minute,
            )
            return False

        # Add current event
        bucket.append(current_time)
        return True


class EventReconciliation:
    """
    Reconciles webhook events after outages or missed deliveries.

    Features:
    1. Detect gaps in event sequence
    2. Request replay of missed events
    3. Verify event completeness
    4. Generate reconciliation reports
    """

    def __init__(self, events_log_path: Optional[Path] = None):
        self.events_log_path = events_log_path or Path("data/webhook_events.jsonl")
        self._event_sequences: dict[str, list[str]] = {}

    def detect_gaps(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """
        Detect gaps in webhook event delivery.

        Returns list of gap periods with details.
        """
        gaps = []

        if not self.events_log_path.exists():
            return gaps

        events = self._load_events_in_range(start_time, end_time)

        # Sort events by timestamp
        events.sort(key=lambda e: e.get("timestamp", 0))

        # Detect gaps larger than expected interval
        expected_interval = 300  # 5 minutes max between events

        for i in range(1, len(events)):
            prev_time = events[i - 1].get("timestamp", 0)
            curr_time = events[i].get("timestamp", 0)

            if curr_time - prev_time > expected_interval:
                gaps.append({
                    "start": datetime.fromtimestamp(prev_time, tz=timezone.utc),
                    "end": datetime.fromtimestamp(curr_time, tz=timezone.utc),
                    "duration_seconds": curr_time - prev_time,
                    "last_event_before": events[i - 1].get("id"),
                    "first_event_after": events[i].get("id"),
                })

        return gaps

    def _load_events_in_range(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """Load events within specified time range."""
        events = []
        start_ts = start_time.timestamp()
        end_ts = end_time.timestamp()

        try:
            with self.events_log_path.open() as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        timestamp = event.get("timestamp", 0)
                        if start_ts <= timestamp <= end_ts:
                            events.append(event)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            log.error("reconciliation.load_events_failed", error=str(e))

        return events

    def verify_subscription_consistency(self) -> dict:
        """
        Verify that subscription events are consistent.

        Checks:
        1. Every created subscription has corresponding completed checkout
        2. Every canceled subscription was previously active
        3. No duplicate active subscriptions per customer

        Returns report of inconsistencies found.
        """
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "total_events": 0,
            "inconsistencies": [],
            "warnings": [],
        }

        if not self.events_log_path.exists():
            return report

        # Track subscription states
        subscriptions: dict[str, dict] = {}
        checkouts: dict[str, dict] = {}

        try:
            with self.events_log_path.open() as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        report["total_events"] += 1

                        event_type = event.get("type", "")
                        event_id = event.get("id", "")

                        if event_type == "checkout.session.completed":
                            session_id = event.get("session_id")
                            if session_id:
                                checkouts[session_id] = event

                        elif event_type == "customer.subscription.created":
                            sub_id = event.get("subscription_id")
                            if sub_id:
                                if sub_id in subscriptions:
                                    report["warnings"].append({
                                        "type": "duplicate_subscription_created",
                                        "subscription_id": sub_id,
                                        "events": [subscriptions[sub_id]["id"], event_id],
                                    })
                                subscriptions[sub_id] = {
                                    "id": event_id,
                                    "status": "active",
                                    "created_at": event.get("timestamp"),
                                }

                        elif event_type == "customer.subscription.deleted":
                            sub_id = event.get("subscription_id")
                            if sub_id:
                                if sub_id not in subscriptions:
                                    report["inconsistencies"].append({
                                        "type": "deleted_without_created",
                                        "subscription_id": sub_id,
                                        "event_id": event_id,
                                    })
                                else:
                                    subscriptions[sub_id]["status"] = "deleted"

                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            log.error("reconciliation.verify_failed", error=str(e))
            report["error"] = str(e)

        # Check for active subscriptions without checkouts
        for sub_id, sub_data in subscriptions.items():
            if sub_data["status"] == "active":
                # Should have a corresponding checkout
                has_checkout = any(
                    c.get("subscription_id") == sub_id
                    for c in checkouts.values()
                )
                if not has_checkout:
                    report["warnings"].append({
                        "type": "subscription_without_checkout",
                        "subscription_id": sub_id,
                        "event_id": sub_data["id"],
                    })

        return report

    def generate_audit_report(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> dict:
        """
        Generate comprehensive audit report for compliance.

        Includes:
        - Event counts by type
        - Processing success/failure rates
        - Detected anomalies
        - Reconciliation status
        """
        report = {
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_events": 0,
                "successful": 0,
                "failed": 0,
                "ignored": 0,
            },
            "by_type": {},
            "gaps_detected": [],
            "consistency_check": {},
        }

        # Load and analyze events
        events = self._load_events_in_range(start_date, end_date)

        for event in events:
            report["summary"]["total_events"] += 1

            action = event.get("action", "unknown")
            if action == "added" or action == "removed":
                report["summary"]["successful"] += 1
            elif action == "ignored":
                report["summary"]["ignored"] += 1
            else:
                report["summary"]["failed"] += 1

            # Count by type
            event_type = event.get("type", "unknown")
            if event_type not in report["by_type"]:
                report["by_type"][event_type] = {
                    "count": 0,
                    "successful": 0,
                    "failed": 0,
                    "ignored": 0,
                }

            report["by_type"][event_type]["count"] += 1
            if action == "added" or action == "removed":
                report["by_type"][event_type]["successful"] += 1
            elif action == "ignored":
                report["by_type"][event_type]["ignored"] += 1
            else:
                report["by_type"][event_type]["failed"] += 1

        # Add gap detection
        report["gaps_detected"] = self.detect_gaps(start_date, end_date)

        # Add consistency check
        report["consistency_check"] = self.verify_subscription_consistency()

        return report


def reconcile_missed_events(
    start_time: datetime,
    end_time: datetime,
    dry_run: bool = True,
) -> dict:
    """
    Main entry point for event reconciliation.

    Detects and optionally replays missed webhook events.
    """
    reconciler = EventReconciliation()

    result = {
        "dry_run": dry_run,
        "period": {
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
        },
        "gaps": reconciler.detect_gaps(start_time, end_time),
        "consistency": reconciler.verify_subscription_consistency(),
        "actions_taken": [],
    }

    if not dry_run and result["gaps"]:
        # In production, this would trigger Stripe API calls to replay events
        for gap in result["gaps"]:
            log.info(
                "reconciliation.replay_requested",
                gap_start=gap["start"],
                gap_end=gap["end"],
                duration=gap["duration_seconds"],
            )
            result["actions_taken"].append({
                "type": "replay_requested",
                "gap": gap,
                "status": "pending",
            })

    return result