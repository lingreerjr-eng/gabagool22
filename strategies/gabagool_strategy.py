import asyncio
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from strategies.yes_no_arbitrage import YesNoArbStrategy, ArbConfig, Position, LegInfo, OrderBookSnapshot
from strategies.market_filter import MarketInfo
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import OrderType

@dataclass
class GabagoolConfig(ArbConfig):
    """Specialized config for Gabagool22 high-frequency strategy."""
    layers: int = 3                   # Number of entry layers
    layer_notional_percent: float = 0.33 # Each layer is 1/3rd of total notional
    min_price_improvement: float = 0.01   # Only add layer if price improves by $0.01
    auto_redeem_buffer_seconds: int = 120 # Sell winners 2 mins before close to recycle cash

class Gabagool22Strategy(YesNoArbStrategy):
    """
    Gabagool22 Strategy:
    1. Dual-Side Entry: Detect cheapest side (YES or NO) to start.
    2. Layering: Buy in small chunks to get better averages.
    3. Box Closing: Hunter-mode to close the other side for profit.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = GabagoolConfig(**self.config.__dict__)
        self.logger.info("👨‍🍳 Gabagool22 Strategy Initialized: Layering & Dual-Side active.")

    async def _check_and_execute(self, market: MarketInfo):
        """Dual-Side detection and execution."""
        yes_book = await self._get_order_book(market.yes_token_id)
        no_book = await self._get_order_book(market.no_token_id)
        if not (yes_book and no_book): return

        # Log active monitoring for this specific market
        if self._trade_count % 10 == 0: # Throttle logs
             self.logger.info(f"🔍 Monitoring: {market.question} ({market.slug})")

        yes_size = self._get_position_size(market.yes_token_id)
        no_size = self._get_position_size(market.no_token_id)

        # 1. SHUTDOWN CHECK
        if not self._running and (yes_size < 1.0 and no_size < 1.0):
            return

        # 2. HEDGE / BOX CLOSING (Existing Logic is good, but we prioritize it)
        if (yes_size > 1.0 and no_size < 1.0) or (no_size > 1.0 and yes_size < 1.0):
            await self._run_hedge_logic(market, yes_book, no_book, yes_size, no_size)
            return

        # 3. ENTRY LOGIC (Cheapest Side Detection)
        if yes_size < 1.0 and no_size < 1.0 and self._running:
            await self._run_dual_entry_logic(market, yes_book, no_book)

        # 4. AUTO-REDEEM (Sell Winners before close to recycle cash)
        await self._check_auto_redeem(market, yes_book, no_book, yes_size, no_size)

        # 5. MONITORING (Logs positions and stop losses)
        await self._run_monitor_logic(market, yes_book, no_book, yes_size, no_size)

    async def _run_dual_entry_logic(self, market: MarketInfo, yes_book: OrderBookSnapshot, no_book: OrderBookSnapshot):
        """Buy whichever side is 'cheaper' relative to its bid-ask spread."""
        # Calculate 'Cheapness' (higher spread intensity usually precedes a bounce/fill)
        yes_entry = yes_book.best_bid_price
        no_entry = no_book.best_bid_price

        # Gabagool Pattern: Buy the side that is closer to its floor or has higher volume
        # Simplified for now: monitor both, buy whatever hits Maker first.
        # We start with YES if they are equal, standardizing on the trader's favorite asset first.
        
        # Check liquidity
        current_notional = await self._get_current_notional()
        layer_size_usdc = current_notional * self.config.layer_notional_percent
        
        # Prioritize YES but allow NO start if significantly cheaper
        if yes_entry <= no_entry:
            await self._execute_leg_1_maker(market, market.yes_token_id, yes_entry, layer_size_usdc, "YES")
        else:
            await self._execute_leg_1_maker(market, market.no_token_id, no_entry, layer_size_usdc, "NO")

    async def _execute_leg_1_maker(self, market: MarketInfo, token_id: str, bid_price: float, notional: float, side_name: str):
        """Place a small Maker layer."""
        size = notional / bid_price
        if size < 1.0: return

        self.logger.info(f"🎣 Gabagool Layer: Buy {side_name} @ ${bid_price:.3f} on '{market.question}'")
        leg_info = await self._place_single_order(token_id, bid_price, size, f"GABAGOOL_L1_{side_name}", BUY)
        
        # Merge Position logic
        if leg_info and leg_info.filled_size > 0:
            existing = self._positions.get(token_id)
            if existing:
                # Weighted Average Merge
                new_total_size = existing.size + leg_info.filled_size
                avg_price = ((existing.avg_price * existing.size) + (leg_info.filled_price * leg_info.filled_size)) / new_total_size
                existing.size = new_total_size
                existing.avg_price = avg_price
                self.logger.info(f"🧱 MERGED: New Average Price for {side_name}: ${avg_price:.4f} (Total Size: {new_total_size:.2f})")
            else:
                self._positions[token_id] = Position(
                    market_slug=market.slug,
                    token_id=token_id,
                    side=side_name,
                    avg_price=leg_info.filled_price,
                    size=leg_info.filled_size,
                    opened_at=datetime.now(timezone.utc)
                )

    async def _run_hedge_logic(self, market: MarketInfo, yes_book: OrderBookSnapshot, no_book: OrderBookSnapshot, yes_size: float, no_size: float):
        """Hunter-mode: close the other side to complete the Arb Box."""
        # This uses the base class logic but is prioritized during shutdown
        if yes_size > 1.0:
            # We have YES, hunt NO
            pos = self._positions.get(market.yes_token_id)
            current_sum = pos.avg_price + no_book.best_ask_price
            if current_sum <= 1.0 - self.config.min_edge:
                await self._execute_leg_2_no(market, no_book, yes_size, pos.avg_price)
        else:
            # We have NO, hunt YES
            pos = self._positions.get(market.no_token_id)
            current_sum = pos.avg_price + yes_book.best_ask_price
            if current_sum <= 1.0 - self.config.min_edge:
                # Custom Leg 2 YES closing logic
                self.logger.info(f"🎁 BOX COMPLETE: Sum=${current_sum:.3f} | Buying YES to lock profit")
                await self._execute_leg_2_yes(market, yes_book, no_size, pos.avg_price)

    async def _run_monitor_logic(self, market: MarketInfo, yes_book: OrderBookSnapshot, no_book: OrderBookSnapshot, yes_size: float, no_size: float):
        """Standard monitoring with layering logic."""
        # Logs the specific market name for better tracking
        if yes_size > 1.0 or no_size > 1.0:
            target_side = "YES" if yes_size > 1.0 else "NO"
            pos = self._positions.get(market.yes_token_id if yes_size > 1.0 else market.no_token_id)
            if pos:
                self.logger.info(f"📊 Tracking '{market.question}': {pos.size:.2f} {target_side} @ ${pos.avg_price:.3f}")

    async def _check_auto_redeem(self, market: MarketInfo, yes_book: OrderBookSnapshot, no_book: OrderBookSnapshot, yes_size: float, no_size: float):
        """
        'Claim Winnings' Hack: If market is about to close and we are winning, 
        sell at $0.99 to get cash back immediately.
        """
        now = datetime.now(timezone.utc).timestamp()
        end_time = market.close_time.timestamp()
        time_left = end_time - now
        
        if 30 < time_left < self.config.auto_redeem_buffer_seconds:
            # Sliding Scale Logic
            # 2m left -> 0.98, 1m left -> 0.95, 30s left -> 0.90
            threshold = 0.98
            if time_left < 60: threshold = 0.95
            if time_left < 30: threshold = 0.90
            
            # Check for winners
            if yes_size > 1.0 and yes_book.best_bid_price >= threshold:
                self.logger.info(f"💰 SLIDING REDEEM ({time_left:.0f}s left): Selling YES winner on '{market.question}' early @ ${yes_book.best_bid_price:.3f} to recycle capital.")
                await self._execute_exit_sell_yes(market, yes_book, yes_size, "AUTO_REDEEM")
            elif no_size > 1.0 and no_book.best_bid_price >= threshold:
                self.logger.info(f"💰 SLIDING REDEEM ({time_left:.0f}s left): Selling NO winner on '{market.question}' early @ ${no_book.best_bid_price:.3f} to recycle capital.")
                # We reuse the sell logic but for NO side
                await self._execute_leg_2_no(market, no_book, no_size, 0) # price=0 so it treats it as 100% winning
                if market.no_token_id in self._positions: del self._positions[market.no_token_id]

    async def _cleanup_zombie_positions(self):
        """Periodically clear positions from markets that have ended."""
        import time
        now = time.time()
        to_delete = []
        for tid, pos in self._positions.items():
            # We don't have the market object here, but we can check if it's very old
            # or if the main loop has stopped seeing it.
            # For now, we rely on the _check_and_execute loop to trigger _check_auto_redeem.
            pass

    async def _execute_leg_2_yes(self, market: MarketInfo, yes_book: OrderBookSnapshot, size_needed: float, no_entry: float):
        """Execute Leg 2: Buy YES to close arbitrage (when we started with NO)."""
        limit_price = yes_book.best_ask_price * (1 + self.config.leg_slippage_buffer)
        size = min(size_needed, yes_book.best_ask_size)
        if size < 1: return

        if self.config.dry_run:
            profit = (1.0 - (no_entry + yes_book.best_ask_price)) * size
            self.logger.info(f"💰 [DRY RUN] LEG 2: Closing YES! Sum=${no_entry + yes_book.best_ask_price:.3f} | Profit: ${profit:.2f}")
            self._total_pnl += profit
            self._current_balance += profit
            self._trade_count += 1
            if profit > 0: self._wins += 1
            else: self._losses += 1
            if market.no_token_id in self._positions: del self._positions[market.no_token_id]
            return

        # LIVE TRADE
        leg_info = await self._place_single_order(market.yes_token_id, limit_price, size, "LEG2_YES", side=BUY)
        if leg_info and leg_info.filled_size > 0:
            profit = (1.0 - (no_entry + leg_info.filled_price)) * leg_info.filled_size
            self.logger.info(f"✅ LEG 2 FILLED: {leg_info.filled_size:.2f} YES @ ${leg_info.filled_price:.4f} | 🎉 ARB CLOSED! Profit: ${profit:.4f}")
            self._total_pnl += profit
            self._trade_count += 1
            if profit > 0: self._wins += 1
            else: self._losses += 1
            if market.no_token_id in self._positions: del self._positions[market.no_token_id]
