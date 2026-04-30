"""
Phase 13: Multi-region Deployment Infrastructure

Provides global distribution with:
1. Geo-routing and latency optimization
2. Regional failover and disaster recovery
3. Data replication and consistency
4. Edge caching and CDN integration
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

import structlog

log = structlog.get_logger(__name__)


class Region(Enum):
    """Available deployment regions."""

    US_EAST = "us-east-1"
    US_WEST = "us-west-2"
    EU_WEST = "eu-west-1"
    EU_CENTRAL = "eu-central-1"
    AP_SOUTH = "ap-south-1"
    AP_NORTHEAST = "ap-northeast-1"


@dataclass
class RegionConfig:
    """Configuration for a region."""

    region: Region
    endpoint: str
    priority: int = 100  # Lower is higher priority
    healthy: bool = True
    latency_ms: float = 0.0
    capacity: int = 100  # Percentage of full capacity
    data_center: str = ""
    features: set[str] = field(default_factory=set)


@dataclass
class RoutingDecision:
    """Result of routing evaluation."""

    primary_region: Region
    fallback_regions: list[Region]
    latency_estimate_ms: float
    confidence: float
    reason: str


class DataReplicator(Protocol):
    """Protocol for data replication across regions."""

    async def replicate(
        self,
        key: str,
        value: Any,
        regions: list[Region],
    ) -> dict[Region, bool]:
        """Replicate data to specified regions."""
        ...

    async def read(
        self,
        key: str,
        region: Optional[Region] = None,
        consistency: str = "eventual",
    ) -> Optional[Any]:
        """Read data from region with specified consistency."""
        ...


class HealthChecker(Protocol):
    """Protocol for region health checking."""

    async def check_health(self, region: Region) -> bool:
        """Check if region is healthy."""
        ...

    async def get_latency(self, region: Region) -> float:
        """Get latency to region in milliseconds."""
        ...


class MultiRegionRouter:
    """
    Intelligent multi-region traffic router.

    Features:
    1. Geo-IP based routing
    2. Latency-optimized selection
    3. Automatic failover
    4. Load distribution
    """

    def __init__(
        self,
        regions: list[RegionConfig],
        health_checker: Optional[HealthChecker] = None,
    ):
        self.regions = {r.region: r for r in regions}
        self.health_checker = health_checker
        self.routing_cache: dict[str, RoutingDecision] = {}
        self.cache_ttl = 300  # 5 minutes

    async def route_request(
        self,
        client_ip: str,
        request_type: str = "api",
    ) -> RoutingDecision:
        """
        Determine optimal region for request.

        Args:
            client_ip: Client IP address for geo-location
            request_type: Type of request (api, webhook, static)

        Returns:
            RoutingDecision with primary and fallback regions
        """
        cache_key = f"{client_ip}:{request_type}"

        # Check cache
        if cache_key in self.routing_cache:
            cached = self.routing_cache[cache_key]
            if hasattr(cached, "_timestamp"):
                if time.time() - cached._timestamp < self.cache_ttl:  # type: ignore
                    return cached

        # Get client location (mock implementation)
        client_region = self._geolocate_ip(client_ip)

        # Get healthy regions
        healthy_regions = await self._get_healthy_regions()

        if not healthy_regions:
            # All regions down - return any region as fallback
            return RoutingDecision(
                primary_region=Region.US_EAST,
                fallback_regions=[],
                latency_estimate_ms=999,
                confidence=0.0,
                reason="all_regions_unhealthy",
            )

        # Sort by latency from client
        sorted_regions = await self._sort_by_latency(healthy_regions, client_region)

        # Select primary and fallbacks
        primary = sorted_regions[0]
        fallbacks = sorted_regions[1:3] if len(sorted_regions) > 1 else []

        decision = RoutingDecision(
            primary_region=primary.region,
            fallback_regions=[r.region for r in fallbacks],
            latency_estimate_ms=primary.latency_ms,
            confidence=0.9 if primary.healthy else 0.5,
            reason=f"geo_optimized_from_{client_region.value}",
        )

        # Cache decision
        decision._timestamp = time.time()  # type: ignore
        self.routing_cache[cache_key] = decision

        return decision

    def _geolocate_ip(self, ip: str) -> Region:
        """
        Determine region from IP address.

        This is a simplified mock - in production would use MaxMind or similar.
        """
        # Hash IP to deterministically assign region for testing
        ip_hash = hashlib.md5(ip.encode()).hexdigest()
        hash_int = int(ip_hash[:8], 16)

        regions = list(Region)
        return regions[hash_int % len(regions)]

    async def _get_healthy_regions(self) -> list[RegionConfig]:
        """Get list of healthy regions."""
        healthy = []

        for config in self.regions.values():
            if self.health_checker:
                config.healthy = await self.health_checker.check_health(config.region)

            if config.healthy:
                healthy.append(config)

        return healthy

    async def _sort_by_latency(
        self,
        regions: list[RegionConfig],
        client_region: Region,
    ) -> list[RegionConfig]:
        """Sort regions by estimated latency from client."""
        # Update latencies if health checker available
        if self.health_checker:
            for config in regions:
                config.latency_ms = await self.health_checker.get_latency(config.region)

        # Estimate latencies based on region proximity
        latency_matrix = self._get_latency_matrix()

        for config in regions:
            if client_region in latency_matrix and config.region in latency_matrix[client_region]:
                estimated = latency_matrix[client_region][config.region]
                # Use measured latency if available, otherwise estimate
                if config.latency_ms == 0:
                    config.latency_ms = estimated

        # Sort by latency and priority
        return sorted(regions, key=lambda r: (r.latency_ms, r.priority))

    def _get_latency_matrix(self) -> dict[Region, dict[Region, float]]:
        """
        Get estimated latency matrix between regions.

        Returns dict of region -> region -> latency_ms
        """
        return {
            Region.US_EAST: {
                Region.US_EAST: 5,
                Region.US_WEST: 70,
                Region.EU_WEST: 85,
                Region.EU_CENTRAL: 95,
                Region.AP_SOUTH: 220,
                Region.AP_NORTHEAST: 180,
            },
            Region.US_WEST: {
                Region.US_EAST: 70,
                Region.US_WEST: 5,
                Region.EU_WEST: 140,
                Region.EU_CENTRAL: 150,
                Region.AP_SOUTH: 180,
                Region.AP_NORTHEAST: 120,
            },
            Region.EU_WEST: {
                Region.US_EAST: 85,
                Region.US_WEST: 140,
                Region.EU_WEST: 5,
                Region.EU_CENTRAL: 25,
                Region.AP_SOUTH: 120,
                Region.AP_NORTHEAST: 240,
            },
            Region.EU_CENTRAL: {
                Region.US_EAST: 95,
                Region.US_WEST: 150,
                Region.EU_WEST: 25,
                Region.EU_CENTRAL: 5,
                Region.AP_SOUTH: 100,
                Region.AP_NORTHEAST: 230,
            },
            Region.AP_SOUTH: {
                Region.US_EAST: 220,
                Region.US_WEST: 180,
                Region.EU_WEST: 120,
                Region.EU_CENTRAL: 100,
                Region.AP_SOUTH: 5,
                Region.AP_NORTHEAST: 80,
            },
            Region.AP_NORTHEAST: {
                Region.US_EAST: 180,
                Region.US_WEST: 120,
                Region.EU_WEST: 240,
                Region.EU_CENTRAL: 230,
                Region.AP_SOUTH: 80,
                Region.AP_NORTHEAST: 5,
            },
        }


class RegionalDataStore:
    """
    Multi-region data storage with replication.

    Features:
    1. Eventual consistency by default
    2. Strong consistency when needed
    3. Conflict resolution
    4. Regional caching
    """

    def __init__(self, regions: list[Region]):
        self.regions = regions
        self.stores: dict[Region, dict[str, Any]] = {r: {} for r in regions}
        self.version_vectors: dict[str, dict[Region, int]] = {}
        self.write_quorum = len(regions) // 2 + 1
        self.read_quorum = len(regions) // 2 + 1

    async def write(
        self,
        key: str,
        value: Any,
        consistency: str = "eventual",
    ) -> bool:
        """
        Write data with specified consistency level.

        Args:
            key: Data key
            value: Data value
            consistency: "eventual" or "strong"

        Returns:
            True if write succeeded
        """
        # Update version vector
        if key not in self.version_vectors:
            self.version_vectors[key] = {r: 0 for r in self.regions}

        # Determine regions to write
        if consistency == "strong":
            required_acks = self.write_quorum
        else:
            required_acks = 1

        # Simulate async replication
        write_tasks = []
        for region in self.regions:
            write_tasks.append(self._write_to_region(key, value, region))

        results = await asyncio.gather(*write_tasks, return_exceptions=True)

        # Count successful writes
        success_count = sum(1 for r in results if r is True)

        return success_count >= required_acks

    async def read(
        self,
        key: str,
        consistency: str = "eventual",
        preferred_region: Optional[Region] = None,
    ) -> Optional[Any]:
        """
        Read data with specified consistency level.

        Args:
            key: Data key
            consistency: "eventual" or "strong"
            preferred_region: Preferred region to read from

        Returns:
            Data value or None if not found
        """
        if consistency == "eventual" and preferred_region:
            # Fast path: read from preferred region only
            return self.stores.get(preferred_region, {}).get(key)

        # Read from quorum for strong consistency
        read_tasks = []
        for region in self.regions:
            read_tasks.append(self._read_from_region(key, region))

        results = await asyncio.gather(*read_tasks, return_exceptions=True)

        # Filter successful reads
        values = [r for r in results if r is not None and not isinstance(r, Exception)]

        if not values:
            return None

        if consistency == "strong":
            # Return most recent version based on vector clock
            return self._resolve_conflicts(key, values)
        else:
            # Return first available
            return values[0]

    async def _write_to_region(
        self,
        key: str,
        value: Any,
        region: Region,
    ) -> bool:
        """Write to specific region."""
        try:
            # Simulate network delay
            await asyncio.sleep(self._get_write_latency(region) / 1000.0)

            # Update version
            self.version_vectors[key][region] += 1

            # Store value with metadata
            self.stores[region][key] = {
                "value": value,
                "version": self.version_vectors[key][region],
                "timestamp": time.time(),
            }

            return True
        except Exception as e:
            log.error(f"Write to {region.value} failed: {e}")
            return False

    async def _read_from_region(
        self,
        key: str,
        region: Region,
    ) -> Optional[Any]:
        """Read from specific region."""
        try:
            # Simulate network delay
            await asyncio.sleep(self._get_read_latency(region) / 1000.0)

            data = self.stores[region].get(key)
            return data["value"] if data else None
        except Exception as e:
            log.error(f"Read from {region.value} failed: {e}")
            return None

    def _get_write_latency(self, region: Region) -> float:
        """Get simulated write latency in ms."""
        base_latencies = {
            Region.US_EAST: 10,
            Region.US_WEST: 15,
            Region.EU_WEST: 20,
            Region.EU_CENTRAL: 25,
            Region.AP_SOUTH: 35,
            Region.AP_NORTHEAST: 30,
        }
        return base_latencies.get(region, 20)

    def _get_read_latency(self, region: Region) -> float:
        """Get simulated read latency in ms."""
        base_latencies = {
            Region.US_EAST: 5,
            Region.US_WEST: 8,
            Region.EU_WEST: 10,
            Region.EU_CENTRAL: 12,
            Region.AP_SOUTH: 18,
            Region.AP_NORTHEAST: 15,
        }
        return base_latencies.get(region, 10)

    def _resolve_conflicts(self, key: str, values: list[Any]) -> Any:
        """
        Resolve conflicts using last-write-wins with vector clock.

        In production, would use more sophisticated CRDT-based resolution.
        """
        if not values:
            return None

        # For now, return the value with highest total version
        max_version = -1
        result = values[0]

        for value in values:
            if isinstance(value, dict) and "version" in value:
                if value["version"] > max_version:
                    max_version = value["version"]
                    result = value["value"]

        return result


class EdgeCache:
    """
    Edge caching layer for global CDN.

    Features:
    1. Geographic edge locations
    2. Cache invalidation
    3. Tiered caching (L1 edge, L2 regional)
    4. Smart prefetching
    """

    def __init__(self, edge_locations: list[str]):
        self.edge_locations = edge_locations
        self.cache: dict[str, dict[str, Any]] = {loc: {} for loc in edge_locations}
        self.cache_stats: dict[str, dict[str, int]] = {
            loc: {"hits": 0, "misses": 0} for loc in edge_locations
        }

    async def get(
        self,
        key: str,
        location: str,
        fetch_fn: Optional[Any] = None,
    ) -> Optional[Any]:
        """
        Get from edge cache with fallback to fetch function.

        Args:
            key: Cache key
            location: Edge location
            fetch_fn: Async function to fetch if cache miss

        Returns:
            Cached or fetched value
        """
        # Check L1 edge cache
        if location in self.cache and key in self.cache[location]:
            entry = self.cache[location][key]
            if time.time() - entry["timestamp"] < entry["ttl"]:
                self.cache_stats[location]["hits"] += 1
                log.debug(f"Edge cache hit: {key} at {location}")
                return entry["value"]

        self.cache_stats[location]["misses"] += 1

        # Fetch from origin if provided
        if fetch_fn:
            value = await fetch_fn(key)
            if value is not None:
                await self.set(key, value, location)
            return value

        return None

    async def set(
        self,
        key: str,
        value: Any,
        location: str,
        ttl: int = 300,
    ) -> None:
        """
        Set edge cache entry.

        Args:
            key: Cache key
            value: Value to cache
            location: Edge location
            ttl: Time-to-live in seconds
        """
        if location not in self.cache:
            self.cache[location] = {}

        self.cache[location][key] = {
            "value": value,
            "timestamp": time.time(),
            "ttl": ttl,
        }

        # Propagate to nearby edges (simplified)
        await self._propagate_to_neighbors(key, value, location, ttl)

    async def invalidate(
        self,
        key: str,
        locations: Optional[list[str]] = None,
    ) -> None:
        """
        Invalidate cache entry across locations.

        Args:
            key: Cache key to invalidate
            locations: Specific locations or all if None
        """
        target_locations = locations or self.edge_locations

        for location in target_locations:
            if location in self.cache and key in self.cache[location]:
                del self.cache[location][key]
                log.debug(f"Invalidated {key} at {location}")

    async def _propagate_to_neighbors(
        self,
        key: str,
        value: Any,
        origin: str,
        ttl: int,
    ) -> None:
        """Propagate cache to nearby edge locations."""
        # Simplified: propagate to 2 random neighbors
        neighbors = [loc for loc in self.edge_locations if loc != origin][:2]

        for neighbor in neighbors:
            # Async propagation with reduced TTL
            asyncio.create_task(
                self.set(key, value, neighbor, ttl=ttl // 2)
            )

    def get_stats(self) -> dict[str, dict[str, Any]]:
        """Get cache statistics for monitoring."""
        stats = {}

        for location in self.edge_locations:
            location_stats = self.cache_stats.get(location, {})
            total = location_stats.get("hits", 0) + location_stats.get("misses", 0)
            hit_rate = location_stats.get("hits", 0) / total if total > 0 else 0

            stats[location] = {
                "hits": location_stats.get("hits", 0),
                "misses": location_stats.get("misses", 0),
                "hit_rate": f"{hit_rate:.2%}",
                "cache_size": len(self.cache.get(location, {})),
            }

        return stats


# Configuration for global deployment
GLOBAL_REGIONS = [
    RegionConfig(
        region=Region.US_EAST,
        endpoint="https://us-east.api.trading-bot.com",
        priority=1,
        data_center="aws-us-east-1",
        features={"billing", "webhooks"},
    ),
    RegionConfig(
        region=Region.US_WEST,
        endpoint="https://us-west.api.trading-bot.com",
        priority=2,
        data_center="aws-us-west-2",
        features={"api", "signals"},
    ),
    RegionConfig(
        region=Region.EU_WEST,
        endpoint="https://eu-west.api.trading-bot.com",
        priority=3,
        data_center="aws-eu-west-1",
        features={"api", "signals"},
    ),
    RegionConfig(
        region=Region.EU_CENTRAL,
        endpoint="https://eu-central.api.trading-bot.com",
        priority=4,
        data_center="aws-eu-central-1",
        features={"api"},
    ),
    RegionConfig(
        region=Region.AP_SOUTH,
        endpoint="https://ap-south.api.trading-bot.com",
        priority=5,
        data_center="aws-ap-south-1",
        features={"api"},
    ),
    RegionConfig(
        region=Region.AP_NORTHEAST,
        endpoint="https://ap-northeast.api.trading-bot.com",
        priority=6,
        data_center="aws-ap-northeast-1",
        features={"api"},
    ),
]

# Edge locations for CDN
EDGE_LOCATIONS = [
    "edge-nyc",
    "edge-sfo",
    "edge-chi",
    "edge-dal",
    "edge-lon",
    "edge-fra",
    "edge-ams",
    "edge-sin",
    "edge-hkg",
    "edge-syd",
    "edge-tok",
    "edge-bom",
]

# Export main components
__all__ = [
    "Region",
    "RegionConfig",
    "MultiRegionRouter",
    "RegionalDataStore",
    "EdgeCache",
    "GLOBAL_REGIONS",
    "EDGE_LOCATIONS",
]