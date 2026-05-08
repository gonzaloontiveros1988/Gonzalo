import os
import json
import time
from datetime import datetime, timedelta, timezone
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ── Who we're copying ─────────────────────────────────────────────────────────
POLITICIAN_ID   = 'K000389'   # Ro Khanna — +112% excess vs S&P 500 since Jan 2024
POLITICIAN_NAME = 'Ro Khanna'

# ── Sizing: how much $ to deploy per copied buy ───────────────────────────────
COPY_BUY_VALUE = 500   # spend ~$500 per copied purchase

# ── Files ─────────────────────────────────────────────────────────────────────
STATE_FILE = 'CopyTrading/estado_copy.json'
LOG_FILE   = 'CopyTrading/resultado_copy.json'

# ── Clients ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

CAPITOL_BASE = 'https://bff.capitoltrades.com'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':          'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin':          'https://www.capitoltrades.com',
    'Referer':         'https://www.capitoltrades.com/',
    'Sec-Fetch-Dest':  'empty',
    'Sec-Fetch-Mode':  'cors',
    'Sec-Fetch-Site':  'same-site',
}

LOOKBACK_DAYS = 7   # how far back to check for new trades on each run


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {'copied_tx_ids': [], 'positions': {}}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def load_log():
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_log(log):
    with open(LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_trades():
    """Fetch recent trades for Ro Khanna from Capitol Trades."""
    trades  = []
    cutoff  = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    for page in range(1, 6):
        try:
            r = requests.get(
                f'{CAPITOL_BASE}/trades',
                headers=HEADERS,
                params={
                    'politician': POLITICIAN_ID,
                    'pageSize':   100,
                    'page':       page,
                },
                timeout=20,
            )
            r.raise_for_status()
            body    = r.json()
            records = body.get('data', [])

            if not records:
                break

            for rec in records:
                raw_date = rec.get('pubDate', '')
                try:
                    pub_dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
                except Exception:
                    pub_dt = None

                if pub_dt and pub_dt < cutoff:
                    return trades   # records are newest-first; stop early

                # Filter to our politician (in case API ignores the param)
                pol   = rec.get('politician', {})
                pol_id = pol.get('id', '')
                name   = f"{pol.get('firstName','')} {pol.get('lastName','')}".strip()
                if pol_id == POLITICIAN_ID or POLITICIAN_NAME.lower() in name.lower():
                    trades.append(rec)

            total = body.get('meta', {}).get('paging', {}).get('totalItems', 0)
            if page * 100 >= total:
                break

            time.sleep(0.5)

        except requests.HTTPError as e:
            # If politician filter not supported, fall back to no filter
            if e.response.status_code == 400 and page == 1:
                print('Politician filter not supported, fetching all trades...')
                return fetch_all_trades_filter_client()
            print(f'HTTP error page {page}: {e}')
            break
        except Exception as e:
            print(f'Error fetching page {page}: {e}')
            break

    return trades


def fetch_all_trades_filter_client():
    """Fallback: fetch all trades and filter for Khanna client-side."""
    trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    for page in range(1, 10):
        try:
            r = requests.get(
                f'{CAPITOL_BASE}/trades',
                headers=HEADERS,
                params={'pageSize': 100, 'page': page},
                timeout=20,
            )
            r.raise_for_status()
            body    = r.json()
            records = body.get('data', [])
            if not records:
                break

            stop = False
            for rec in records:
                raw_date = rec.get('pubDate', '')
                try:
                    pub_dt = datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
                except Exception:
                    pub_dt = None

                if pub_dt and pub_dt < cutoff:
                    stop = True
                    break

                pol    = rec.get('politician', {})
                pol_id = pol.get('id', '')
                name   = f"{pol.get('firstName','')} {pol.get('lastName','')}".strip()
                if pol_id == POLITICIAN_ID or POLITICIAN_NAME.lower() in name.lower():
                    trades.append(rec)

            if stop:
                break
            time.sleep(0.5)

        except Exception as e:
            print(f'Error (fallback) page {page}: {e}')
            break

    return trades


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def get_price(ticker):
    try:
        q = data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=ticker)
        )
        return float(q[ticker].ask_price)
    except Exception:
        return None


def get_position_qty(ticker):
    try:
        for pos in trading.get_all_positions():
            if pos.symbol == ticker:
                return int(float(pos.qty))
        return 0
    except Exception:
        return 0


