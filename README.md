# PolySpike Trader

A sophisticated automated trading bot for Polymarket featuring multiple trading strategies on the Polygon network:

1. **Spike Strategy** - Detects price spikes and executes trades with automatic position management
2. **YES/NO Arbitrage** - Market-neutral box strategy that buys YES+NO when sum < $1.00

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run spike strategy (default)
python main.py --strategy spike

# Run YES/NO arbitrage in dry-run mode
python main.py --strategy yesno_arb --dry-run

# Run YES/NO arbitrage live (after testing!)
DRY_RUN=false python main.py --strategy yesno_arb
```

## Strategy Overview


The bot implements the following trading strategy:

1. **Price Spike Detection**
   - Monitors price movements across market pairs
   - Detects significant price spikes above/below threshold
   - Executes trades when spike conditions are met

2. **Position Management**
   - Automatic take-profit and stop-loss execution
   - Position size management based on account balance
   - Minimum liquidity requirements for trade execution
   - Maximum concurrent trades limit

3. **Risk Management**
   - Slippage protection
   - Maximum holding time limits
   - Minimum liquidity requirements
   - Concurrent trade limits
   - USDC balance checks

## Features

- Multi-pair trading support
- Real-time price monitoring
- Automatic spike detection
- Configurable take-profit and stop-loss levels
- USDC balance management
- Automatic API credential refresh
- Comprehensive logging system
- Thread-safe state management
- Error handling and recovery
- Graceful shutdown handling

## Prerequisites

- Python 3.7+
- MetaMask wallet with Polygon network
- USDC balance on Polygon network
- Polymarket account

## Installation

1. Creat venv:
```bash
python -m venv .venv
.venv/Scripts/activate
```

2. Install required Python packages:
```bash
pip install web3==6.11.1
pip install python-dotenv==1.0.0
pip install requests==2.31.0
pip install py-clob-client==0.1.0
pip install halo==0.0.31
pip install colorlog==6.7.0
pip install dotenv
```

3. Create a `.env` file with your configuration:
```env
# Wallet Configuration
PK=your_private_key_here
YOUR_PROXY_WALLET=your_proxy_wallet_address
BOT_TRADER_ADDRESS=your_trader_address
USDC_CONTRACT_ADDRESS=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
POLYMARKET_SETTLEMENT_CONTRACT=0x56C79347e95530c01A2FC76E732f9566dA16E113

# Trading Parameters
trade_unit=3.0
slippage_tolerance=0.02
pct_profit=0.03
pct_loss=-0.025
cash_profit=1.0
cash_loss=-0.75
spike_threshold=0.02
sold_position_time=120
holding_time_limit=3600
price_history_size=100
cooldown_period=10
keep_min_shares=1
max_concurrent_trades=3
min_liquidity_requirement=10.0
```

## Configuration Parameters

### Wallet Settings
- `PK`: Your wallet's private key
- `YOUR_PROXY_WALLET`: Your Polymarket proxy wallet address
- `BOT_TRADER_ADDRESS`: Your MetaMask wallet address
- `USDC_CONTRACT_ADDRESS`: USDC contract address on Polygon
- `POLYMARKET_SETTLEMENT_CONTRACT`: Polymarket settlement contract address

### Trading Parameters
- `trade_unit`: Base trade size in USDC
- `slippage_tolerance`: Maximum allowed slippage (e.g., 0.02 for 2%)
- `pct_profit`: Take profit threshold (e.g., 0.03 for 3%)
- `pct_loss`: Stop loss threshold (e.g., -0.025 for -2.5%)
- `cash_profit`: Take profit in USDC
- `cash_loss`: Stop loss in USDC
- `spike_threshold`: Minimum price movement to trigger trade
- `sold_position_time`: Cooldown period between trades (seconds)
- `holding_time_limit`: Maximum time to hold a position (seconds)
- `price_history_size`: Number of price points to track
- `cooldown_period`: Retry cooldown for failed orders (seconds)
- `keep_min_shares`: Minimum shares to keep when selling
- `max_concurrent_trades`: Maximum number of concurrent trades
- `min_liquidity_requirement`: Minimum liquidity required to trade (USDC)

## Running the Bot

1. Ensure your `.env` file is properly configured
2. Run the bot:
```bash
python test.py
```

## Logging

The bot maintains detailed logs in the `logs` directory:
- `polymarket_bot.log`: Main log file with all bot activities
- Logs include price updates, trade executions, and error messages
- Color-coded console output for easy monitoring

## Safety Features

- Automatic retry mechanism for failed orders
- USDC balance checks before trades
- Comprehensive error handling and logging
- Transaction receipt verification
- Thread-safe state management
- Graceful shutdown handling

## Important Notes

- Ensure sufficient USDC balance in your wallet
- Monitor the bot's logs regularly
- The bot maintains minimum shares when selling
- Trades are executed with configurable unit size
- API credentials are refreshed hourly
- Price updates occur every second
- Position checks occur every second

---

## YES/NO Arbitrage Strategy

### Overview

The YES/NO arbitrage strategy attempts to capture spread when the combined price of YES and NO shares falls below $1.00. By purchasing both outcomes simultaneously, you lock in a guaranteed profit at settlement (minus execution costs).

**How it works:**
```
Example: Bitcoin price market
- YES ask: $0.48 (betting price goes up)
- NO ask:  $0.50 (betting price stays/goes down)
- Sum:     $0.98

