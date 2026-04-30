"""
Infrastructure modules for scaling and deployment.
"""

from .autoscaler import (
    AutoScaler,
    RailwayScalingBackend,
    ScalingDecision,
    ScalingMetrics,
    SystemMetricsProvider,
    autoscaling_loop,
)
from .multiregion import (
    EDGE_LOCATIONS,
    GLOBAL_REGIONS,
    EdgeCache,
    MultiRegionRouter,
    Region,
    RegionConfig,
    RegionalDataStore,
)

__all__ = [
    # Autoscaling
    "AutoScaler",
    "ScalingMetrics",
    "ScalingDecision",
    "RailwayScalingBackend",
    "SystemMetricsProvider",
    "autoscaling_loop",
    # Multi-region
    "Region",
    "RegionConfig",
    "MultiRegionRouter",
    "RegionalDataStore",
    "EdgeCache",
    "GLOBAL_REGIONS",
    "EDGE_LOCATIONS",
]