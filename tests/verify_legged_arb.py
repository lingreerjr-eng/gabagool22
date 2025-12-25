import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

# Add project root to path
sys.path.append('/Users/lindseygreer/Polymarket-spike-bot-v1')

from strategies.yes_no_arbitrage import YesNoArbStrategy, ArbConfig, Position, OrderBookSnapshot
from strategies.market_filter import MarketInfo

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Verification")

async def verify_logic():
    print("--- Starting Verification ---")
    
    # 1. Setup Mocks
    mock_client = MagicMock()
    mock_client.create_market_order = MagicMock()
    mock_client.post_order = MagicMock(return_value={'success': True, 'data': {'avgPrice': 0.5, 'filledAmount': 10}})
    
    # Mock Order Book
    async def mock_get_order_book(token_id):
        return OrderBookSnapshot(
            token_id=token_id,
            best_ask_price=0.60, # 60c ask
            best_ask_size=100,
            best_bid_price=0.40, # 40c bid
            best_bid_size=100
        )
    
    strategy = YesNoArbStrategy(
        clob_client=mock_client,
        market_filter=AsyncMock(),
        fee_model=MagicMock(),
        hedge_manager=MagicMock(),
        config=ArbConfig(
            dry_run=True, 
            max_hedge_hold_seconds=5, # Short timeout for test
            min_edge=0.01
        ),
        logger=logger
    )
    strategy._get_order_book = mock_get_order_book # Override with async mock
    
    # 2. Simulate an existing YES position held for too long
    logger.info("Test 1: Testing Timeout Logic")
    market = MarketInfo(
        condition_id="c1", slug="test-market", question="Test Market?", 
        yes_token_id="t_yes", no_token_id="t_no", 
        close_time=datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    
    # Inject position opened 10 seconds ago (Timeout is 5s)
    strategy._positions["t_yes"] = Position(
        market_slug="test-market", token_id="t_yes", side="YES", 
        size=10.0, avg_price=0.5, 
        opened_at=datetime.now(timezone.utc) - timedelta(seconds=10)
    )
    
    # Run check
    # We expect _execute_exit_sell_yes to be called
    strategy._execute_exit_sell_yes = AsyncMock(wraps=strategy._execute_exit_sell_yes)
    
    await strategy._check_and_execute(market)
    
    if strategy._execute_exit_sell_yes.called:
        print("✅ PASS: Timeout Logic triggered Stop Loss.")
    else:
        print("❌ FAIL: Timeout Logic DID NOT trigger.")

    # 3. Simulate Direct Profit logic
    logger.info("\nTest 2: Testing Direct Profit Logic")
    # Reset
    strategy._positions.clear()
    strategy._execute_exit_sell_yes.reset_mock()
    
    # Position opened 1s ago (No timeout)
    strategy._positions["t_yes"] = Position(
        market_slug="test-market", token_id="t_yes", side="YES", 
        size=10.0, avg_price=0.30, # Low entry
        opened_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    
    # Mock book to have High Bid (0.60) vs Entry (0.30) -> Huge Profit
    # Re-mock get_order_book for this test to show profitable bid
    async def mock_profitable_book(token_id):
         return OrderBookSnapshot(
            token_id=token_id,
            best_ask_price=0.65, 
            best_ask_size=100,
            best_bid_price=0.40, # Bid 0.40 > Entry 0.30 + edge
            best_bid_size=100
        )
    strategy._get_order_book = mock_profitable_book

    await strategy._check_and_execute(market)
    
    if strategy._execute_exit_sell_yes.called:
        print("✅ PASS: Direct Profit Logic triggered Sell.")
    else:
        print("❌ FAIL: Direct Profit Logic DID NOT trigger.")

if __name__ == "__main__":
    asyncio.run(verify_logic())
