import os
import json
import math
from datetime import datetime, date, timedelta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderType, ContractType
)
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL         = 'TSLA'
CONTRACTS      = 1        # 1 contract = 100 shares
PUT_OTM_PCT    = 0.05     # sell put 5% below current price
CALL_OTM_PCT   = 0.05     # sell call 5% above assignment price
TARGET_DTE     = 35       # target days to expiration when selling
STATE_FILE     = 'Wheel/estado_rueda.json'

# ── Clients ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
stock_data  = StockHistoricalDataClient(API_KEY, API_SECRET)
option_data = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {'fase': 'vender_put', 'ciclos': 0, 'premium_total': 0.0}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)

def get_stock_price():
    q = stock_data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=SYMBOL)
    )
    return float(q[SYMBOL].ask_price)

def target_expiry():
    """Find the next Friday that's ~TARGET_DTE days out."""
    target = date.today() + timedelta(days=TARGET_DTE)
    days_to_fri = (4 - target.weekday()) % 7
    return target + timedelta(days=days_to_fri)

def round_strike(price):
    """Round to nearest $5 increment (standard options strikes)."""
    return round(price / 5) * 5

def find_contract(contract_type, strike, expiry):
    """Find the best option contract near strike and expiry."""
    try:
        result = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[SYMBOL],
            contract_type=contract_type,
            expiration_date_gte=expiry - timedelta(days=7),
            expiration_date_lte=expiry + timedelta(days=7),
            strike_price_gte=str(strike - 10),
            strike_price_lte=str(strike + 10),
            status='active',
        ))
        contracts = result.option_contracts if hasattr(result, 'option_contracts') else result
        if not contracts:
            return None
        # Pick closest strike to target
        return min(contracts, key=lambda c: abs(float(c.strike_price) - strike))
    except Exception as e:
        print(f'Error finding contract ({contract_type} ${strike} {expiry}): {e}')
        return None

def get_option_mid(symbol):
    """Get mid-price of an option for limit order pricing."""
    try:
        q = option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        )
        quote = q[symbol]
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid <= 0 and ask <= 0:
            return None
        if bid <= 0:
            return round(ask * 0.9, 2)
        return round((bid + ask) / 2, 2)
    except Exception as e:
        print(f'Error getting quote for {symbol}: {e}')
        return None

def get_position(symbol):
    """Return qty for a symbol (negative = short)."""
    try:
        for pos in trading.get_all_positions():
            if pos.symbol == symbol:
                return int(float(pos.qty))
    except Exception:
        pass
    return 0


# ── Strategy phases ───────────────────────────────────────────────────────────

def fase_vender_put(state):
    """Sell a cash-secured put. Entry to the wheel."""
    price  = get_stock_price()
    strike = round_strike(price * (1 - PUT_OTM_PCT))
    expiry = target_expiry()
    dte    = (expiry - date.today()).days

    print(f'TSLA @ ${price:.2f}')
    print(f'Target put: ${strike} strike | {expiry} ({dte} DTE)')

    contract = find_contract(ContractType.PUT, strike, expiry)
    if not contract:
        print('No suitable put contract found — will retry next run')
        return state

    mid = get_option_mid(contract.symbol)
    if not mid or mid < 0.05:
        print(f'No valid premium for {contract.symbol}')
        return state

    # Sell at mid-price for a fair fill
    limit_px = max(round(mid, 2), 0.05)

    try:
        order = trading.submit_order(LimitOrderRequest(
            symbol=contract.symbol,
            qty=CONTRACTS,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_px,
        ))
        gross_premium = limit_px * 100 * CONTRACTS
        state.update({
            'fase':            'put_vendida',
            'put_symbol':      contract.symbol,
            'put_strike':      float(contract.strike_price),
            'put_expiry':      str(contract.expiration_date),
            'put_premium':     limit_px,
            'put_order_id':    str(order.id),
            'entry_price':     price,
            'put_sold_at':     datetime.now().isoformat(),
        })
        print(f'\nSOLD PUT:  {contract.symbol}')
        print(f'  Premium:    ${limit_px:.2f}/share  |  ${gross_premium:.0f} total')
        print(f'  Strike:     ${contract.strike_price}')
        print(f'  Expiry:     {contract.expiration_date}  ({dte} DTE)')
        print(f'  Breakeven:  ${float(contract.strike_price) - limit_px:.2f}')
        print(f'  Max profit: ${gross_premium:.0f} (if TSLA stays above ${contract.strike_price})')
    except Exception as e:
        print(f'Error selling put: {e}')

    return state


def fase_monitor_put(state):
    """Watch the short put. Handle expiry / assignment."""
    put_symbol = state.get('put_symbol', '')
    put_strike = state.get('put_strike', 0.0)
    put_expiry = state.get('put_expiry', '')

    price    = get_stock_price()
    expiry_d = date.fromisoformat(put_expiry) if put_expiry else date.today()
    dte      = (expiry_d - date.today()).days
    pct_otm  = (price - put_strike) / price * 100

    print(f'TSLA @ ${price:.2f} | Put ${put_strike} strike | {dte} DTE | {pct_otm:.1f}% OTM')

    put_qty = get_position(put_symbol)

    # Expired or assigned
    if dte <= 0 or put_qty == 0:
        tsla_qty = get_position(SYMBOL)
        if tsla_qty >= 100:
            print('ASSIGNED — now holding 100 TSLA shares')
            state['fase']             = 'asignado'
            state['assignment_price'] = put_strike
            state['shares_held']      = tsla_qty
        else:
            print('Put expired WORTHLESS — full premium kept!')
            state['ciclos']        = state.get('ciclos', 0) + 1
            state['premium_total'] = round(state.get('premium_total', 0) + state.get('put_premium', 0) * 100, 2)
            state['fase']          = 'vender_put'
            print(f'  Cycle #{state["ciclos"]} complete | Total premium collected: ${state["premium_total"]:.2f}')
        return state

    # Still alive — report status
    if dte <= 7 and price < put_strike * 1.03:
        print(f'WARNING: {dte} DTE and near/below strike — potential assignment soon')

    state['ultimo_chequeo'] = datetime.now().isoformat()
    state['tsla_price']     = price
    state['dte']            = dte
    state['pct_otm']        = round(pct_otm, 2)
    return state


