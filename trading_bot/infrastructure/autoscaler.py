"""
Phase 12: Auto-scaling Infrastructure

Provides dynamic scaling based on:
1. Request load and latency
2. Resource utilization (CPU, memory)
3. Queue depth for async tasks
4. Time-based scaling patterns
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ScalingMetrics:
    """Metrics used for scaling decisions."""

    timestamp: float = field(default_factory=time.time)
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    request_rate: float = 0.0  # requests per second
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    queue_depth: int = 0
    active_connections: int = 0
    error_rate: float = 0.0


@dataclass
class ScalingDecision:
    """Result of a scaling evaluation."""

    action: str  # "scale_up", "scale_down", "maintain"
    current_replicas: int
    target_replicas: int
    reason: str
    confidence: float  # 0.0 to 1.0
    cooldown_seconds: int = 60


class MetricsProvider(Protocol):
    """Protocol for metrics collection."""

    async def get_current_metrics(self) -> ScalingMetrics:
        """Get current system metrics."""
        ...


class ScalingBackend(Protocol):
    """Protocol for scaling backends (Railway, K8s, etc)."""

    async def get_replica_count(self) -> int:
        """Get current number of replicas."""
        ...

    async def set_replica_count(self, count: int) -> bool:
        """Set target replica count."""
        ...

    async def get_deployment_status(self) -> dict:
        """Get deployment health status."""
        ...


class AutoScaler:
    """
    Intelligent auto-scaling controller.

    Features:
    1. Predictive scaling based on patterns
    2. Multi-metric decision making
    3. Cooldown periods to prevent flapping
    4. Cost-aware scaling limits
    """

    def __init__(
        self,
        metrics_provider: MetricsProvider,
        scaling_backend: ScalingBackend,
        min_replicas: int = 1,
        max_replicas: int = 10,
        target_cpu: float = 70.0,
        target_memory: float = 80.0,
        target_p95_ms: float = 500.0,
    ):
        self.metrics_provider = metrics_provider
        self.scaling_backend = scaling_backend
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        self.target_cpu = target_cpu
        self.target_memory = target_memory
        self.target_p95_ms = target_p95_ms

        # Historical metrics for trend analysis
        self.metrics_history: deque[ScalingMetrics] = deque(maxlen=100)
        self.scaling_history: deque[ScalingDecision] = deque(maxlen=50)

        # Cooldown tracking
        self.last_scale_time: float = 0
        self.cooldown_period: int = 60  # seconds

        # Time-based patterns (hour -> typical load)
        self.hourly_patterns: dict[int, float] = {}
        self.daily_patterns: dict[int, float] = {}

    async def evaluate_scaling(self) -> ScalingDecision:
        """
        Evaluate current metrics and decide on scaling action.

        Returns ScalingDecision with action to take.
        """
        metrics = await self.metrics_provider.get_current_metrics()
        self.metrics_history.append(metrics)

        current_replicas = await self.scaling_backend.get_replica_count()

        # Check cooldown period
        if self._in_cooldown():
            return ScalingDecision(
                action="maintain",
                current_replicas=current_replicas,
                target_replicas=current_replicas,
                reason="cooldown_period_active",
                confidence=1.0,
            )

        # Calculate scaling factors
        cpu_factor = metrics.cpu_percent / self.target_cpu
        memory_factor = metrics.memory_percent / self.target_memory
        latency_factor = metrics.p95_latency_ms / self.target_p95_ms

        # Queue depth factor (scale up if queue is growing)
        queue_factor = 1.0
        if metrics.queue_depth > 100:
            queue_factor = 1.0 + (metrics.queue_depth - 100) / 100

        # Error rate factor (scale up if errors are high)
        error_factor = 1.0
        if metrics.error_rate > 0.01:  # > 1% error rate
            error_factor = 1.0 + metrics.error_rate * 10

        # Combined scaling factor (weighted average)
        combined_factor = (
            cpu_factor * 0.3 +
            memory_factor * 0.2 +
            latency_factor * 0.3 +
            queue_factor * 0.1 +
            error_factor * 0.1
        )

        # Add predictive component based on time patterns
        predictive_factor = self._get_predictive_factor()
        if predictive_factor > 1.0:
            combined_factor = max(combined_factor, predictive_factor * 0.8)

        # Determine target replicas
        target_replicas = int(current_replicas * combined_factor)
        target_replicas = max(self.min_replicas, min(target_replicas, self.max_replicas))

        # Determine action and confidence
        if target_replicas > current_replicas:
            action = "scale_up"
            reason = self._get_scale_up_reason(metrics)
            confidence = min(1.0, combined_factor - 1.0)
        elif target_replicas < current_replicas:
            action = "scale_down"
            reason = self._get_scale_down_reason(metrics)
            confidence = min(1.0, 1.0 - combined_factor)
        else:
            action = "maintain"
            reason = "metrics_within_targets"
            confidence = 0.5

        decision = ScalingDecision(
            action=action,
            current_replicas=current_replicas,
            target_replicas=target_replicas,
            reason=reason,
            confidence=confidence,
            cooldown_seconds=self.cooldown_period,
        )

        self.scaling_history.append(decision)
        return decision

    async def execute_scaling(self, decision: ScalingDecision) -> bool:
        """
        Execute a scaling decision.

        Returns True if scaling was successful.
        """
        if decision.action == "maintain":
            return True

        # Check confidence threshold
        if decision.confidence < 0.3:
            log.info(
                "autoscaler.low_confidence_skip",
                action=decision.action,
                confidence=decision.confidence,
            )
            return False

        # Execute scaling
        success = await self.scaling_backend.set_replica_count(decision.target_replicas)

        if success:
            self.last_scale_time = time.time()
            log.info(
                "autoscaler.scaled",
                action=decision.action,
                from_replicas=decision.current_replicas,
                to_replicas=decision.target_replicas,
                reason=decision.reason,
            )
        else:
            log.error(
                "autoscaler.scale_failed",
                action=decision.action,
                target_replicas=decision.target_replicas,
            )

        return success

    def _in_cooldown(self) -> bool:
        """Check if we're in cooldown period."""
        return time.time() - self.last_scale_time < self.cooldown_period

    def _get_predictive_factor(self) -> float:
        """
        Get predictive scaling factor based on time patterns.

        Returns factor > 1.0 if we expect increased load.
        """
        now = datetime.now(timezone.utc)
        hour = now.hour
        weekday = now.weekday()

        # Market hours (9:30 AM - 4:00 PM ET, UTC-5)
        market_open_utc = 14.5  # 9:30 AM ET
        market_close_utc = 21.0  # 4:00 PM ET

        # Scale up before market open
        if market_open_utc - 0.5 <= hour < market_open_utc:
            return 1.5  # Pre-market scale up

        # Peak during market hours on weekdays
        if 0 <= weekday <= 4:  # Monday-Friday
            if market_open_utc <= hour < market_close_utc:
                return 1.3  # Market hours scaling

        # Use learned patterns if available
        if hour in self.hourly_patterns:
            return self.hourly_patterns[hour]

        return 1.0

    def _get_scale_up_reason(self, metrics: ScalingMetrics) -> str:
        """Determine primary reason for scaling up."""
        reasons = []

        if metrics.cpu_percent > self.target_cpu:
            reasons.append(f"high_cpu_{metrics.cpu_percent:.1f}%")
        if metrics.memory_percent > self.target_memory:
            reasons.append(f"high_memory_{metrics.memory_percent:.1f}%")
        if metrics.p95_latency_ms > self.target_p95_ms:
            reasons.append(f"high_latency_{metrics.p95_latency_ms:.0f}ms")
        if metrics.queue_depth > 100:
            reasons.append(f"queue_depth_{metrics.queue_depth}")
        if metrics.error_rate > 0.01:
            reasons.append(f"error_rate_{metrics.error_rate:.2%}")

        return " + ".join(reasons) if reasons else "combined_metrics_high"

    def _get_scale_down_reason(self, metrics: ScalingMetrics) -> str:
        """Determine primary reason for scaling down."""
        reasons = []

        if metrics.cpu_percent < self.target_cpu * 0.5:
            reasons.append(f"low_cpu_{metrics.cpu_percent:.1f}%")
        if metrics.memory_percent < self.target_memory * 0.5:
            reasons.append(f"low_memory_{metrics.memory_percent:.1f}%")
        if metrics.request_rate < 1.0:
            reasons.append(f"low_requests_{metrics.request_rate:.1f}/s")

        return " + ".join(reasons) if reasons else "underutilized"

    async def learn_patterns(self) -> None:
        """
        Learn traffic patterns from historical metrics.

        Updates hourly and daily patterns for predictive scaling.
        """
        if len(self.metrics_history) < 20:
            return

        # Group metrics by hour
        hourly_loads: dict[int, list[float]] = {}

        for metric in self.metrics_history:
            dt = datetime.fromtimestamp(metric.timestamp, tz=timezone.utc)
            hour = dt.hour

            load = (
                metric.cpu_percent / self.target_cpu * 0.3 +
                metric.memory_percent / self.target_memory * 0.2 +
                metric.request_rate / 10.0 * 0.5  # Normalize to 10 req/s baseline
            )

            if hour not in hourly_loads:
                hourly_loads[hour] = []
            hourly_loads[hour].append(load)

        # Calculate average patterns
        for hour, loads in hourly_loads.items():
            self.hourly_patterns[hour] = sum(loads) / len(loads)

        log.info(
            "autoscaler.patterns_learned",
            hourly_patterns=self.hourly_patterns,
        )


