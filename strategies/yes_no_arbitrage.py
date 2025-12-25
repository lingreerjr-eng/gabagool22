"""
YES/NO Arbitrage Strategy for Polymarket

A market-neutral strategy that buys YES and NO shares simultaneously when
their combined price is below $1.00, capturing deterministic spread at settlement.

This strategy targets:
- Short-expiry crypto markets (15-min style)
- Binary markets with YES/NO tokens
- Opportunities where yes_ask + no_ask < MAX_SUM_PRICE
"""
import asyncio
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
import logging
import aiohttp

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from strategies.fee_model import FeeModel
from strategies.market_filter import MarketFilter, MarketInfo
from strategies.hedge_manager import (
    HedgeManager, 
    LegInfo, 
    FillStatus, 
    BoxTradeResult,
    HedgeMode
)


@dataclass
class ArbConfig:
    """Configuration for YES/NO arbitrage strategy."""
    max_sum_price: float = 0.998      # Arbitrage trigger
    min_edge: float = 0.003           # Rebalanced: 0.3% target for anti-noise buffer
    trade_notional_usdc: float = 10.0 # USDC per trade
    leg_slippage_buffer: float = 0.002  # 0.2% buffer on limit prices
    min_liquidity: float = 25.0       # Minimum $ liquidity at best ask
    hedge_timeout_ms: int = 750       # Wait time for fill confirmation
    max_position_per_market: float = 200.0  # Max position size per market token
    poll_interval_seconds: float = 0.5  # Main loop poll interval
    max_concurrent_trades: int = 3    # Maximum simultaneous open positions
    max_hedge_hold_seconds: int = 300  # Gabagool-Optimized: 5 minutes hold time
    max_leg1_price: float = 0.70      # Do not buy Leg 1 if it's more expensive than this
    min_leg1_price: float = 0.40      # Do not buy Leg 1 if it's cheaper than this (User protection)
    max_entry_sum: float = 1.01       # Maker Mode: Allow entry at parity since we buy at Bid
    min_profit_usdc: float = 0.10     # Gabagool-Optimized: Scalping floor
    max_price_drop_usdc: float = 0.20 # NEW: Anti-Noise protection ($0.20)
    min_time_remaining_seconds: int = 300 # Don't enter if market closes in < 5 mins
    max_time_remaining_seconds: int = 900 # NEW: Don't enter if market closes in > 15 mins (Too Early)
    use_dynamic_notional: bool = True # Auto-adjust trade size based on balance
    notional_percent: float = 0.10    # 10% of balance per trade
    dry_run_wallet_usdc: float = 150.0 # Gabagool-Optimized: $150 starting bankroll
    dry_run: bool = True              # Dry run mode - no real trades
    
    def __post_init__(self):
        if self.min_edge <= 0:
            raise ValueError("min_edge must be positive")
        if self.max_sum_price >= 1.0:
            raise ValueError("max_sum_price must be less than 1.0")
        
        # Safety: min_time_remaining must be at least as long as the hedge timeout + buffer
        # This prevents entering a trade only to have the market close during our hedge hold.
        min_required = self.max_hedge_hold_seconds + 60
        if self.min_time_remaining_seconds < min_required:
            self.min_time_remaining_seconds = min_required


@dataclass
class OrderBookSnapshot:
    """Snapshot of an order book for one token."""
    token_id: str
    best_ask_price: float = 0.0
    best_ask_size: float = 0.0
    best_bid_price: float = 0.0
    best_bid_size: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def has_liquidity(self) -> bool:
        return self.best_ask_price > 0 and self.best_ask_size > 0


