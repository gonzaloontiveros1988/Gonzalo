"""
Wheel Strategy — NVDA
=====================
Initial investment: $10,000 in NVDA shares (buy on first run)

Stage 0 → Buy:  Purchase as many 100-share lots as $10,000 allows.
               If price > $100 and $10,000 < 100 shares, start with puts instead.
Stage 1 → Sell covered calls: 10% above cost basis, 2-4 weeks out
           If shares called away → back to Stage 1 (sell puts with cash)
Stage 2 → Sell cash-secured puts: 10% OTM, 2-4 weeks out (if shares called away)
           If assigned again → back to selling calls

Rules enforced:
- Buy exactly floor(10000 / price / 100) * 100 shares on init (multiples of 100)
- Never sell call below cost basis (purchase price minus all premiums)
- Never sell put without enough cash to cover assignment
- Close any position early at 50% profit, immediately re-sell
- Track total premium across all cycles
- Daily summary at market close
- Do nothing outside market hours
"""

import os
import json
import datetime as dt
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest, LimitOrderRequest, MarketOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, ContractType

from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest

# ── Parameters ────────────────────────────────────────────────────────────────
SYMBOL       = 'NVDA'
BUDGET       = 10_000.0   # initial capital to deploy in NVDA
CONTRACTS    = 1           # 1 contract = 100 shares
PUT_OTM_PCT  = 0.10        # sell put 10% below current price
CALL_OTM_PCT = 0.10        # sell call 10% above cost basis
DTE_MIN      = 14          # 2 weeks minimum
DTE_MAX      = 28          # 4 weeks maximum
EARLY_CLOSE  = 0.50        # close at 50% profit

STATE_FILE   = 'Wheel/estado_rueda_nvda.json'
SUMMARY_FILE = 'Wheel/resumen_diario_nvda.json'

# ── Clients ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
stock_data  = StockHistoricalDataClient(API_KEY, API_SECRET)
option_data = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_et():
    utc = dt.datetime.utcnow()
    offset = -4  # EDT (UTC-4), switch to -5 in November
    return utc + dt.timedelta(hours=offset)

def is_market_hours():
    t = now_et()
    if t.weekday() >= 5:
        return False
    open_  = t.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = t.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= t <= close_

def is_close_window():
    t = now_et()
    return t.weekday() < 5 and t.hour == 15 and t.minute >= 45


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            'fase':               'iniciar',   # first run buys shares
            'ciclos':             0,
            'premium_total':      0.0,
            'premium_this_cycle': 0.0,
            'cost_basis':         None,
            'shares_held':        0,
        }

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


# ── Market data ───────────────────────────────────────────────────────────────

def get_stock_price():
    q = stock_data.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=SYMBOL)
    )
    return float(q[SYMBOL].ask_price)

def get_option_mid(symbol):
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
            return round(ask * 0.90, 2)
        return round((bid + ask) / 2, 2)
    except Exception as e:
        print(f'  [quote error] {symbol}: {e}')
        return None

def get_account():
    return trading.get_account()

def get_position(symbol):
    try:
        for pos in trading.get_all_positions():
            if pos.symbol == symbol:
                return int(float(pos.qty))
    except Exception:
        pass
    return 0


# ── Contract selection ────────────────────────────────────────────────────────

def target_expiry():
    today = dt.date.today()
    mid   = today + dt.timedelta(days=(DTE_MIN + DTE_MAX) // 2)  # ~21 days
    days_ahead = (4 - mid.weekday()) % 7                          # nearest Friday
    expiry = mid + dt.timedelta(days=days_ahead)
    if (expiry - today).days < DTE_MIN:
        expiry += dt.timedelta(days=7)
    return expiry

def round_strike(price):
    return round(price / 5) * 5

def find_contract(contract_type, strike, expiry):
    try:
        result = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[SYMBOL],
            contract_type=contract_type,
            expiration_date_gte=expiry - dt.timedelta(days=5),
            expiration_date_lte=expiry + dt.timedelta(days=5),
            strike_price_gte=str(strike - 10),
            strike_price_lte=str(strike + 10),
            status='active',
        ))
        contracts = getattr(result, 'option_contracts', result)
        if not contracts:
            return None
        return min(contracts, key=lambda c: abs(float(c.strike_price) - strike))
    except Exception as e:
        print(f'  [contract error]: {e}')
        return None