def fase_vender_call(state):
    """Assigned on put — sell covered call to exit the position."""
    assignment_price = state.get('assignment_price', state.get('put_strike', 0))
    price  = get_stock_price()
    # Strike: at least above assignment price so we exit at a profit
    strike = max(
        round_strike(price * (1 + CALL_OTM_PCT)),
        round_strike(assignment_price * 1.01)
    )
    expiry = target_expiry()
    dte    = (expiry - date.today()).days

    print(f'TSLA @ ${price:.2f} | Assignment: ${assignment_price}')
    print(f'Target call: ${strike} strike | {expiry} ({dte} DTE)')

    contract = find_contract(ContractType.CALL, strike, expiry)
    if not contract:
        print('No suitable call contract found — will retry next run')
        return state

    mid = get_option_mid(contract.symbol)
    if not mid or mid < 0.05:
        print(f'No valid premium for {contract.symbol}')
        return state

    limit_px = max(round(mid, 2), 0.05)

    try:
        order = trading.submit_order(LimitOrderRequest(
            symbol=contract.symbol,
            qty=CONTRACTS,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_px,
        ))
        gross_premium = limit_px * 100 * CONTRACTS
        total_premium = state.get('premium_total', 0) + state.get('put_premium', 0) * 100
        state.update({
            'fase':          'call_vendida',
            'call_symbol':   contract.symbol,
            'call_strike':   float(contract.strike_price),
            'call_expiry':   str(contract.expiration_date),
            'call_premium':  limit_px,
            'call_order_id': str(order.id),
            'call_sold_at':  datetime.now().isoformat(),
        })
        print(f'\nSOLD CALL: {contract.symbol}')
        print(f'  Premium:     ${limit_px:.2f}/share  |  ${gross_premium:.0f} total')
        print(f'  Strike:      ${contract.strike_price}')
        print(f'  Expiry:      {contract.expiration_date}  ({dte} DTE)')
        print(f'  Total premium this cycle: ${total_premium + gross_premium:.0f}')
    except Exception as e:
        print(f'Error selling call: {e}')

    return state


def fase_monitor_call(state):
    """Watch the covered call. Handle expiry / shares called away."""
    call_symbol      = state.get('call_symbol', '')
    call_strike      = state.get('call_strike', 0.0)
    call_expiry      = state.get('call_expiry', '')
    assignment_price = state.get('assignment_price', 0.0)

    price    = get_stock_price()
    expiry_d = date.fromisoformat(call_expiry) if call_expiry else date.today()
    dte      = (expiry_d - date.today()).days
    pnl_share = price - assignment_price

    print(f'TSLA @ ${price:.2f} | Call ${call_strike} strike | {dte} DTE | PnL/share: ${pnl_share:.2f}')

    call_qty = get_position(call_symbol)

    if dte <= 0 or call_qty == 0:
        tsla_qty = get_position(SYMBOL)
        if tsla_qty < 100:
            # Shares were called away
            profit_per_share = call_strike - assignment_price
            put_prem  = state.get('put_premium', 0) * 100
            call_prem = state.get('call_premium', 0) * 100
            total = profit_per_share * 100 + put_prem + call_prem
            state['premium_total'] = round(state.get('premium_total', 0) + put_prem + call_prem, 2)
            state['ciclos']        = state.get('ciclos', 0) + 1
            state['fase']          = 'vender_put'
            print(f'\nSHARES CALLED AWAY at ${call_strike}!')
            print(f'  Stock profit:  ${profit_per_share * 100:.0f}')
            print(f'  Put premium:   ${put_prem:.0f}')
            print(f'  Call premium:  ${call_prem:.0f}')
            print(f'  Cycle total:   ${total:.0f}')
            print(f'  Cycle #{state["ciclos"]} complete | All-time premium: ${state["premium_total"]:.0f}')
        else:
            # Call expired worthless — sell another
            print('Call expired worthless — selling new covered call')
            call_prem = state.get('call_premium', 0) * 100
            state['premium_total'] = round(state.get('premium_total', 0) + call_prem, 2)
            state['fase']          = 'asignado'
        return state

    state['ultimo_chequeo'] = datetime.now().isoformat()
    state['tsla_price']     = price
    state['dte']            = dte
    return state


# ── Entry point ───────────────────────────────────────────────────────────────
state = load_state()
fase  = state.get('fase', 'vender_put')

print(f'[{datetime.now().isoformat()}]  WHEEL STRATEGY — TSLA')
print(f'Fase: {fase} | Ciclos completados: {state.get("ciclos", 0)} | Premium total: ${state.get("premium_total", 0):.2f}')
print('─' * 60)

if fase == 'vender_put':
    state = fase_vender_put(state)
elif fase == 'put_vendida':
    state = fase_monitor_put(state)
elif fase == 'asignado':
    state = fase_vender_call(state)
elif fase == 'call_vendida':
    state = fase_monitor_call(state)
else:
    print(f'Unknown fase: {fase}')

save_state(state)