class RailwayScalingBackend:
    """Railway-specific scaling backend."""

    def __init__(self, service_id: str, project_id: str):
        self.service_id = service_id
        self.project_id = project_id
        self.api_token = os.getenv("RAILWAY_API_TOKEN", "")

    async def get_replica_count(self) -> int:
        """Get current replica count from Railway."""
        # Railway doesn't have native replicas yet, return 1
        # This would integrate with Railway's API when horizontal scaling is available
        return 1

    async def set_replica_count(self, count: int) -> bool:
        """Set target replica count in Railway."""
        # Placeholder for Railway horizontal scaling API
        # For now, log the intent
        log.info(
            "railway.scale_request",
            service_id=self.service_id,
            target_replicas=count,
        )
        return True

    async def get_deployment_status(self) -> dict:
        """Get deployment health from Railway."""
        return {
            "healthy": True,
            "replicas": 1,
            "service_id": self.service_id,
        }


class SystemMetricsProvider:
    """System metrics collection provider."""

    def __init__(self):
        self.request_times: deque[float] = deque(maxlen=1000)
        self.request_latencies: deque[float] = deque(maxlen=1000)
        self.error_count = 0
        self.total_requests = 0

    async def get_current_metrics(self) -> ScalingMetrics:
        """Collect current system metrics."""
        try:
            import psutil

            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            memory_percent = memory.percent
        except ImportError:
            # Fallback if psutil not available
            cpu_percent = 50.0
            memory_percent = 60.0

        # Calculate request rate (requests in last minute)
        now = time.time()
        recent_requests = [t for t in self.request_times if now - t < 60]
        request_rate = len(recent_requests) / 60.0 if recent_requests else 0

        # Calculate latency percentiles
        if self.request_latencies:
            sorted_latencies = sorted(self.request_latencies)
            p95_idx = int(len(sorted_latencies) * 0.95)
            p99_idx = int(len(sorted_latencies) * 0.99)
            p95_latency = sorted_latencies[p95_idx] if p95_idx < len(sorted_latencies) else 0
            p99_latency = sorted_latencies[p99_idx] if p99_idx < len(sorted_latencies) else 0
        else:
            p95_latency = 0
            p99_latency = 0

        # Calculate error rate
        error_rate = self.error_count / self.total_requests if self.total_requests > 0 else 0

        return ScalingMetrics(
            cpu_percent=cpu_percent,
            memory_percent=memory_percent,
            request_rate=request_rate,
            p95_latency_ms=p95_latency,
            p99_latency_ms=p99_latency,
            queue_depth=0,  # Would be populated from async task queue
            active_connections=len(recent_requests),
            error_rate=error_rate,
        )

    def record_request(self, latency_ms: float, error: bool = False) -> None:
        """Record a request for metrics calculation."""
        self.request_times.append(time.time())
        self.request_latencies.append(latency_ms)
        self.total_requests += 1
        if error:
            self.error_count += 1


async def autoscaling_loop(
    autoscaler: AutoScaler,
    interval_seconds: int = 30,
) -> None:
    """
    Main autoscaling control loop.

    Continuously monitors metrics and makes scaling decisions.
    """
    log.info("autoscaler.started", interval=interval_seconds)

    while True:
        try:
            # Evaluate scaling need
            decision = await autoscaler.evaluate_scaling()

            # Log decision
            log.info(
                "autoscaler.decision",
                action=decision.action,
                current=decision.current_replicas,
                target=decision.target_replicas,
                reason=decision.reason,
                confidence=decision.confidence,
            )

            # Execute if needed
            if decision.action != "maintain":
                await autoscaler.execute_scaling(decision)

            # Learn patterns periodically
            if len(autoscaler.metrics_history) % 20 == 0:
                await autoscaler.learn_patterns()

        except Exception as e:
            log.error("autoscaler.error", error=str(e))

        await asyncio.sleep(interval_seconds)


# Export the main interface
__all__ = [
    "AutoScaler",
    "ScalingMetrics",
    "ScalingDecision",
    "RailwayScalingBackend",
    "SystemMetricsProvider",
    "autoscaling_loop",
]