# ── Cash safety check ─────────────────────────────────────────────────────────

def can_afford_put(strike):
    try:
        acct = get_account()
        buying_power = float(acct.buying_power)
        needed = strike * 100 * CONTRACTS
        ok = buying_power >= needed
        print(f'  Cash check: need ${needed:,.0f} | have ${buying_power:,.0f} → {"OK" if ok else "INSUFFICIENT"}')
        return ok
    except Exception as e:
        print(f'  [cash check error]: {e}')
        return True


# ── Early close at 50% profit ─────────────────────────────────────────────────

def check_early_close(state):
    fase = state.get('fase')
    if fase == 'put_vendida':
        symbol   = state.get('put_symbol')
        sold_for = state.get('put_premium', 0)
    elif fase == 'call_vendida':
        symbol   = state.get('call_symbol')
        sold_for = state.get('call_premium', 0)
    else:
        return False

    if not symbol or not sold_for:
        return False

    current = get_option_mid(symbol)
    if current is None:
        return False

    profit_pct = (sold_for - current) / sold_for
    print(f'  50% check: sold ${sold_for:.2f} | now ${current:.2f} | profit {profit_pct*100:.1f}%')

    if profit_pct >= EARLY_CLOSE:
        print(f'  → 50% profit reached! Closing early and re-selling.')
        try:
            trading.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=CONTRACTS,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(current * 1.05, 2),
            ))
            profit = (sold_for - current) * 100 * CONTRACTS
            state['premium_total']      = round(state.get('premium_total', 0) + profit, 2)
            state['premium_this_cycle'] = round(state.get('premium_this_cycle', 0) + profit, 2)
            print(f'  Closed for ${profit:.2f} profit')
        except Exception as e:
            print(f'  [close error]: {e}')
            return False
        return True

    return False


# ── Stage 0: Initial share purchase ──────────────────────────────────────────