@dataclass
class Position:
    """Tracks a position in a market."""
    market_slug: str
    token_id: str
    side: str  # 'YES' or 'NO'
    size: float
    avg_price: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class YesNoArbStrategy:
    """
    Market-neutral YES+NO arbitrage strategy.
    
    Monitors crypto markets for arbitrage opportunities where:
    - yes_best_ask + no_best_ask < MAX_SUM_PRICE
    - Edge after fees >= MIN_EDGE
    - Sufficient liquidity at best ask
    
    Places both legs as close together as possible using batch orders
    when available, otherwise sequential with hedge protection.
    """
    
    def __init__(
        self,
        clob_client: ClobClient,
        market_filter: MarketFilter,
        fee_model: FeeModel,
        hedge_manager: HedgeManager,
        config: ArbConfig,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize the arbitrage strategy.
        
        Args:
            clob_client: Polymarket CLOB client
            market_filter: Market discovery and filtering
            fee_model: Fee and edge calculations
            hedge_manager: Fill confirmation and hedging
            config: Strategy configuration
            logger: Logger instance
        """
        self.client = clob_client
        self.market_filter = market_filter
        self.fee_model = fee_model
        self.hedge_manager = hedge_manager
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        
        self._running = False
        self._positions: Dict[str, Position] = {}  # token_id -> Position
        self._cooldowns: Dict[str, float] = {}  # market_slug -> cooldown until timestamp
        self._session: Optional[aiohttp.ClientSession] = None
        self._trade_count = 0
        self._wins = 0
        self._losses = 0
        self._total_pnl = 0.0
        self._start_time = datetime.now()
        
        # Balance Tracking
        self._initial_balance = self.config.dry_run_wallet_usdc if self.config.dry_run else 0.0
        self._current_balance = self._initial_balance
        self._is_balance_initialized = False # Flag for live balance fetching
    
    async def run(self):
        """Main strategy loop."""
        self._running = True
        self.logger.info(f"🚀 Starting YES/NO Arbitrage Strategy")
        self.logger.info(f"   Config: dry_run={self.config.dry_run}, max_sum={self.config.max_sum_price}")
        self.logger.info(f"   Min edge={self.config.min_edge}, notional=${self.config.trade_notional_usdc}")
        
        try:
            while self._running or self._positions:
                if not self._running and self._positions:
                    self.logger.info(f"⏳ SHUTDOWN: Waiting for {len(self._positions)} open positions to close...")

                try:
                    await self._run_iteration()
                    
                    # Faster polling during shutdown to finish quickly
                    interval = self.config.poll_interval_seconds if self._running else 1.0
                    jitter = random.uniform(0.8, 1.2)
                    await asyncio.sleep(interval * jitter)
                    
                except asyncio.CancelledError:
                    if self._positions:
                        self.logger.warning("⚠️ Forced cancellation while positions still open!")
                    break
                except Exception as e:
                    self.logger.error(f"❌ Strategy loop error: {e}", exc_info=True)
                    await asyncio.sleep(1)  # Backoff on error
                    
        finally:
            await self._cleanup()
    
    def stop(self):
        """Signal the strategy to stop gracefully."""
        self.logger.info("🛑 Graceful shutdown requested. Finishing current trades...")
        self._running = False

    async def _run_iteration(self):
        """Single iteration of the strategy loop."""
        # Get target markets
        markets = await self.market_filter.get_target_markets()
        
        if not markets:
            return
        
        # Check each market for opportunities
        for market in markets:
            if self._is_on_cooldown(market.slug):
                continue
                
            try:
                await self._check_and_execute(market)
            except Exception as e:
                # Catch PolyApiException or string check for 404
                if "404" in str(e) or "No orderbook exists" in str(e):
                    self.logger.warning(f"⚠️ Market {market.slug} tokens not found on CLOB (404). Cooling down for 10m.")
                    self._set_cooldown(market.slug, 600) # 10 minute cooldown
                else:
                    self.logger.error(f"Error checking market {market.slug}: {e}")
                continue
    
    async def _check_and_execute(self, market: MarketInfo):
        """
        Check legged arbitrage conditions and execute.
        
        Strategy:
        1. LEG 1: If we have room in YES position, BUY YES immediately (Market/Aggressive).
        2. LEG 2: If we have YES position, check NO price. If YES_Entry + NO_Ask < 1.0, BUY NO.
        3. EXIT: If position held too long without hedge, SELL YES (Stop Loss).
        """
        # Get order books for both tokens
        yes_book = await self._get_order_book(market.yes_token_id)
        no_book = await self._get_order_book(market.no_token_id)
        
        if not yes_book or not no_book:
            return
        
        # Current Positions
        yes_size = self._get_position_size(market.yes_token_id)
        no_size = self._get_position_size(market.no_token_id)
        
        # --- LEG 1 LOGIC: "Always Buy YES" ---
        # "One Shot" Entry: Only buy if we have essentially 0 position.
        
        if yes_size < 1.0:
            # STOP ENTRY IF SHUTTING DOWN
            if not self._running:
                return
                
            # --- SMART CAPITAL MANAGEMENT ---
            available_usdc = await self._get_available_balance()
            current_notional = await self._get_current_notional()
            
            # We reserve 2.5x notional to ensure we can always finish a hedge (Leg 2)
            required_reservation = current_notional * 2.5 
            
            if available_usdc < required_reservation:
                 self.logger.warning(
                     f"⏸️ Insufficient Funds: Available ${available_usdc:.2f} < Required ${required_reservation:.2f}. "
                     f"Skipping new entries to protect Leg 2 reserves."
                 )
                 return

            # --- NEW SMART ENTRY FILTERS ---
            entry_sum = yes_book.best_ask_price + no_book.best_ask_price
                
            # Filter 1: Is this market near parity? (entry_sum < 1.01)
            if entry_sum > self.config.max_entry_sum:
                if random.random() < 0.1: 
                    self.logger.debug(f"⏭️ Skipping {market.slug}: Market too expensive (Sum: ${entry_sum:.3f})")
                return
            
            # Filter 2: Is YES price acceptable? (0.40 < YES < 0.70)
            if yes_book.best_ask_price > self.config.max_leg1_price:
                self.logger.debug(f"⏭️ Skipping {market.slug}: YES price too high (${yes_book.best_ask_price:.3f} > ${self.config.max_leg1_price:.3f})")
                return
            
            if yes_book.best_ask_price < self.config.min_leg1_price:
                self.logger.info(f"⏭️ Skipping {market.slug}: YES price too low (${yes_book.best_ask_price:.3f} < ${self.config.min_leg1_price:.3f})")
                return

            # Filter 3: Time Remaining (Don't buy into a dying market)
            time_left = (market.close_time - datetime.now(timezone.utc)).total_seconds()
            if time_left < self.config.min_time_remaining_seconds:
                self.logger.info(f"⏭️ Skipping {market.slug}: Only {time_left:.0f}s left (Min: {self.config.min_time_remaining_seconds}s)")
                return

            # Filter 3b: Too Early (Don't jump into next hour's market)
            if time_left > self.config.max_time_remaining_seconds:
                if random.random() < 0.01:
                    self.logger.debug(f"⏭️ Skipping {market.slug}: Too early ({time_left/60:.1f}m left)")
                return

            # Filter 4: Spread Quality (Skip if spread is > 15% of price)
            yes_spread = yes_book.best_ask_price - yes_book.best_bid_price
            if yes_spread / yes_book.best_ask_price > 0.15:
                self.logger.warning(f"⏭️ Skipping {market.slug}: Spread too wide ({yes_spread/yes_book.best_ask_price:.1%})")
                return

            # Filter 5: Dual-Leg Liquidity (SHARK MODE MUST FILL BOTH)
            # Both YES and NO must have enough liquidity for at least our notional
            min_size = (current_notional / 0.5) # Estimate size for a $~0.50 token
            if yes_book.best_ask_size < min_size or no_book.best_ask_size < min_size:
                return

            # Calculate actual size to buy based on notional, but capped by liquidity
            limit_price_yes = yes_book.best_ask_price * (1 + self.config.leg_slippage_buffer)
            size_to_buy = min(current_notional / limit_price_yes, yes_book.best_ask_size * 0.95, no_book.best_ask_size * 0.95)
            
            if size_to_buy < 1.0:
                return

            # We have room AND market looks good for an arb!
            self.logger.info(f"👉 Analysing {market.slug}: {market.question} | Size: {size_to_buy:.2f}")
            await self._execute_leg_1_yes(market, yes_book, size_to_buy)

        # --- LEG 2 LOGIC: "Close with NO" ---
        # Check if we have an unhedged YES position.
        unhedged_yes = yes_size - no_size
        
        if unhedged_yes > 1.0: # Tolerance for dust
            pos = self._positions.get(market.yes_token_id)
            if not pos: return

            # Calculate metrics
            avg_entry = pos.avg_price
            current_sum = avg_entry + no_book.best_ask_price
            current_edge = 1.0 - current_sum
            
            # Identify current duration
            now = datetime.now(timezone.utc)
            duration = (now - pos.opened_at).total_seconds()
            
            # Direct Profit Check (Can we sell YES for profit?)
            # Solo Exit Floor: $0.50 minimum gain (5% on $10 trade)
            price_inc = yes_book.best_bid_price - avg_entry
            est_profit = price_inc * unhedged_yes
            
            if price_inc > (self.config.min_edge * 4.0) and est_profit >= self.config.min_profit_usdc:
                 await self._execute_exit_sell_yes(market, yes_book, unhedged_yes, reason="PROFIT_EXIT")
                 return
            
            # PRICE STOP LOSS: Cut losses early if price drops too much
            # GRACE PERIOD: Don't panic sell in the first 10 seconds (let hedge finish)
            if duration > 10.0 and price_inc <= -self.config.max_price_drop_usdc:
                await self._execute_exit_sell_yes(market, yes_book, unhedged_yes, reason="PRICE_STOP_LOSS")
                return

            # Monitor Log
            remaining_time = self.config.max_hedge_hold_seconds - duration
            stop_distance = price_inc - (-self.config.max_price_drop_usdc)
            if self.config.dry_run:
                self.logger.info(
                    f"👀 Monitoring {market.slug} ({market.question[:40]}...)\n"
                    f"   Holding YES ({unhedged_yes:.0f} @ ${avg_entry:.3f}) for {duration:.0f}s (Timeout in {remaining_time:.0f}s)\n"
                    f"   Target NO < ${1.00 - avg_entry - self.config.min_edge:.3f} (Curr: ${no_book.best_ask_price:.3f})\n"
                    f"   Price: ${yes_book.best_bid_price:.3f} ({price_inc:+.3f}) | Stop Dist: ${stop_distance:.3f}\n"
                    f"   Sum=${current_sum:.3f} Edge={current_edge*100:.2f}%"
                )
            
            # CHECK PROFIT EXIT (Leg 2 Hedge)
            # IMPORTANT: For Hedged Arbitrage, we ignore the $1.00 floor.
            # If we can lock in ANY profit (sum < 1.0), we take it.
            if current_sum <= 1.0 - self.config.min_edge:
                await self._execute_leg_2_no(market, no_book, unhedged_yes, avg_entry)
                return

            # EMERGENCY BREAK-EVEN HEDGE
            # If we are 70% through our timeout, accept any hedge <= $1.002 to break even
            if duration > (self.config.max_hedge_hold_seconds * 0.7):
                if current_sum <= 1.002: # Allow tiny slippage for break-even
                    self.logger.warning(f"🚨 EMERGENCY HEDGE: Timeout imminent ({duration:.0f}s). Hedging at break-even (Sum: ${current_sum:.3f})")
                    await self._execute_leg_2_no(market, no_book, unhedged_yes, avg_entry)
                    return

            # CHECK TIMEOUT STOP LOSS
            if duration > self.config.max_hedge_hold_seconds:
                await self._execute_exit_sell_yes(market, yes_book, unhedged_yes, reason="TIMEOUT_STOP_LOSS")

    async def _execute_exit_sell_yes(self, market: MarketInfo, yes_book: OrderBookSnapshot, size: float, reason: str = "EXIT"):
        """Execute Exit: Sell YES back to market."""
        # Sell at Bid
        best_bid = yes_book.best_bid_price
        if best_bid <= 0:
            self.logger.error(f"Cannot sell YES for {market.slug}, no bids!")
            return

        pos = self._positions.get(market.yes_token_id)
        avg_price = pos.avg_price if pos else 0
        est_pnl = (best_bid - avg_price) * size

        if self.config.dry_run:
             self.logger.info(
                 f"🔻 [DRY RUN] {reason}: Selling {size:.2f} YES @ ${best_bid:.3f} on {market.slug}\n"
                 f"   (Entry: ${avg_price:.3f} | Est PnL: ${est_pnl:.4f})"
             )
             # Track PnL for summary
             self._total_pnl += est_pnl
             self._current_balance += est_pnl  # Compounding dry run balance
             self._trade_count += 1
             if est_pnl > 0: self._wins += 1
             else: self._losses += 1

             # Remove false position
             if market.yes_token_id in self._positions:
                 del self._positions[market.yes_token_id]
             
             # Longer cooldown for timeouts to avoid re-entering bad markets
             cooldown = 120 if "TIMEOUT" in reason else 30
             self._set_cooldown(market.slug, cooldown)
             return

        # LIVE TRADE
        self.logger.info(f"🔻 EXECUTING {reason}: Sell {size:.2f} YES @ ${best_bid:.3f}")
        leg_info = await self._place_single_order(
            market.yes_token_id,
            best_bid, # Limit price = current bid
            size,
            reason,
            side=SELL
        )
        
        if leg_info and leg_info.filled_size > 0:
            actual_pnl = (leg_info.filled_price - avg_price) * leg_info.filled_size
            self.logger.info(
                f"✅ {reason} FILLED: Sold {leg_info.filled_size:.2f} YES @ ${leg_info.filled_price:.4f}\n"
                f"   📈 Final PnL: ${actual_pnl:.4f}"
            )

            # Track PnL for summary
            self._total_pnl += actual_pnl
            if self.config.dry_run:
                self._current_balance += actual_pnl # Compound
            
            self._trade_count += 1
            if actual_pnl > 0: self._wins += 1
            else: self._losses += 1

            if market.yes_token_id in self._positions:
                 del self._positions[market.yes_token_id]
            
            cooldown = 180 if "TIMEOUT" in reason else 60
            self._set_cooldown(market.slug, cooldown)
        else:
            self.logger.warning(f"❌ {reason} Failed to fill")

    async def _execute_leg_1_yes(self, market: MarketInfo, yes_book: OrderBookSnapshot, size_to_buy: float):
        """Execute Leg 1: Buy YES using Maker Order (at Best Bid)."""
        # MAKER MODE: Join the Bid instead of hitting the Ask
        limit_price = yes_book.best_bid_price 
        if limit_price <= 0:
             self.logger.warning(f"⚠️ Limit price {limit_price} <= 0 for {market.slug}. Skipping Leg 1.")
             return

        self.logger.info(f"👉 [MAKER] Attempting Leg 1 on: {market.question} ({market.slug})")

        if self.config.dry_run:
             self.logger.info(
                f"🚀 [DRY RUN] LEG 1 (MAKER): Buying {size_to_buy:.2f} YES @ ${limit_price:.3f} (Best Bid)"
             )
             self._add_position(market.slug, market.yes_token_id, 'YES', size_to_buy, limit_price)
             self._set_cooldown(market.slug, 5)
             return

        # LIVE TRADE
        self.logger.info(f"🚀 EXECUTING LEG 1: Buy {size_to_buy:.2f} YES @ ${limit_price:.3f}")
        leg_info = await self._place_single_order(
            market.yes_token_id,
            limit_price,
            size_to_buy,
            "LEG1_YES",
            side=BUY
        )
        
        if leg_info and leg_info.filled_size > 0:
            self.logger.info(f"✅ LEG 1 FILLED: {leg_info.filled_size:.2f} YES @ ${leg_info.filled_price:.4f}")
            self._add_position(market.slug, market.yes_token_id, 'YES', leg_info.filled_size, leg_info.filled_price)
            self._set_cooldown(market.slug, 1)
        else:
            self.logger.warning("❌ Leg 1 Failed to fill or partially failed")
            self._set_cooldown(market.slug, 5)
            
    async def _execute_leg_2_no(self, market: MarketInfo, no_book: OrderBookSnapshot, size_needed: float, yes_entry: float):
        """Execute Leg 2: Buy NO to close arbitrage."""
        limit_price = no_book.best_ask_price * (1 + self.config.leg_slippage_buffer)
        
        # Don't buy more than available liquidity
        size = min(size_needed, no_book.best_ask_size)
        
        if size < 1:
            return

        if self.config.dry_run:
             profit = (1.0 - (yes_entry + no_book.best_ask_price)) * size
             self.logger.info(
                f"💰 [DRY RUN] LEG 2: Closing! Buying {size:.2f} NO @ ${no_book.best_ask_price:.3f}. "
                f"Locked Profit: ${profit:.2f}"
             )
             self._total_pnl += profit
             self._current_balance += profit # Compound
             self._trade_count += 1
             if profit > 0: self._wins += 1
             else: self._losses += 1

             # RELEASE FUNDS: The arbitrage is now locked.
             if market.yes_token_id in self._positions:
                 del self._positions[market.yes_token_id]
             
             self._set_cooldown(market.slug, 30) # Cool down after win
             return

        # LIVE TRADE
        self.logger.info(f"💰 EXECUTING LEG 2: Buy {size:.2f} NO @ ${limit_price:.3f} to close arb")
        leg_info = await self._place_single_order(
            market.no_token_id,
            limit_price,
            size,
            "LEG2_NO",
            side=BUY
        )
        
        if leg_info and leg_info.filled_size > 0:
            profit = (1.0 - (yes_entry + leg_info.filled_price)) * leg_info.filled_size
            self.logger.info(
                f"✅ LEG 2 FILLED: {leg_info.filled_size:.2f} NO @ ${leg_info.filled_price:.4f} | "
                f"🎉 ARB CLOSED! Profit: ${profit:.4f}"
            )
            # Track PnL for summary
            self._total_pnl += profit
            if self.config.dry_run:
                self._current_balance += profit # Compound
            
            self._trade_count += 1
            if profit > 0: self._wins += 1
            else: self._losses += 1

            # RELEASE FUNDS: Arbitrage locked.
            if market.yes_token_id in self._positions:
                del self._positions[market.yes_token_id]
            
            self._set_cooldown(market.slug, 30)
        else:
            self.logger.warning("❌ Leg 2 Failed to fill")
    
    async def _place_single_order(
        self,
        token_id: str,
        limit_price: float,
        size: float,
        label: str,
        side: str # Added side parameter
    ) -> Optional[LegInfo]:
        """Place a single order and return fill info."""
        if self.config.dry_run:
            return LegInfo(
                order_id=f"sim_{random.randint(1000, 9999)}",
                token_id=token_id,
                side=side,
                intended_price=limit_price,
                intended_size=size,
                filled_price=limit_price,
                filled_size=size,
                status=FillStatus.FILLED
            )

        try:
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(size * limit_price),
                side=side, # Use the passed side parameter
            )
            signed_order = self.client.create_market_order(order_args)
            response = self.client.post_order(signed_order, OrderType.FOK)
            
            if response.get('success'):
                data = response.get('data', {})
                return LegInfo(
                    order_id=data.get('orderID', ''),
                    token_id=token_id,
                    side=side, # Use the passed side parameter
                    intended_price=limit_price,
                    intended_size=size,
                    filled_price=float(data.get('avgPrice', limit_price)),
                    filled_size=float(data.get('filledAmount', size)),
                    status=FillStatus.FILLED
                )
            else:
                self.logger.warning(f"{label} order failed: {response.get('error', 'Unknown')}")
                return LegInfo(
                    order_id='',
                    token_id=token_id,
                    side=side, # Use the passed side parameter
                    intended_price=limit_price,
                    intended_size=size,
                    status=FillStatus.FAILED
                )
                
        except Exception as e:
            self.logger.error(f"Error placing {label} order: {e}")
            return None
    
    async def _get_order_book(self, token_id: str) -> Optional[OrderBookSnapshot]:
        """Get current order book for a token."""
        try:
            book = self.client.get_order_book(token_id)
            
            if not book:
                return None
            
            best_ask_price = 0.0
            best_ask_size = 0.0
            best_bid_price = 0.0
            best_bid_size = 0.0
            
            if book.asks:
                # Asks are sorted, lowest ask first (or use [-1] if sorted desc)
                best_ask = book.asks[0] if book.asks[0].price < book.asks[-1].price else book.asks[-1]
                best_ask_price = float(best_ask.price)
                best_ask_size = float(best_ask.size)
            
            if book.bids:
                # Bids are sorted, highest bid first (or use [-1] if sorted asc)
                best_bid = book.bids[0] if book.bids[0].price > book.bids[-1].price else book.bids[-1]
                best_bid_price = float(best_bid.price)
                best_bid_size = float(best_bid.size)
            
            return OrderBookSnapshot(
                token_id=token_id,
                best_ask_price=best_ask_price,
                best_ask_size=best_ask_size,
                best_bid_price=best_bid_price,
                best_bid_size=best_bid_size
            )
            
        except Exception as e:
            # Let the caller handle the error level to avoid duplicate spam
            raise e
    
    def _get_position_size(self, token_id: str) -> float:
        """Get current position size for a token."""
        pos = self._positions.get(token_id)
        return pos.size if pos else 0.0
    
    def _add_position(self, market_slug: str, token_id: str, side: str, size: float, price: float):
        """Add or update a position."""
        current_pos = self._positions.get(token_id)
        
        if current_pos:
            new_size = current_pos.size + size
            if new_size > 0:
                new_price = ((current_pos.size * current_pos.avg_price) + (size * price)) / new_size
            else:
                new_price = 0.0
            
            # Update existing position, PRESERVING opened_at
            current_pos.size = new_size
            current_pos.avg_price = new_price
            # Market/Token/Side remain same
            
            self.logger.debug(f"📈 Updated Position {market_slug}: {new_size:.2f} {side} @ ${new_price:.4f}")
        else:
            self._positions[token_id] = Position(
                market_slug=market_slug,
                token_id=token_id,
                side=side,
                size=size,
                avg_price=price
            )
    
    def _is_on_cooldown(self, market_slug: str) -> bool:
        """Check if market is on cooldown."""
        cooldown_until = self._cooldowns.get(market_slug, 0)
        return asyncio.get_event_loop().time() < cooldown_until
    
    def _set_cooldown(self, market_slug: str, seconds: float):
        """Set cooldown for a market."""
        self._cooldowns[market_slug] = asyncio.get_event_loop().time() + seconds
    
    async def _get_available_balance(self) -> float:
        """Get the current spendable balance (Total - Committed)."""
        if self.config.dry_run:
            # In dry run, committed funds are the notional value of all open tokens
            committed = len(self._positions) * (await self._get_current_notional())
            return self._current_balance - committed
        
        # LIVE BALANCE CHECK
        try:
            # We fetch from Polymarket CLOB
            # Note: We track committed locally to avoid waiting for API settlement
            resp = self.client.get_balance() 
            # Assuming resp is a float or has balance field, adjust based on library docs
            balance = float(resp) if isinstance(resp, (float, int)) else 0.0
            
            committed = len(self._positions) * (await self._get_current_notional())
            return balance - committed
        except Exception as e:
            self.logger.error(f"Error fetching balance: {e}")
            return 0.0

    async def _get_current_total_balance(self) -> float:
        """Get total balance (Cash + Committed)."""
        if self.config.dry_run:
            return self._current_balance
        
        try:
            resp = self.client.get_balance()
            return float(resp) if isinstance(resp, (float, int)) else 0.0
        except:
            return 0.0

    async def _get_current_notional(self) -> float:
        """Calculate dynamic or fixed notional."""
        if not self.config.use_dynamic_notional:
            return self.config.trade_notional_usdc
        
        balance = await self._get_current_total_balance()
        dynamic = balance * self.config.notional_percent
        
        # Guardrails (Polymarket min is ~$5, Max safety cap)
        return max(min(dynamic, 100.0), 5.0)

    async def _cleanup(self):
        """Cleanup resources on shutdown and print final report."""
        self.logger.info("🔄 Cleaning up strategy resources...")
        
        await self.market_filter.close()
        
        # FINAL PNL REPORT
        duration = datetime.now() - self._start_time
        win_rate = (self._wins / self._trade_count * 100) if self._trade_count > 0 else 0
        
        report = [
            "\n" + "="*50,
            "📊 FINAL SESSION REPORT " + ("(DRY RUN)" if self.config.dry_run else "(LIVE)"),
            "="*50,
            f"⏱️  Duration:      {str(duration).split('.')[0]}",
            f"🔄 Total Trades:   {self._trade_count}",
            f"✅ Wins:           {self._wins}",
            f"❌ Losses:         {self._losses}",
            f"📈 Win Rate:       {win_rate:.1f}%",
            f"💵 Net PnL:        ${self._total_pnl:.4f}",
            "="*50 + "\n"
        ]
        
        for line in report:
            self.logger.info(line)
    
    def stop(self):
        """Signal the strategy to stop."""
        self._running = False
        self.logger.info("🛑 Strategy stop requested")
