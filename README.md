# AI Hedge Fund

This is a proof of concept for an AI-powered hedge fund. The goal of this project is to explore the use of AI to make trading decisions. This project is for **educational** purposes only and is not intended for real trading or investment.

<img width="2400" height="1460" alt="image" src="https://github.com/user-attachments/assets/e3985623-c226-4c1a-a587-e03fb4eca31e" />


Note: the system does not actually make any trades.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## Disclaimer

This project is for **educational and research purposes only**.

- Not intended for real trading or investment
- No investment advice or guarantees provided
- Creator assumes no liability for financial losses
- Consult a financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning purposes.

## How to Install

```bash
pipx install aihf
```

(or `uv tool install aihf`, or `pip install aihf` into an environment of your choice)

Then run it from anywhere:

```bash
aihf
```

### API keys

The app asks for keys the first time it needs them and saves them to `~/.hedge-fund/.env` — nothing to configure up front. It needs:

- A [Financial Datasets](https://financialdatasets.ai) API key, for prices, fundamentals, and earnings.
- One model API key for the investor agents. Supported providers: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi, TypeSafe (Jev).

Keys exported in your shell always win over the saved file.

## A-shares (A 股)

A mandate can trade mainland China A-shares by setting `market: cn`. No
Financial Datasets key is needed: prices and quotes come from Tencent, and
fundamentals, announcements, and news from Eastmoney's public endpoints. An
LLM provider key is still required for the investor agents.

```yaml
schema_version: 2
name: cn-value-fund
market: cn                    # us (default) | cn
language: zh                  # en | zh; cn defaults to zh
strategies:
  - name: deep-value
    blend: {mode: long_only}
    models:
      - name: graham
      - name: buffett
      - name: munger
risk:
  max_position_pct: 0.25
  max_gross_exposure: 1.0
capital: 1000000              # CNY
rebalance: weekly
benchmark: 000300.SH          # CSI 300; also the backtest's trading-day grid
```

```bash
cp hedge_fund/fund/example-cn.yaml ~/.hedge-fund/mandates/
aihf backtest ~/.hedge-fund/mandates/example-cn.yaml --universe 600519,000001,300750 --start 2024-01-02
aihf paper create cn-value --mandate ~/.hedge-fund/mandates/example-cn.yaml --universe 600519,000001,300750
aihf paper tick cn-value
```

What changes with `market: cn`:

- **Tickers** use `code.exchange` (`600519.SH`, `000001.SZ`, `430047.BJ`). A bare
  six-digit stock code is accepted and the exchange is inferred; indices need
  their suffix (`000300.SH`).
- **Calendar and clock** follow Shanghai (Asia/Shanghai), and the benchmark's
  bars are the trading-day grid, so exchange holidays need no separate calendar.
- **Currency** is CNY. Board lots are 100 shares: buys are rounded down to whole
  lots, while an odd-lot position can always be sold in full.
- **Fundamentals** are cumulative year-to-date filings. The client derives TTM
  revenue, profit, EPS, margins, ROE, and growth from annual + current YTD −
  prior-year YTD, and marks rows `period="ytd"` when that history is missing.
  Metrics are visible only from their announcement (`NOTICE_DATE`), never the
  fiscal period end.
- **News and announcements** come from Eastmoney; there is no insider-trade or
  analyst-consensus feed, so PEAD abstains on A-shares instead of inventing a
  surprise.
- **Minute funds are rejected** for `market: cn`: the A-share day has a midday
  break (11:30–13:00) that the current bar grid does not model.

With `language: zh` (the default for `market: cn`), the investor agents reason
and write their theses in Chinese, and fundamentals snapshots use Chinese
labels. Terminal chrome, CLI help, and log messages remain in English.

See [`hedge_fund/fund/example-cn.yaml`](hedge_fund/fund/example-cn.yaml) for a
complete mandate.

## How to Run

### Interactive app

```bash
aihf
```

With no arguments, this launches the interactive terminal app. It has two modes:

- **Paper trading** — your funds, each with a ledger of real market days and fake money. Highlight a fund to see what its next run will do; press enter to run it through the next completed session (an approval step shows the exact decision about to execute before anything trades), `s` for its full session history, `h`/`r` for the kill switch. Build a new fund from the same list: strategies, capital, cadence, tickers, and it is live. Every session is appended to a hash-chained ledger in `~/.hedge-fund/paper/<name>/`, so NAV is a track record, not a reset.
- **Backtesting** — replay a fund over history and watch its equity curve draw against its benchmark. Results are saved to `~/.hedge-fund/research/`. Fund definitions live in `~/.hedge-fund/mandates/` and carry no tickers; you pick the universe per backtest.



## Development

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused. This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/virattt/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
