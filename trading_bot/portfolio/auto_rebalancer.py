"""
Intelligent Portfolio Auto-Rebalancing Engine
Dynamic allocation with tax optimization and risk parity
"""

from __future__ import annotations

import asyncio
import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple
from enum import Enum

import structlog
import pandas as pd
from scipy.optimize import minimize

log = structlog.get_logger(__name__)


class RebalanceStrategy(Enum):
    """Portfolio rebalancing strategies"""
    THRESHOLD = "threshold"  # Rebalance when drift exceeds threshold
    CALENDAR = "calendar"  # Rebalance on schedule
    DYNAMIC = "dynamic"  # Adaptive based on market conditions
    VOLATILITY = "volatility"  # Rebalance based on volatility
    TAX_AWARE = "tax_aware"  # Minimize tax impact
    RISK_PARITY = "risk_parity"  # Equal risk contribution


@dataclass
class Asset:
    """Portfolio asset"""
    symbol: str
    name: str
    asset_class: str  # "equity", "bond", "commodity", "crypto", "reit"
    sector: str
    market_cap: str  # "large", "mid", "small"
    geography: str  # "us", "intl", "em"
    current_weight: float
    target_weight: float
    current_value: Decimal
    cost_basis: Decimal
    unrealized_gain: Decimal
    holding_period: timedelta
    dividend_yield: float
    expense_ratio: float
    risk_score: float
    correlation_group: str


@dataclass
class RebalanceAction:
    """Rebalancing trade action"""
    asset: Asset
    action: str  # "buy", "sell"
    quantity: Decimal
    value: Decimal
    reason: str
    priority: int  # 1 = highest
    tax_impact: Decimal
    transaction_cost: Decimal


@dataclass
class PortfolioAllocation:
    """Target portfolio allocation"""
    name: str
    description: str
    risk_level: int  # 1-10
    allocations: Dict[str, float]  # Asset class -> weight
    constraints: Dict[str, Any]
    rebalance_threshold: float
    min_trade_size: Decimal