# ── Trade execution ───────────────────────────────────────────────────────────

def execute_trade(trade, log):
    tx_id     = trade.get('_txId', '')
    issuer    = trade.get('issuer', {})
    ticker    = (issuer.get('ticker') or '').upper().strip()
    tx_type   = (trade.get('txType') or '').lower()
    asset_type = (trade.get('assetType') or '').lower()
    pub_date  = trade.get('pubDate', '')
    khanna_size = trade.get('size', 'Unknown')

    # Skip options — Alpaca options require separate permissions
    if any(w in asset_type for w in ('option', 'call', 'put')):
        print(f'  [skip] Options trade — {ticker} {tx_type}')
        return

    if not ticker or ticker in ('', 'N/A', '--'):
        print(f'  [skip] No ticker for tx {tx_id}')
        return

    # Map tx_type to buy / sell
    if any(w in tx_type for w in ('purchase', 'buy', 'exercise')):
        action = 'buy'
    elif any(w in tx_type for w in ('sale', 'sell')):
        action = 'sell'
    else:
        print(f'  [skip] Unknown tx_type: {tx_type}')
        return

    price = get_price(ticker)
    if not price:
        print(f'  [skip] Cannot get price for {ticker}')
        return

    if action == 'buy':
        qty  = max(1, int(COPY_BUY_VALUE / price))
        side = OrderSide.BUY
    else:
        qty  = get_position_qty(ticker)
        side = OrderSide.SELL
        if qty <= 0:
            print(f'  [skip] No position in {ticker} to sell')
            log.append({
                'tx_id': tx_id, 'ticker': ticker, 'action': 'sell',
                'qty': 0, 'note': 'no position held',
                'khanna_size': khanna_size, 'pub_date': pub_date,
                'timestamp': datetime.now().isoformat()
            })
            return

    try:
        order = trading.submit_order(MarketOrderRequest(
            symbol=ticker, qty=qty, side=side,
            time_in_force=TimeInForce.DAY
        ))
        entry = {
            'tx_id':        tx_id,
            'ticker':       ticker,
            'action':       action,
            'qty':          qty,
            'price':        price,
            'total':        round(qty * price, 2),
            'khanna_size':  khanna_size,
            'pub_date':     pub_date,
            'order_id':     str(order.id),
            'timestamp':    datetime.now().isoformat(),
        }
        log.append(entry)
        print(f'  COPIED  {action.upper()} {qty} {ticker} @ ~${price:.2f}  '
              f'(Khanna: {khanna_size})')
    except Exception as e:
        print(f'  ERROR   {action} {ticker}: {e}')
        log.append({
            'tx_id': tx_id, 'ticker': ticker, 'action': action,
            'error': str(e), 'timestamp': datetime.now().isoformat()
        })


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    state      = load_state()
    log        = load_log()
    copied_ids = set(state.get('copied_tx_ids', []))

    print(f'[{datetime.now().isoformat()}] Checking trades for {POLITICIAN_NAME}...')
    trades = fetch_trades()
    print(f'Found {len(trades)} trade(s) in last {LOOKBACK_DAYS} days')

    new_trades = [t for t in trades if t.get('_txId') not in copied_ids]
    print(f'New trades to copy: {len(new_trades)}')

    for trade in new_trades:
        ticker   = trade.get('issuer', {}).get('ticker', 'N/A')
        tx_type  = trade.get('txType', 'N/A')
        pub_date = trade.get('pubDate', '')[:10]
        print(f'\n  Trade: {ticker} | {tx_type} | disclosed {pub_date}')
        execute_trade(trade, log)
        copied_ids.add(trade.get('_txId', ''))
        time.sleep(1)

    state['copied_tx_ids'] = list(copied_ids)
    state['ultimo_chequeo'] = datetime.now().isoformat()
    state['politician'] = POLITICIAN_NAME
    state['total_trades_tracked'] = len(copied_ids)

    save_state(state)
    save_log(log[-200:])   # keep last 200 entries

    summary = {
        'politician':      POLITICIAN_NAME,
        'new_copied':      len(new_trades),
        'total_tracked':   len(copied_ids),
        'ultimo_chequeo':  state['ultimo_chequeo'],
    }
    print(f'\n{json.dumps(summary, indent=2)}')


if __name__ == '__main__':
    run()
