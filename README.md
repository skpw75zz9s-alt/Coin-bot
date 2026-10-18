# Coin-bot: a memecoin desk that tries to pay for its own subscription

A small team of trading roles runs every hour on GitHub Actions. There's no server to keep up and no app to watch.
It trades memecoins on Crypto.com Exchange, sets aside part of every profit for your monthly
subscription, and messages you only when something important happens.

> **Read this first.** Most memecoin traders lose money, and so do most bots. Nothing here
> guarantees a profit. The team is built to **lose slowly and stop by itself** when it's
> wrong: small positions, hard stops, and a kill switch. It starts in **paper mode** (real
> prices, fake money). Leave it there until the backtest and a few weeks of paper results
> look good, and only ever go live with money you can afford to lose completely.

## The team

Each role is one small file in `coinbot/team/`:

| Role | Job |
|---|---|
| **Scout** | Filters the coin list to the pairs that are liquid right now: 24h volume ≥ $25k and spread ≤ 0.5%. |
| **Analyst** | Looks for volume-backed breakouts in an uptrend. The close must beat the 24h high on at least 2× normal volume, with fast EMA above slow EMA. It skips coins that were already overbought, that printed one giant candle, or that are up more than 60% in a day. |
| **Risk manager** | Sizes each trade so a stopped-out position costs about 1% of equity. Caps each position at 25% of equity and 0.5% of the coin's daily volume, with at most 3 positions. After −5% in a day it stops buying until the next day. **After −25% from the peak it sells everything and halts** until you resume it. |
| **Trader** | Exits on −8% stop-loss, +25% take-profit, a 6% trailing stop once a position is up 8%, or after 72h. After exiting a coin it waits 12h before buying it again. |
| **Treasurer** | Moves 50% of net realized profit into a ring-fenced **subscription reserve** until this month's bill is covered, then 20% after that. Losses have to be won back before anything else is swept. The reserve is never traded. |
| **Reporter** | Writes `REPORT.md` every run. It pings your webhook on trades, when the month gets covered, on a halt, after 3 failed runs in a row, and with a Monday digest. |

`coinbot/desk.py` runs them in that order once an hour. Every number above is set in [`config.toml`](config.toml).

## Setup (about 10 minutes)

1. **Optional: get notifications.** Create a Discord or Slack incoming webhook and save it as the
   repo secret `COINBOT_WEBHOOK_URL` (Settings → Secrets and variables → Actions).
2. **Set your bill.** Add the repo variable `COINBOT_MONTHLY_SUBSCRIPTION_USD`, for example `20`, `100` or
   `200`, or edit `config.toml`.
3. **Run a backtest.** Go to Actions → *memecoin-desk* → *Run workflow*, pick `backtest`, and choose 60 days.
   The run summary shows return, max drawdown, win rate and how many months of subscription the
   reserve would have covered. If those numbers are bad, stop here.
4. **Let it paper trade.** Do nothing. The hourly schedule is already on. Each run saves its state and
   `REPORT.md` to the `bot-state` branch, so open that branch to see how it's doing.

### Going live (only after weeks of good paper results)

Live mode needs **all three** of these, so it can't be switched on by accident:

- The repo variable `COINBOT_MODE` set to `live`.
- The secrets `CRYPTOCOM_API_KEY` and `CRYPTOCOM_API_SECRET`. Create the key with **trading
  permission only, never withdrawal**. GitHub's runners don't have fixed IPs, so the key can't be
  IP-locked. That's another reason it must not be able to withdraw.
- The secret `COINBOT_LIVE_CONFIRM` set to exactly: `I understand this can lose real money`

The desk then trades at most `COINBOT_LIVE_MAX_CAPITAL_USD` (default $300) of the account's
USD balance and leaves anything above that alone. Live and paper keep separate state files, so paper positions
never carry over.

**About "paying the subscription":** the bot never withdraws money. That's deliberate, because a
key that can withdraw is a key that can be stolen and drained. The reserve is USD the bot won't touch.
When it covers a month, move that money out yourself and pay the bill.

## Running it yourself

No dependencies, just Python 3.11+.

```bash
python -m unittest discover -s tests       # tests
python -m coinbot run                      # one paper cycle, state in ./state/
python -m coinbot status                   # print the latest report
python -m coinbot resume                   # clear a kill-switch halt
python -m coinbot backtest --days 60       # replay history through the same team
python -m coinbot backtest --data dir/     # ...or from <INSTRUMENT>.json files of {t,o,h,l,c,v}
```

## Good to know

- **Liquidity is the real limit.** In October 2026, memecoin USD pairs on Crypto.com were thin.
  Only DOGE had more than $150k a day, and PEPE, WIF, BONK and TRUMP were around $25–55k. That's
  fine for an account of a few hundred dollars. It does not scale, and the volume cap will keep
  positions small.
- **It costs nothing to run.** Hourly runs use about 720 Actions minutes a month, inside the free
  2,000 for private repos and free for public ones. No paid APIs are used.
- If a run fails it retries next hour. You get an alert only after 3 failures in a row.
- GitHub can pause scheduled workflows on **public** repos after 60 days without activity. It emails
  you first, and turning them back on takes one click.
- The live order code follows Crypto.com's v1 API docs and is unit-tested against mocked responses.
  It has **not** been run against a real account yet, so watch the first live trades closely.