class AutoRebalancer:
    """
    Sophisticated portfolio rebalancing engine.
    Implements modern portfolio theory with tax optimization.
    """

    def __init__(self):
        self.portfolio = {}
        self.target_allocations = {}
        self.rebalance_history = []
        self.tax_lots = []
        self.correlation_matrix = None
        self.strategies = {
            RebalanceStrategy.THRESHOLD: self._threshold_rebalance,
            RebalanceStrategy.CALENDAR: self._calendar_rebalance,
            RebalanceStrategy.DYNAMIC: self._dynamic_rebalance,
            RebalanceStrategy.VOLATILITY: self._volatility_rebalance,
            RebalanceStrategy.TAX_AWARE: self._tax_aware_rebalance,
            RebalanceStrategy.RISK_PARITY: self._risk_parity_rebalance
        }
        self.preset_portfolios = self._init_preset_portfolios()

    def _init_preset_portfolios(self) -> Dict[str, PortfolioAllocation]:
        """Initialize preset portfolio allocations"""
        return {
            "conservative": PortfolioAllocation(
                name="Conservative Growth",
                description="Low risk, stable returns",
                risk_level=3,
                allocations={
                    "bonds": 0.60,
                    "equity_large": 0.25,
                    "equity_intl": 0.10,
                    "cash": 0.05
                },
                constraints={"max_equity": 0.40},
                rebalance_threshold=0.05,
                min_trade_size=Decimal("100")
            ),
            "moderate": PortfolioAllocation(
                name="Moderate Growth",
                description="Balanced risk and return",
                risk_level=5,
                allocations={
                    "equity_large": 0.40,
                    "equity_mid": 0.10,
                    "equity_intl": 0.20,
                    "bonds": 0.25,
                    "reit": 0.05
                },
                constraints={"max_equity": 0.70},
                rebalance_threshold=0.075,
                min_trade_size=Decimal("100")
            ),
            "aggressive": PortfolioAllocation(
                name="Aggressive Growth",
                description="High risk, high potential return",
                risk_level=8,
                allocations={
                    "equity_large": 0.35,
                    "equity_mid": 0.15,
                    "equity_small": 0.10,
                    "equity_intl": 0.20,
                    "equity_em": 0.10,
                    "crypto": 0.05,
                    "commodity": 0.05
                },
                constraints={"max_equity": 0.90, "max_crypto": 0.10},
                rebalance_threshold=0.10,
                min_trade_size=Decimal("100")
            ),
            "all_weather": PortfolioAllocation(
                name="All Weather Portfolio",
                description="Ray Dalio's risk parity approach",
                risk_level=4,
                allocations={
                    "equity_large": 0.30,
                    "long_term_bonds": 0.40,
                    "intermediate_bonds": 0.15,
                    "commodity": 0.075,
                    "gold": 0.075
                },
                constraints={"risk_parity": True},
                rebalance_threshold=0.05,
                min_trade_size=Decimal("100")
            ),
            "three_fund": PortfolioAllocation(
                name="Three Fund Portfolio",
                description="Simple Bogleheads approach",
                risk_level=5,
                allocations={
                    "total_stock_market": 0.60,
                    "total_intl_stock": 0.20,
                    "total_bond_market": 0.20
                },
                constraints={},
                rebalance_threshold=0.10,
                min_trade_size=Decimal("100")
            ),
            "factor_based": PortfolioAllocation(
                name="Factor-Based Portfolio",
                description="Multi-factor smart beta",
                risk_level=6,
                allocations={
                    "value_stocks": 0.20,
                    "momentum_stocks": 0.20,
                    "quality_stocks": 0.20,
                    "low_volatility": 0.15,
                    "small_cap_value": 0.15,
                    "bonds": 0.10
                },
                constraints={"factor_exposure": True},
                rebalance_threshold=0.075,
                min_trade_size=Decimal("100")
            )
        }

    async def analyze_portfolio(
        self,
        assets: List[Asset],
        target_allocation: PortfolioAllocation
    ) -> Dict[str, Any]:
        """
        Analyze portfolio and determine if rebalancing is needed.
        
        Args:
            assets: Current portfolio assets
            target_allocation: Target allocation
        
        Returns:
            Analysis results including drift, risk metrics, and recommendations
        """
        # Calculate current allocation
        total_value = sum(asset.current_value for asset in assets)
        current_allocation = {}
        
        for asset in assets:
            asset_class = asset.asset_class
            weight = float(asset.current_value / total_value) if total_value > 0 else 0
            
            if asset_class not in current_allocation:
                current_allocation[asset_class] = 0
            current_allocation[asset_class] += weight
        
        # Calculate drift
        drift_analysis = self._calculate_drift(
            current_allocation,
            target_allocation.allocations
        )
        
        # Risk analysis
        risk_metrics = await self._calculate_risk_metrics(assets)
        
        # Tax implications
        tax_analysis = self._analyze_tax_implications(assets)
        
        # Correlation analysis
        correlation_analysis = self._analyze_correlations(assets)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(
            drift_analysis,
            risk_metrics,
            tax_analysis
        )
        
        return {
            "current_allocation": current_allocation,
            "target_allocation": target_allocation.allocations,
            "drift": drift_analysis,
            "risk_metrics": risk_metrics,
            "tax_analysis": tax_analysis,
            "correlation": correlation_analysis,
            "recommendations": recommendations,
            "needs_rebalancing": drift_analysis["max_drift"] > target_allocation.rebalance_threshold
        }

    async def rebalance(
        self,
        assets: List[Asset],
        target_allocation: PortfolioAllocation,
        strategy: RebalanceStrategy = RebalanceStrategy.THRESHOLD,
        constraints: Optional[Dict] = None
    ) -> List[RebalanceAction]:
        """
        Generate rebalancing actions.
        
        Args:
            assets: Current portfolio assets
            target_allocation: Target allocation
            strategy: Rebalancing strategy
            constraints: Additional constraints
        
        Returns:
            List of rebalancing actions to execute
        """
        if strategy not in self.strategies:
            raise ValueError(f"Unknown strategy: {strategy}")
        
        # Apply strategy-specific rebalancing
        actions = await self.strategies[strategy](
            assets,
            target_allocation,
            constraints or {}
        )
        
        # Optimize execution order
        actions = self._optimize_execution_order(actions)
        
        # Record rebalancing event
        self.rebalance_history.append({
            "timestamp": datetime.now(),
            "strategy": strategy.value,
            "actions": len(actions),
            "total_value": sum(a.value for a in actions)
        })
        
        return actions

    async def _threshold_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Rebalance when drift exceeds threshold"""
        actions = []
        total_value = sum(asset.current_value for asset in assets)
        
        for asset in assets:
            current_weight = float(asset.current_value / total_value)
            target_weight = target.allocations.get(asset.asset_class, 0)
            
            drift = abs(current_weight - target_weight)
            
            if drift > target.rebalance_threshold:
                # Calculate trade size
                target_value = Decimal(str(target_weight)) * total_value
                trade_value = target_value - asset.current_value
                
                if abs(trade_value) > target.min_trade_size:
                    action = RebalanceAction(
                        asset=asset,
                        action="buy" if trade_value > 0 else "sell",
                        quantity=abs(trade_value / asset.current_value * Decimal(str(asset.current_weight))),
                        value=abs(trade_value),
                        reason=f"Drift {drift:.1%} exceeds threshold",
                        priority=1 if drift > target.rebalance_threshold * 2 else 2,
                        tax_impact=self._calculate_tax_impact(asset, trade_value),
                        transaction_cost=abs(trade_value) * Decimal("0.001")  # 0.1% cost
                    )
                    actions.append(action)
        
        return actions

    async def _calendar_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Rebalance on a fixed schedule"""
        # Check if it's time to rebalance
        schedule = constraints.get("schedule", "quarterly")
        last_rebalance = self.rebalance_history[-1]["timestamp"] if self.rebalance_history else datetime.min
        
        if schedule == "monthly":
            next_rebalance = last_rebalance + timedelta(days=30)
        elif schedule == "quarterly":
            next_rebalance = last_rebalance + timedelta(days=90)
        elif schedule == "annually":
            next_rebalance = last_rebalance + timedelta(days=365)
        else:
            next_rebalance = datetime.now()
        
        if datetime.now() >= next_rebalance:
            # Force rebalance to target
            return await self._force_rebalance(assets, target)
        
        return []

    async def _dynamic_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Dynamic rebalancing based on market conditions"""
        actions = []
        
        # Get market indicators
        market_volatility = constraints.get("market_volatility", 0.15)
        market_trend = constraints.get("market_trend", "neutral")
        
        # Adjust thresholds based on market conditions
        if market_volatility > 0.25:  # High volatility
            adjusted_threshold = target.rebalance_threshold * 0.5  # More frequent
        elif market_volatility < 0.10:  # Low volatility
            adjusted_threshold = target.rebalance_threshold * 1.5  # Less frequent
        else:
            adjusted_threshold = target.rebalance_threshold
        
        # Adjust target weights based on market trend
        adjusted_target = target.allocations.copy()
        
        if market_trend == "bullish":
            # Increase equity allocation slightly
            for asset_class in adjusted_target:
                if "equity" in asset_class:
                    adjusted_target[asset_class] *= 1.1
                elif "bonds" in asset_class:
                    adjusted_target[asset_class] *= 0.9
        elif market_trend == "bearish":
            # Decrease equity allocation
            for asset_class in adjusted_target:
                if "equity" in asset_class:
                    adjusted_target[asset_class] *= 0.9
                elif "bonds" in asset_class or asset_class == "cash":
                    adjusted_target[asset_class] *= 1.1
        
        # Normalize weights
        total_weight = sum(adjusted_target.values())
        adjusted_target = {k: v/total_weight for k, v in adjusted_target.items()}
        
        # Create modified target and rebalance
        modified_target = PortfolioAllocation(
            **{**target.__dict__, "allocations": adjusted_target, "rebalance_threshold": adjusted_threshold}
        )
        
        return await self._threshold_rebalance(assets, modified_target, constraints)

    async def _volatility_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Rebalance based on asset volatility"""
        actions = []
        
        # Calculate volatility-adjusted weights
        volatilities = [asset.risk_score for asset in assets]
        inverse_vols = [1/v if v > 0 else 1 for v in volatilities]
        total_inverse = sum(inverse_vols)
        
        vol_adjusted_weights = [iv/total_inverse for iv in inverse_vols]
        
        total_value = sum(asset.current_value for asset in assets)
        
        for asset, target_weight in zip(assets, vol_adjusted_weights):
            current_weight = float(asset.current_value / total_value)
            
            if abs(current_weight - target_weight) > 0.02:  # 2% threshold
                target_value = Decimal(str(target_weight)) * total_value
                trade_value = target_value - asset.current_value
                
                if abs(trade_value) > target.min_trade_size:
                    action = RebalanceAction(
                        asset=asset,
                        action="buy" if trade_value > 0 else "sell",
                        quantity=abs(trade_value / asset.current_value * Decimal(str(asset.current_weight))),
                        value=abs(trade_value),
                        reason=f"Volatility-based adjustment",
                        priority=2,
                        tax_impact=self._calculate_tax_impact(asset, trade_value),
                        transaction_cost=abs(trade_value) * Decimal("0.001")
                    )
                    actions.append(action)
        
        return actions

    async def _tax_aware_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Tax-optimized rebalancing"""
        actions = []
        
        # Separate assets by tax status
        taxable_assets = [a for a in assets if a.holding_period < timedelta(days=365)]
        long_term_assets = [a for a in assets if a.holding_period >= timedelta(days=365)]
        
        # Prioritize selling assets with losses (tax loss harvesting)
        loss_assets = sorted(
            [a for a in assets if a.unrealized_gain < 0],
            key=lambda x: x.unrealized_gain
        )
        
        # Then long-term gains (lower tax rate)
        long_gain_assets = sorted(
            [a for a in long_term_assets if a.unrealized_gain > 0],
            key=lambda x: x.unrealized_gain
        )
        
        # Avoid short-term gains if possible
        short_gain_assets = sorted(
            [a for a in taxable_assets if a.unrealized_gain > 0],
            key=lambda x: x.unrealized_gain,
            reverse=True  # Start with highest gains to minimize trades
        )
        
        # Generate tax-efficient trades
        ordered_assets = loss_assets + long_gain_assets + short_gain_assets
        
        total_value = sum(asset.current_value for asset in assets)
        
        for asset in ordered_assets:
            current_weight = float(asset.current_value / total_value)
            target_weight = target.allocations.get(asset.asset_class, 0)
            
            drift = abs(current_weight - target_weight)
            
            if drift > target.rebalance_threshold:
                target_value = Decimal(str(target_weight)) * total_value
                trade_value = target_value - asset.current_value
                
                # Skip if tax impact is too high
                tax_impact = self._calculate_tax_impact(asset, trade_value)
                if tax_impact > abs(trade_value) * Decimal("0.15"):  # >15% tax
                    continue
                
                if abs(trade_value) > target.min_trade_size:
                    action = RebalanceAction(
                        asset=asset,
                        action="buy" if trade_value > 0 else "sell",
                        quantity=abs(trade_value / asset.current_value * Decimal(str(asset.current_weight))),
                        value=abs(trade_value),
                        reason=f"Tax-optimized rebalancing",
                        priority=3 if asset in loss_assets else 4,
                        tax_impact=tax_impact,
                        transaction_cost=abs(trade_value) * Decimal("0.001")
                    )
                    actions.append(action)
        
        return actions

    async def _risk_parity_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation,
        constraints: Dict
    ) -> List[RebalanceAction]:
        """Risk parity rebalancing - equal risk contribution"""
        if self.correlation_matrix is None:
            # Need correlation matrix for risk parity
            self.correlation_matrix = await self._calculate_correlation_matrix(assets)
        
        # Calculate risk contributions
        weights = np.array([float(a.current_value) for a in assets])
        weights = weights / weights.sum()
        
        volatilities = np.array([a.risk_score for a in assets])
        
        # Marginal risk contributions
        portfolio_vol = np.sqrt(np.dot(weights, np.dot(self.correlation_matrix * np.outer(volatilities, volatilities), weights)))
        marginal_contrib = np.dot(self.correlation_matrix * np.outer(volatilities, volatilities), weights) / portfolio_vol
        risk_contrib = weights * marginal_contrib
        
        # Target equal risk contribution
        target_contrib = 1.0 / len(assets)
        
        actions = []
        total_value = sum(asset.current_value for asset in assets)
        
        for i, asset in enumerate(assets):
            current_contrib = risk_contrib[i]
            
            if abs(current_contrib - target_contrib) > 0.05:  # 5% tolerance
                # Adjust weight to achieve target contribution
                adjustment_factor = target_contrib / current_contrib if current_contrib > 0 else 1
                new_weight = weights[i] * adjustment_factor
                
                # Normalize
                new_weight = new_weight / (weights.sum() - weights[i] + new_weight)
                
                target_value = Decimal(str(new_weight)) * total_value
                trade_value = target_value - asset.current_value
                
                if abs(trade_value) > target.min_trade_size:
                    action = RebalanceAction(
                        asset=asset,
                        action="buy" if trade_value > 0 else "sell",
                        quantity=abs(trade_value / asset.current_value * Decimal(str(weights[i]))),
                        value=abs(trade_value),
                        reason=f"Risk parity adjustment",
                        priority=2,
                        tax_impact=self._calculate_tax_impact(asset, trade_value),
                        transaction_cost=abs(trade_value) * Decimal("0.001")
                    )
                    actions.append(action)
        
        return actions

    async def _force_rebalance(
        self,
        assets: List[Asset],
        target: PortfolioAllocation
    ) -> List[RebalanceAction]:
        """Force rebalance to exact target allocation"""
        actions = []
        total_value = sum(asset.current_value for asset in assets)
        
        for asset in assets:
            target_weight = target.allocations.get(asset.asset_class, 0)
            target_value = Decimal(str(target_weight)) * total_value
            trade_value = target_value - asset.current_value
            
            if abs(trade_value) > target.min_trade_size:
                action = RebalanceAction(
                    asset=asset,
                    action="buy" if trade_value > 0 else "sell",
                    quantity=abs(trade_value / asset.current_value * Decimal(str(asset.current_weight))),
                    value=abs(trade_value),
                    reason="Forced rebalance to target",
                    priority=1,
                    tax_impact=self._calculate_tax_impact(asset, trade_value),
                    transaction_cost=abs(trade_value) * Decimal("0.001")
                )
                actions.append(action)
        
        return actions

    def _calculate_drift(
        self,
        current: Dict[str, float],
        target: Dict[str, float]
    ) -> Dict[str, Any]:
        """Calculate portfolio drift from target"""
        drift_by_class = {}
        total_drift = 0
        max_drift = 0
        
        for asset_class in set(list(current.keys()) + list(target.keys())):
            current_weight = current.get(asset_class, 0)
            target_weight = target.get(asset_class, 0)
            drift = current_weight - target_weight
            
            drift_by_class[asset_class] = {
                "current": current_weight,
                "target": target_weight,
                "drift": drift,
                "drift_pct": drift / target_weight if target_weight > 0 else 0
            }
            
            total_drift += abs(drift)
            max_drift = max(max_drift, abs(drift))
        
        return {
            "by_class": drift_by_class,
            "total_drift": total_drift,
            "max_drift": max_drift
        }

    async def _calculate_risk_metrics(
        self,
        assets: List[Asset]
    ) -> Dict[str, float]:
        """Calculate portfolio risk metrics"""
        weights = np.array([float(a.current_value) for a in assets])
        weights = weights / weights.sum() if weights.sum() > 0 else weights
        
        volatilities = np.array([a.risk_score for a in assets])
        
        # Portfolio volatility (simplified - would use covariance in production)
        portfolio_vol = np.sqrt(np.sum((weights * volatilities) ** 2))
        
        # Concentration risk (Herfindahl index)
        herfindahl = np.sum(weights ** 2)
        
        # Maximum position
        max_position = np.max(weights) if len(weights) > 0 else 0
        
        return {
            "portfolio_volatility": float(portfolio_vol),
            "herfindahl_index": float(herfindahl),
            "max_position_weight": float(max_position),
            "effective_diversification": 1.0 / herfindahl if herfindahl > 0 else 1
        }

    def _analyze_tax_implications(
        self,
        assets: List[Asset]
    ) -> Dict[str, Any]:
        """Analyze tax implications of current holdings"""
        short_term_gains = Decimal("0")
        long_term_gains = Decimal("0")
        unrealized_losses = Decimal("0")
        
        for asset in assets:
            if asset.unrealized_gain > 0:
                if asset.holding_period < timedelta(days=365):
                    short_term_gains += asset.unrealized_gain
                else:
                    long_term_gains += asset.unrealized_gain
            else:
                unrealized_losses += abs(asset.unrealized_gain)
        
        # Tax estimates (simplified)
        short_term_tax = short_term_gains * Decimal("0.35")  # 35% rate
        long_term_tax = long_term_gains * Decimal("0.15")  # 15% rate
        
        return {
            "short_term_gains": float(short_term_gains),
            "long_term_gains": float(long_term_gains),
            "unrealized_losses": float(unrealized_losses),
            "estimated_tax_short": float(short_term_tax),
            "estimated_tax_long": float(long_term_tax),
            "tax_loss_harvesting_available": float(unrealized_losses)
        }

    def _analyze_correlations(
        self,
        assets: List[Asset]
    ) -> Dict[str, float]:
        """Analyze asset correlations"""
        # Group assets by correlation group
        groups = {}
        for asset in assets:
            group = asset.correlation_group
            if group not in groups:
                groups[group] = []
            groups[group].append(asset)
        
        # Calculate average intra-group correlation (simplified)
        avg_correlation = 0.5  # Placeholder
        
        return {
            "average_correlation": avg_correlation,
            "correlation_groups": len(groups),
            "max_group_concentration": max(len(g) / len(assets) for g in groups.values()) if groups else 0
        }

    def _generate_recommendations(
        self,
        drift: Dict[str, Any],
        risk: Dict[str, float],
        tax: Dict[str, Any]
    ) -> List[str]:
        """Generate actionable recommendations"""
        recommendations = []
        
        # Drift recommendations
        if drift["max_drift"] > 0.10:
            recommendations.append(f"Portfolio drift of {drift['max_drift']:.1%} exceeds 10% - consider rebalancing")
        
        # Risk recommendations
        if risk["herfindahl_index"] > 0.20:
            recommendations.append(f"High concentration risk (HHI={risk['herfindahl_index']:.2f}) - consider diversifying")
        
        if risk["portfolio_volatility"] > 0.20:
            recommendations.append(f"High portfolio volatility ({risk['portfolio_volatility']:.1%}) - consider adding defensive assets")
        
        # Tax recommendations
        if tax["unrealized_losses"] > 1000:
            recommendations.append(f"${tax['unrealized_losses']:.0f} in tax loss harvesting opportunities available")
        
        if tax["short_term_gains"] > tax["long_term_gains"]:
            recommendations.append("Consider holding positions longer to qualify for long-term capital gains treatment")
        
        return recommendations

    def _calculate_tax_impact(
        self,
        asset: Asset,
        trade_value: Decimal
    ) -> Decimal:
        """Calculate tax impact of a trade"""
        if trade_value > 0:  # Buying
            return Decimal("0")
        
        # Selling
        if asset.unrealized_gain <= 0:
            return Decimal("0")  # Loss or breakeven
        
        # Calculate tax on gain
        if asset.holding_period < timedelta(days=365):
            tax_rate = Decimal("0.35")  # Short-term rate
        else:
            tax_rate = Decimal("0.15")  # Long-term rate
        
        # Tax on proportional gain
        proportion_sold = abs(trade_value) / asset.current_value
        taxable_gain = asset.unrealized_gain * proportion_sold
        
        return taxable_gain * tax_rate

    def _optimize_execution_order(
        self,
        actions: List[RebalanceAction]
    ) -> List[RebalanceAction]:
        """Optimize order of execution for tax and cost efficiency"""
        # Sort by priority, then by tax impact (ascending)
        return sorted(
            actions,
            key=lambda x: (x.priority, x.tax_impact, -x.value)
        )

    async def _calculate_correlation_matrix(
        self,
        assets: List[Asset]
    ) -> np.ndarray:
        """Calculate asset correlation matrix"""
        # Simplified - would use historical returns in production
        n = len(assets)
        correlation = np.eye(n)
        
        for i in range(n):
            for j in range(i + 1, n):
                # Higher correlation for same asset class
                if assets[i].asset_class == assets[j].asset_class:
                    corr = 0.8
                elif assets[i].correlation_group == assets[j].correlation_group:
                    corr = 0.5
                else:
                    corr = 0.2
                
                correlation[i, j] = corr
                correlation[j, i] = corr
        
        return correlation


# Export main components
__all__ = [
    "AutoRebalancer",
    "RebalanceStrategy",
    "Asset",
    "RebalanceAction",
    "PortfolioAllocation"
]