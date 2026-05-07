"""
World-Class Social Trading Platform
Copy trading, leaderboards, and community features
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import structlog

log = structlog.get_logger(__name__)


@dataclass
class Trader:
    """Elite trader profile"""
    id: str
    username: str
    display_name: str
    avatar_url: str
    bio: str
    verified: bool
    followers: int
    following: int
    total_return_pct: float
    win_rate: float
    sharpe_ratio: float
    max_drawdown: float
    trades_count: int
    joined_date: datetime
    risk_score: int  # 1-10
    badges: List[str]
    subscription_fee: float  # Monthly fee to copy this trader
    performance_history: List[Dict]
    current_positions: List[Dict]
    strategies: List[str]


@dataclass
class TradeCopy:
    """Copy trading configuration"""
    follower_id: str
    trader_id: str
    allocation: float  # Amount allocated
    max_risk_per_trade: float
    copy_ratio: float  # 0.1 = copy 10% of trader's position size
    auto_stop_loss: bool
    max_daily_loss: float
    allowed_symbols: Set[str]
    blocked_symbols: Set[str]
    status: str  # "active", "paused", "stopped"


class SocialTradingEngine:
    """
    Revolutionary social trading platform.
    Connects millions of traders globally.
    """

    def __init__(self):
        self.traders: Dict[str, Trader] = {}
        self.copy_relationships: Dict[str, List[TradeCopy]] = {}
        self.trade_feed: List[Dict] = []
        self.leaderboard_cache: Dict[str, List[Trader]] = {}
        self.community_signals: List[Dict] = []

    async def register_trader(self, trader: Trader) -> str:
        """Register new elite trader"""
        self.traders[trader.id] = trader

        # Calculate initial rankings
        await self._update_rankings(trader)

        # Send welcome notification
        await self._send_notification(
            trader.id,
            "Welcome to Elite Trading Network!",
            f"Complete your profile to start earning from {trader.followers} followers."
        )

        log.info(f"Registered elite trader: {trader.username}")
        return trader.id

    async def follow_trader(
        self,
        follower_id: str,
        trader_id: str,
        config: TradeCopy
    ) -> bool:
        """Start following and copying a trader"""
        if trader_id not in self.traders:
            return False

        trader = self.traders[trader_id]

        # Check subscription payment
        if not await self._process_subscription_payment(
            follower_id,
            trader.subscription_fee
        ):
            return False

        # Create copy relationship
        if follower_id not in self.copy_relationships:
            self.copy_relationships[follower_id] = []

        self.copy_relationships[follower_id].append(config)

        # Update trader stats
        trader.followers += 1

        # Send notifications
        await asyncio.gather(
            self._send_notification(
                follower_id,
                "Now Following",
                f"You're now copying {trader.display_name}'s trades"
            ),
            self._send_notification(
                trader_id,
                "New Follower",
                f"You have a new follower! Total: {trader.followers}"
            )
        )

        return True

    async def execute_trade(
        self,
        trader_id: str,
        trade: Dict
    ) -> List[Dict]:
        """Execute trade and copy to all followers"""
        if trader_id not in self.traders:
            return []

        copies_executed = []

        # Find all followers
        followers = self._get_followers(trader_id)

        # Execute copies in parallel
        copy_tasks = []
        for follower_id, copy_config in followers:
            if copy_config.status == "active":
                copy_tasks.append(
                    self._execute_copy_trade(
                        follower_id,
                        copy_config,
                        trade
                    )
                )

        # Execute all copies
        if copy_tasks:
            copies = await asyncio.gather(*copy_tasks, return_exceptions=True)
            copies_executed = [c for c in copies if not isinstance(c, Exception)]

        # Update trade feed
        await self._update_trade_feed(trader_id, trade, len(copies_executed))

        # Update trader stats
        await self._update_trader_performance(trader_id, trade)

        return copies_executed

    async def _execute_copy_trade(
        self,
        follower_id: str,
        config: TradeCopy,
        original_trade: Dict
    ) -> Dict:
        """Execute copy trade with risk management"""
        # Check if symbol is allowed
        symbol = original_trade["symbol"]
        if symbol in config.blocked_symbols:
            return {}
        if config.allowed_symbols and symbol not in config.allowed_symbols:
            return {}

        # Calculate position size
        original_size = original_trade["quantity"]
        copy_size = int(original_size * config.copy_ratio)

        if copy_size == 0:
            return {}

        # Check risk limits
        trade_value = copy_size * original_trade["price"]
        max_risk_value = config.allocation * config.max_risk_per_trade

        if trade_value > max_risk_value:
            copy_size = int(max_risk_value / original_trade["price"])

        # Create copy trade
        copy_trade = {
            **original_trade,
            "quantity": copy_size,
            "follower_id": follower_id,
            "original_trader": config.trader_id,
            "copy_ratio": config.copy_ratio,
            "timestamp": datetime.now()
        }

        # Add auto stop-loss if configured
        if config.auto_stop_loss and "stop_loss" not in copy_trade:
            copy_trade["stop_loss"] = original_trade["price"] * 0.98

        # Execute trade (would call actual broker here)
        log.info(f"Executed copy trade for {follower_id}: {copy_trade}")

        return copy_trade

    def _get_followers(
        self,
        trader_id: str
    ) -> List[Tuple[str, TradeCopy]]:
        """Get all active followers of a trader"""
        followers = []

        for follower_id, configs in self.copy_relationships.items():
            for config in configs:
                if config.trader_id == trader_id:
                    followers.append((follower_id, config))

        return followers

    async def get_leaderboard(
        self,
        category: str = "returns",
        timeframe: str = "monthly"
    ) -> List[Trader]:
        """Get trader leaderboard"""
        cache_key = f"{category}:{timeframe}"

        # Check cache
        if cache_key in self.leaderboard_cache:
            cached = self.leaderboard_cache[cache_key]
            # Return if cache is less than 5 minutes old
            # (Implement cache timestamp logic)
            return cached

        # Calculate leaderboard
        traders = list(self.traders.values())

        if category == "returns":
            traders.sort(key=lambda t: t.total_return_pct, reverse=True)
        elif category == "followers":
            traders.sort(key=lambda t: t.followers, reverse=True)
        elif category == "sharpe":
            traders.sort(key=lambda t: t.sharpe_ratio, reverse=True)
        elif category == "win_rate":
            traders.sort(key=lambda t: t.win_rate, reverse=True)

        # Cache result
        self.leaderboard_cache[cache_key] = traders[:100]

        return traders[:100]

    async def get_trade_feed(
        self,
        filter_by: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict]:
        """Get social trade feed"""
        feed = self.trade_feed

        if filter_by == "following":
            # Filter to only show trades from followed traders
            # (Implementation needed)
            pass
        elif filter_by == "top_traders":
            # Filter to only top traders
            top_trader_ids = {t.id for t in await self.get_leaderboard()[:10]}
            feed = [t for t in feed if t.get("trader_id") in top_trader_ids]

        return feed[:limit]

    async def _update_trade_feed(
        self,
        trader_id: str,
        trade: Dict,
        copies_count: int
    ) -> None:
        """Update global trade feed"""
        trader = self.traders[trader_id]

        feed_item = {
            "id": f"{trader_id}:{trade.get('id')}",
            "trader_id": trader_id,
            "trader_name": trader.display_name,
            "trader_avatar": trader.avatar_url,
            "trade": trade,
            "copies_executed": copies_count,
            "timestamp": datetime.now(),
            "reactions": {"likes": 0, "comments": []},
            "verified": trader.verified
        }

        # Add to feed (keep last 1000)
        self.trade_feed.insert(0, feed_item)
        self.trade_feed = self.trade_feed[:1000]

    async def _update_trader_performance(
        self,
        trader_id: str,
        trade: Dict
    ) -> None:
        """Update trader performance metrics"""
        trader = self.traders[trader_id]

        # Update trade count
        trader.trades_count += 1

        # Update win rate (simplified)
        if trade.get("profit", 0) > 0:
            trader.win_rate = (trader.win_rate * (trader.trades_count - 1) + 1) / trader.trades_count
        else:
            trader.win_rate = (trader.win_rate * (trader.trades_count - 1)) / trader.trades_count

        # Update performance history
        trader.performance_history.append({
            "date": datetime.now(),
            "return": trade.get("profit", 0),
            "balance": trade.get("balance", 0)
        })

        # Update badges
        await self._update_badges(trader)

    async def _update_badges(self, trader: Trader) -> None:
        """Award badges based on achievements"""
        badges = []

        if trader.total_return_pct > 100:
            badges.append("🏆 Century Club")
        if trader.win_rate > 0.7:
            badges.append("🎯 Sharpshooter")
        if trader.followers > 1000:
            badges.append("👥 Influencer")
        if trader.sharpe_ratio > 2.0:
            badges.append("📊 Risk Master")
        if trader.trades_count > 1000:
            badges.append("💎 Veteran")

        trader.badges = badges

    async def _update_rankings(self, trader: Trader) -> None:
        """Update trader rankings"""
        # Calculate percentile rankings
        all_traders = list(self.traders.values())

        returns_rank = self._calculate_percentile(
            trader.total_return_pct,
            [t.total_return_pct for t in all_traders]
        )

        # Store rankings (would update database)
        log.info(f"Updated rankings for {trader.username}: {returns_rank}th percentile")

    def _calculate_percentile(self, value: float, all_values: List[float]) -> int:
        """Calculate percentile ranking"""
        if not all_values:
            return 50

        below = sum(1 for v in all_values if v < value)
        return int((below / len(all_values)) * 100)

    async def _process_subscription_payment(
        self,
        follower_id: str,
        amount: float
    ) -> bool:
        """Process subscription payment"""
        # Would integrate with payment processor
        log.info(f"Processed payment of ${amount} from {follower_id}")
        return True

    async def _send_notification(
        self,
        user_id: str,
        title: str,
        message: str
    ) -> None:
        """Send push notification"""
        log.info(f"Notification to {user_id}: {title}")


class CommunityIntelligence:
    """
    Crowdsourced trading intelligence.
    Aggregates wisdom of the crowd.
    """

    def __init__(self):
        self.signals: List[Dict] = []
        self.sentiment_votes: Dict[str, Dict] = {}
        self.discussions: Dict[str, List[Dict]] = {}

    async def submit_signal(
        self,
        user_id: str,
        signal: Dict
    ) -> str:
        """Submit community signal"""
        signal_id = f"sig_{len(self.signals)}"

        community_signal = {
            "id": signal_id,
            "user_id": user_id,
            "signal": signal,
            "votes": {"bull": 0, "bear": 0},
            "confidence": 0.0,
            "timestamp": datetime.now()
        }

        self.signals.append(community_signal)

        return signal_id

    async def vote_on_signal(
        self,
        signal_id: str,
        user_id: str,
        vote: str  # "bull" or "bear"
    ) -> None:
        """Vote on community signal"""
        for signal in self.signals:
            if signal["id"] == signal_id:
                signal["votes"][vote] += 1

                # Recalculate confidence
                total_votes = signal["votes"]["bull"] + signal["votes"]["bear"]
                if total_votes > 0:
                    signal["confidence"] = signal["votes"]["bull"] / total_votes

                break

    async def get_consensus(
        self,
        symbol: str
    ) -> Dict:
        """Get community consensus on symbol"""
        symbol_signals = [s for s in self.signals if s["signal"].get("symbol") == symbol]

        if not symbol_signals:
            return {"consensus": "neutral", "confidence": 0.0}

        # Aggregate votes
        total_bull = sum(s["votes"]["bull"] for s in symbol_signals)
        total_bear = sum(s["votes"]["bear"] for s in symbol_signals)

        if total_bull + total_bear == 0:
            return {"consensus": "neutral", "confidence": 0.0}

        confidence = total_bull / (total_bull + total_bear)

        if confidence > 0.6:
            consensus = "bullish"
        elif confidence < 0.4:
            consensus = "bearish"
        else:
            consensus = "neutral"

        return {
            "consensus": consensus,
            "confidence": confidence,
            "total_signals": len(symbol_signals),
            "bull_votes": total_bull,
            "bear_votes": total_bear
        }


# Export main components
__all__ = [
    "Trader",
    "TradeCopy",
    "SocialTradingEngine",
    "CommunityIntelligence"
]