At settlement, ONE of YES or NO will be worth $1.00.
Since you own both, you receive $1.00 regardless of outcome.
Profit = $1.00 - $0.98 = $0.02 per share (before fees)
```

### EOA Key Configuration

The YES/NO arbitrage strategy uses a **normal EOA (Externally Owned Account)** for trading. The same address serves as both the trading identity and the balance holder.

1. **Generate or use an existing EOA private key**

2. **Add to your `.env` file:**
   ```bash
   PK=your_64_character_hex_private_key
   EOA_ADDRESS=0xYourDerivedAddress  # Optional, derived from PK if not set
   ```

3. **Fund the EOA:**
   - Transfer USDC to your EOA address on Polygon
   - Ensure you have MATIC for gas fees

4. **Security best practices:**
   - Never share or commit your private key
   - Use a dedicated trading wallet with limited funds
   - The key is only read from `.env` at startup
   - No logging of private key material

### Hedge Logic

When placing a box trade, BOTH legs must fill for the arbitrage to be complete. The hedge manager handles partial fill scenarios:

#### Scenario 1: Both Legs Fill ✅
Perfect execution - profit is locked at settlement.

#### Scenario 2: One Leg Fills, Other Fails ⚠️

**Unwind Mode** (default, `HEDGE_MODE=unwind`):
1. Cancel the unfilled order
2. Sell back the filled position at market
3. Accept small loss to exit the incomplete position

**Complete Mode** (`HEDGE_MODE=complete`):
1. Cancel the unfilled order
2. Place a more aggressive order for the missing leg (up to `MAX_HEDGE_SLIPPAGE`)
3. Falls back to unwind if aggressive order fails

### Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_SUM_PRICE` | 0.99 | Trigger when YES + NO ask ≤ this |
| `MIN_EDGE` | 0.005 | Minimum edge after fees (0.5%) |
| `TRADE_NOTIONAL_USDC` | 10 | USDC per trade |
| `LEG_SLIPPAGE_BUFFER` | 0.002 | Buffer on limit prices (0.2%) |
| `MIN_LIQUIDITY` | 25 | Min $ at best ask |
| `HEDGE_TIMEOUT_MS` | 750 | Wait for fill confirmation |
| `MAX_HEDGE_SLIPPAGE` | 0.01 | Max slippage for hedge (1%) |
| `TARGET_TAGS` | Crypto,Bitcoin,... | Market filter keywords |
| `MAX_TIME_TO_CLOSE_MIN` | 20 | Only markets closing soon |
| `DRY_RUN` | true | Log orders without executing |

### Running the Strategy

```bash
# Dry run mode (recommended first)
python main.py --strategy yesno_arb --dry-run

# With environment variable
DRY_RUN=true python main.py --strategy yesno_arb

# Live trading (use caution!)
DRY_RUN=false python main.py --strategy yesno_arb
```

### Execution Risks

> ⚠️ **WARNING: This is NOT risk-free arbitrage**

| Risk | Description | Mitigation |
|------|-------------|------------|
| **Partial Fills** | One leg fills, other fails | Hedge manager unwinds position |
| **Spread Changes** | Prices move between order and fill | Slippage buffer on limits |
| **Latency** | Network delays cause missed opportunities | Use fast RPC, short timeouts |
| **Liquidity** | Thin markets have high slippage | MIN_LIQUIDITY filter |
| **Settlement Risk** | Market resolution disputes | Diversify across markets |
| **Smart Contract Risk** | CLOB or settlement bugs | Use small position sizes |

### Testing Workflow

1. **Start with dry run mode** - verify market discovery and calculations
2. **Use small notional** - set `TRADE_NOTIONAL_USDC=1` for initial live tests
3. **Monitor logs closely** - watch for hedge triggers and fill issues
4. **Scale gradually** - increase size only after consistent fills

---

## Disclaimer

This bot is for educational purposes only. Trading cryptocurrencies and prediction markets involves significant risk. Use at your own risk and never trade with funds you cannot afford to lose.

**The YES/NO arbitrage strategy carries execution risk.** Partial fills, spread slippage, and settlement disputes can result in losses. Always test thoroughly in dry-run mode before live trading.


## License

[Your License Here]

## Bot Structure

![Bot Architecture Diagram](diagram.png)

1. **State Management**
   - Global variables for tracking trades and prices
   - Price history management
   - Active trade tracking

2. **Trading Logic**
   - Price spike detection
   - Order placement with retries
   - Take-profit and stop-loss management
   - USDC allowance management

3. **Main Loop**
   - Price updates
   - Trade detection
   - Position management
   - API credential refresh

## Contact ME
[Telegram](https://t.me/trust4120)