def fase_iniciar(state):
    """
    Buy as many 100-share lots of NVDA as BUDGET allows.
    If the stock is too expensive for even 100 shares, switch to put selling.
    """
    price  = get_stock_price()
    lots   = int(BUDGET / price / 100)     # full 100-share lots within budget
    shares = lots * 100

    print(f'NVDA @ ${price:.2f}')
    print(f'Budget ${BUDGET:,.0f} → {lots} lot(s) → {shares} shares (${shares*price:,.0f})')

    if shares < 100:
        print(f'  ${BUDGET:,.0f} not enough for 100 shares at ${price:.2f}')
        print(f'  → Switching to put-selling mode (Stage 1) with ${BUDGET:,.0f} as collateral')
        state['fase'] = 'vender_put'
        return state

    try:
        order = trading.submit_order(MarketOrderRequest(
            symbol=SYMBOL,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        cost_basis = price  # approximation; will be refined from fill price
        print(f'\n  BOUGHT {shares} shares of {SYMBOL} @ ~${price:.2f}')
        print(f'  Estimated cost: ${shares * price:,.0f}')
        print(f'  Cost basis:     ${cost_basis:.2f}/share')
        print(f'  Order ID:       {order.id}')

        state.update({
            'fase':               'asignado',   # go straight to covered call selling
            'shares_held':        shares,
            'cost_basis':         round(cost_basis, 2),
            'buy_order_id':       str(order.id),
            'buy_price':          round(price, 2),
            'buy_date':           dt.datetime.now().isoformat(),
        })
    except Exception as e:
        print(f'  [buy error]: {e}')

    return state


# ── Stage 1: Sell covered call ────────────────────────────────────────────────

def fase_vender_call(state):
    cost_basis = state.get('cost_basis', state.get('buy_price', 0))
    price      = get_stock_price()
    expiry     = target_expiry()
    dte        = (expiry - dt.date.today()).days

    # Rule: call strike must be at least 10% above current price AND above cost basis
    target_strike = round_strike(max(
        price * (1 + CALL_OTM_PCT),
        cost_basis * 1.005              # never below cost basis
    ))

    print(f'NVDA @ ${price:.2f} | Cost basis: ${cost_basis:.2f}')
    print(f'Target call: ${target_strike} | {expiry} ({dte} DTE)')

    contract = find_contract(ContractType.CALL, target_strike, expiry)
    if not contract:
        print('No suitable call contract found — will retry')
        return state

    # Hard safety: block if strike would be below cost basis
    if float(contract.strike_price) < cost_basis:
        print(f'  BLOCKED: strike ${contract.strike_price} < cost basis ${cost_basis:.2f}')
        return state

    mid = get_option_mid(contract.symbol)
    if not mid or mid < 0.05:
        print(f'No valid premium for {contract.symbol}')
        return state

    limit_px = round(mid, 2)

    try:
        order = trading.submit_order(LimitOrderRequest(
            symbol=contract.symbol,
            qty=CONTRACTS,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_px,
        ))
        gross      = limit_px * 100 * CONTRACTS
        total_cyc  = state.get('premium_this_cycle', 0) + gross
        if_called  = (float(contract.strike_price) - cost_basis) * 100 * CONTRACTS

        print(f'\n  SOLD CALL: {contract.symbol}')
        print(f'  Premium:   ${limit_px:.2f}/share | ${gross:.0f} total')
        print(f'  Strike:    ${contract.strike_price} | Expiry: {contract.expiration_date} ({dte} DTE)')
        print(f'  If called: stock profit ${if_called:.0f} + cycle premium ${total_cyc:.0f}')

        state.update({
            'fase':               'call_vendida',
            'call_symbol':        contract.symbol,
            'call_strike':        float(contract.strike_price),
            'call_expiry':        str(contract.expiration_date),
            'call_premium':       limit_px,
            'call_order_id':      str(order.id),
            'call_sold_at':       dt.datetime.now().isoformat(),
            'premium_this_cycle': round(total_cyc, 2),
        })
    except Exception as e:
        print(f'  [sell call error]: {e}')

    return state


# ── Stage 1 monitor ───────────────────────────────────────────────────────────

def fase_monitor_call(state):
    if check_early_close(state):
        state['fase'] = 'asignado'   # re-sell a new call
        return state

    call_symbol = state.get('call_symbol', '')
    call_strike = state.get('call_strike', 0.0)
    call_expiry = state.get('call_expiry', '')
    cost_basis  = state.get('cost_basis', 0.0)

    price    = get_stock_price()
    expiry_d = dt.date.fromisoformat(call_expiry) if call_expiry else dt.date.today()
    dte      = (expiry_d - dt.date.today()).days
    pnl      = (price - cost_basis) * state.get('shares_held', 100)

    print(f'NVDA @ ${price:.2f} | Short call ${call_strike} | {dte} DTE | Unrealized PnL: ${pnl:.2f}')

    call_qty = get_position(call_symbol)

    if dte <= 0 or call_qty == 0:
        nvda_qty = get_position(SYMBOL)
        if nvda_qty < 100:
            # Shares called away — go back to put selling
            stock_profit  = (call_strike - cost_basis) * 100 * CONTRACTS
            total_premium = state.get('premium_this_cycle', 0)
            state['premium_total'] = round(state.get('premium_total', 0) + total_premium, 2)
            state['ciclos']        = state.get('ciclos', 0) + 1
            print(f'\n  SHARES CALLED AWAY at ${call_strike}')
            print(f'  Stock profit:     ${stock_profit:.0f}')
            print(f'  Cycle premium:    ${total_premium:.0f}')
            print(f'  Cycle total:      ${stock_profit + total_premium:.0f}')
            print(f'  All-time premium: ${state["premium_total"]:.2f}')
            state.update({
                'fase':               'vender_put',
                'premium_this_cycle': 0.0,
                'cost_basis':         None,
                'shares_held':        0,
            })
        else:
            # Call expired worthless — sell another
            gross = state.get('call_premium', 0) * 100
            state['premium_total']      = round(state.get('premium_total', 0) + gross, 2)
            state['premium_this_cycle'] = round(state.get('premium_this_cycle', 0) + gross, 2)
            state['ciclos']             = state.get('ciclos', 0) + 1
            state['fase']               = 'asignado'
            print(f'  CALL EXPIRED WORTHLESS — ${gross:.0f} premium kept | selling new call')
        return state

    state['ultimo_chequeo'] = dt.datetime.now().isoformat()
    state['nvda_price']     = price
    state['dte']            = dte
    return state


# ── Stage 2: Sell cash-secured put (after shares get called away) ─────────────

def fase_vender_put(state):
    price  = get_stock_price()
    strike = round_strike(price * (1 - PUT_OTM_PCT))   # 10% OTM
    expiry = target_expiry()
    dte    = (expiry - dt.date.today()).days

    print(f'NVDA @ ${price:.2f}')
    print(f'Target put: ${strike} | {expiry} ({dte} DTE)')

    if not can_afford_put(strike):
        print('Insufficient buying power — skipping this cycle')
        return state

    contract = find_contract(ContractType.PUT, strike, expiry)
    if not contract:
        print('No suitable put contract found — will retry')
        return state

    mid = get_option_mid(contract.symbol)
    if not mid or mid < 0.05:
        print(f'No valid premium for {contract.symbol}')
        return state

    limit_px = round(mid, 2)

    try:
        order = trading.submit_order(LimitOrderRequest(
            symbol=contract.symbol,
            qty=CONTRACTS,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_px,
        ))
        gross = limit_px * 100 * CONTRACTS
        print(f'\n  SOLD PUT:  {contract.symbol}')
        print(f'  Premium:   ${limit_px:.2f}/share | ${gross:.0f} total')
        print(f'  Strike:    ${contract.strike_price} | Expiry: {contract.expiration_date} ({dte} DTE)')
        print(f'  Breakeven: ${float(contract.strike_price) - limit_px:.2f}')

        state.update({
            'fase':               'put_vendida',
            'put_symbol':         contract.symbol,
            'put_strike':         float(contract.strike_price),
            'put_expiry':         str(contract.expiration_date),
            'put_premium':        limit_px,
            'put_order_id':       str(order.id),
            'put_sold_at':        dt.datetime.now().isoformat(),
            'entry_price':        price,
            'premium_this_cycle': round(state.get('premium_this_cycle', 0) + gross, 2),
        })
    except Exception as e:
        print(f'  [sell put error]: {e}')

    return state


# ── Stage 2 monitor ───────────────────────────────────────────────────────────

def fase_monitor_put(state):
    if check_early_close(state):
        state['fase'] = 'vender_put'
        return state

    put_symbol = state.get('put_symbol', '')
    put_strike = state.get('put_strike', 0.0)
    put_expiry = state.get('put_expiry', '')

    price    = get_stock_price()
    expiry_d = dt.date.fromisoformat(put_expiry) if put_expiry else dt.date.today()
    dte      = (expiry_d - dt.date.today()).days
    otm_pct  = (price - put_strike) / price * 100

    print(f'NVDA @ ${price:.2f} | Short put ${put_strike} | {dte} DTE | {otm_pct:.1f}% OTM')

    put_qty = get_position(put_symbol)

    if dte <= 0 or put_qty == 0:
        nvda_qty = get_position(SYMBOL)
        if nvda_qty >= 100:
            # Assigned — update cost basis with all premiums received
            gross      = state.get('put_premium', 0) * 100
            cost_basis = put_strike - (state.get('premium_this_cycle', gross) / 100)
            state.update({
                'fase':            'asignado',
                'assignment_price': put_strike,
                'shares_held':     nvda_qty,
                'cost_basis':      round(cost_basis, 2),
            })
            print(f'  ASSIGNED at ${put_strike} | Effective cost basis: ${cost_basis:.2f}')
        else:
            # Expired worthless
            gross = state.get('put_premium', 0) * 100
            state['premium_total']      = round(state.get('premium_total', 0) + gross, 2)
            state['premium_this_cycle'] = round(state.get('premium_this_cycle', 0) + gross, 2)
            state['ciclos']             = state.get('ciclos', 0) + 1
            state['fase']               = 'vender_put'
            print(f'  PUT EXPIRED WORTHLESS — ${gross:.0f} profit | cycle #{state["ciclos"]}')
        return state

    if dte <= 5 and price <= put_strike * 1.02:
        print(f'  WARNING: {dte} DTE and near/below strike — assignment likely')

    state['ultimo_chequeo'] = dt.datetime.now().isoformat()
    state['nvda_price']     = price
    state['dte']            = dte
    return state


# ── Daily summary ─────────────────────────────────────────────────────────────

def generate_daily_summary(state):
    price = get_stock_price()
    try:
        acct         = get_account()
        equity       = float(acct.equity)
        cash         = float(acct.cash)
        buying_power = float(acct.buying_power)
    except Exception:
        equity = cash = buying_power = 0

    nvda_qty   = get_position(SYMBOL)
    cost_basis = state.get('cost_basis')
    stock_pnl  = (price - cost_basis) * nvda_qty if cost_basis and nvda_qty else 0

    summary = {
        'date':                    dt.date.today().isoformat(),
        'time_et':                 now_et().strftime('%H:%M'),
        'nvda_price':              f'${price:.2f}',
        '── WHEEL STATUS ──':      '─' * 30,
        'fase':                    state.get('fase'),
        'ciclos_completados':      state.get('ciclos', 0),
        'premium_total_all_time':  f"${state.get('premium_total', 0):.2f}",
        'premium_this_cycle':      f"${state.get('premium_this_cycle', 0):.2f}",
        '── POSITIONS ──':         '─' * 30,
        'nvda_shares':             nvda_qty,
        'cost_basis':              f'${cost_basis:.2f}' if cost_basis else 'N/A',
        'stock_pnl':               f'${stock_pnl:.2f}',
        'open_put':                (state.get('put_symbol', 'None')
                                    if state.get('fase') == 'put_vendida' else 'None'),
        'open_call':               (state.get('call_symbol', 'None')
                                    if state.get('fase') == 'call_vendida' else 'None'),
        'dte_remaining':           state.get('dte', 'N/A'),
        '── ACCOUNT ──':           '─' * 30,
        'equity':                  f'${equity:,.2f}',
        'cash':                    f'${cash:,.2f}',
        'buying_power':            f'${buying_power:,.2f}',
        '── TOTAL RETURN ──':      '─' * 30,
        'total_return':            f"${state.get('premium_total', 0) + stock_pnl:.2f}",
        'initial_investment':      f'${BUDGET:,.0f}',
        'return_pct':              (f"{((state.get('premium_total', 0) + stock_pnl) / BUDGET * 100):.2f}%"
                                    if BUDGET else 'N/A'),
    }

    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2)

    print('\n' + '═' * 52)
    print('  DAILY SUMMARY — WHEEL STRATEGY NVDA ($10,000)')
    print('═' * 52)
    for k, v in summary.items():
        if '──' not in str(k):
            print(f'  {k:<32} {v}')
    print('═' * 52)


# ── Entry point ───────────────────────────────────────────────────────────────

if not is_market_hours():
    print(f'[{dt.datetime.now().isoformat()}] Outside market hours — exiting')
    exit(0)

state = load_state()
fase  = state.get('fase', 'iniciar')

print(f'[{now_et().strftime("%Y-%m-%d %H:%M ET")}]  WHEEL STRATEGY — NVDA ($10,000)')
print(f'Fase: {fase} | Ciclos: {state.get("ciclos", 0)} | Premium total: ${state.get("premium_total", 0):.2f}')
print('─' * 60)

if fase == 'iniciar':
    state = fase_iniciar(state)
elif fase == 'asignado':
    state = fase_vender_call(state)
elif fase == 'call_vendida':
    state = fase_monitor_call(state)
elif fase == 'vender_put':
    state = fase_vender_put(state)
elif fase == 'put_vendida':
    state = fase_monitor_put(state)
else:
    print(f'Unknown fase: {fase}')

if is_close_window():
    generate_daily_summary(state)

save_state(state)
