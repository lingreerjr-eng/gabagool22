"""
Fee Model for YES/NO Arbitrage Strategy

Provides pluggable fee and slippage calculations for arbitrage edge computation.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class FeeModel:
    """
    Calculates transaction costs including fees and slippage.
    
    Attributes:
        taker_fee_bps: Taker fee in basis points (1 bps = 0.01%)
                       Polymarket currently has 0 taker fees
        slippage_bps: Estimated slippage in basis points
        min_edge_buffer_bps: Additional safety buffer in basis points
    """
    taker_fee_bps: float = 0.0      # Default 0 bps per Polymarket current policy
    slippage_bps: float = 20.0      # 0.2% default slippage estimate
    min_edge_buffer_bps: float = 10.0  # 0.1% safety buffer
    
    def __post_init__(self):
        """Validate fee parameters."""
        if self.taker_fee_bps < 0:
            raise ValueError("taker_fee_bps cannot be negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative")
    
    @property
    def total_cost_bps(self) -> float:
        """Total transaction cost in basis points (both legs)."""
        # Fees apply to both YES and NO legs
        return (self.taker_fee_bps * 2) + (self.slippage_bps * 2)
    
    @property
    def total_cost_decimal(self) -> float:
        """Total transaction cost as a decimal."""
        return self.total_cost_bps / 10000.0
    
    def calculate_total_cost(self, notional: float) -> float:
        """
        Calculate total fees + slippage for a notional trade size.
        
        Args:
            notional: Trade size in USDC
            
        Returns:
            Estimated total cost in USDC
        """
        return notional * self.total_cost_decimal
    
    def calculate_edge(self, sum_price: float) -> float:
        """
        Calculate net edge after fees and slippage.
        
        The raw edge is (1.0 - sum_price), representing the profit
        if you can buy both YES and NO for sum_price and receive $1 at settlement.
        
        Args:
            sum_price: Combined price of YES ask + NO ask
            
        Returns:
            Net edge after subtracting estimated costs
        """
        raw_edge = 1.0 - sum_price
        return raw_edge - self.total_cost_decimal
    
    def calculate_min_profitable_edge(self) -> float:
        """
        Calculate minimum edge required to be profitable.
        
        Returns:
            Minimum edge threshold including safety buffer
        """
        return self.total_cost_decimal + (self.min_edge_buffer_bps / 10000.0)
    
    def is_profitable(self, sum_price: float, min_edge: Optional[float] = None) -> bool:
        """
        Check if a trade at the given sum_price would be profitable.
        
        Args:
            sum_price: Combined YES + NO ask price
            min_edge: Override minimum edge requirement
            
        Returns:
            True if expected edge exceeds minimum threshold
        """
        edge = self.calculate_edge(sum_price)
        threshold = min_edge if min_edge is not None else self.calculate_min_profitable_edge()
        return edge >= threshold
    
    def estimate_pnl(self, sum_price: float, size: float) -> float:
        """
        Estimate profit/loss for a box trade.
        
        Args:
            sum_price: Combined entry price (YES + NO)
            size: Number of share pairs
            
        Returns:
            Estimated PnL in USDC (assuming $1 settlement)
        """
        edge = self.calculate_edge(sum_price)
        return edge * size
    
    def __repr__(self) -> str:
        return (
            f"FeeModel(taker={self.taker_fee_bps}bps, "
            f"slippage={self.slippage_bps}bps, "
            f"total_cost={self.total_cost_bps}bps)"
        )
