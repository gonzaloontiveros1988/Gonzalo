"""
Wheel Strategy — TSLA
=====================
Stage 1: Sell cash-secured put  → 10% OTM, 2-4 weeks out
Stage 2: Sell covered call      → 10% above cost basis, 2-4 weeks out

Rules enforced:
- Never sell put without enough cash to buy shares if assigned
- Never sell call below cost basis (purchase price minus all premiums)
- Close any position early at 50% profit, immediately re-sell
- Track total premium across all cycles
- Daily summary at market close
- Do nothing outside market hours
"""

import os
import json
import math
import datetime as dt
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, ContractType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, OptionLatestQuoteRequest

# ── Parameters ────────────────────────────────────────────────────────────────
SYMBOL        = 'TSLA'
CONTRACTS     = 1          # 1 contract = 100 shares
PUT_OTM_PCT   = 0.10       # sell put 10% below current price
CALL_OTM_PCT  = 0.10       # sell call 10% above cost basis
DTE_MIN       = 14         # minimum DTE (2 weeks)
DTE_MAX       = 28         # maximum DTE (4 weeks)
EARLY_CLOSE   = 0.50       # close at 50% profit

STATE_FILE   = 'Wheel/estado_rueda.json'
SUMMARY_FILE = 'Wheel/resumen_diario.json'

# ── Clients ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
stock_data  = StockHistoricalDataClient(API_KEY, API_SECRET)
option_data = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ── Time helpers ──────────────────────────────────────────────────────────────

def now_et():
    """Current time in ET (UTC-4 during EDT, UTC-5 during EST)."""
    utc = dt.datetime.utcnow()
    # May = EDT = UTC-4
    offset = -4
    return utc + dt.timedelta(hours=offset)

def is_market_hours():
    t = now_et()
    if t.weekday() >= 5:        # Saturday / Sunday
        return False
    market_open  = t.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = t.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= t <= market_close

def is_close_window():
    """True during the 3:45–4:00 PM ET window for daily summary."""
    t = now_et()
    return t.weekday() < 5 and t.hour == 15 and t.minute >= 45


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            'fase':              'vender_put',
            'ciclos':            0,
            'premium_total':     0.0,   # all-time premium collected ($)
            'premium_this_cycle': 0.0,  # premium in current put→call cycle
            'cost_basis':        None,  # effective cost per share after premiums
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
    """Find the first Friday that falls in the DTE_MIN–DTE_MAX window."""
    today = dt.date.today()
    target_mid = today + dt.timedelta(days=(DTE_MIN + DTE_MAX) // 2)  # ~21 days
    # Step forward to the nearest Friday
    days_ahead = (4 - target_mid.weekday()) % 7
    expiry = target_mid + dt.timedelta(days=days_ahead)
    # Ensure we stay inside the window
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
    """Rule: never sell a put without enough cash to buy the shares."""
    try:
        acct = get_account()
        buying_power = float(acct.buying_power)
        needed = strike * 100 * CONTRACTS
        ok = buying_power >= needed
        print(f'  Cash check: need ${needed:,.0f} | have ${buying_power:,.0f} → {"OK" if ok else "INSUFFICIENT"}')
        return ok
    except Exception as e:
        print(f'  [cash check error]: {e}')
        return True  # allow if check fails in paper mode


# ── Early close at 50% profit ─────────────────────────────────────────────────

def check_early_close(state):
    """
    If the current short option is worth <= 50% of what we sold it for,
    buy it back and return True so we re-sell a fresh one.
    """
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
            # Buy to close
            trading.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=CONTRACTS,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(current * 1.05, 2),  # slight buffer to ensure fill
            ))
            # Book the profit
            profit = (sold_for - current) * 100 * CONTRACTS
            state['premium_total']      = round(state.get('premium_total', 0) + profit, 2)
            state['premium_this_cycle'] = round(state.get('premium_this_cycle', 0) + profit, 2)
            print(f'  Closed for ${profit:.2f} profit')
        except Exception as e:
            print(f'  [close error]: {e}')
            return False
        return True

    return False


# ── Stage 1: Sell cash-secured put ───────────────────────────────────────────

def fase_vender_put(state):
    price  = get_stock_price()
    strike = round_strike(price * (1 - PUT_OTM_PCT))   # 10% OTM
    expiry = target_expiry()
    dte    = (expiry - dt.date.today()).days

    print(f'TSLA @ ${price:.2f}')
    print(f'Target put: ${strike} strike | {expiry} ({dte} DTE, {DTE_MIN}-{DTE_MAX} range)')

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
        print(f'  Premium:   ${limit_px:.2f}/share  |  ${gross:.0f} total')
        print(f'  Strike:    ${contract.strike_price}  |  Expiry: {contract.expiration_date} ({dte} DTE)')
        print(f'  Breakeven: ${float(contract.strike_price) - limit_px:.2f}')
        print(f'  Max gain:  ${gross:.0f}  |  Obligation: buy 100 shares @ ${contract.strike_price}')

        state.update({
            'fase':              'put_vendida',
            'put_symbol':        contract.symbol,
            'put_strike':        float(contract.strike_price),
            'put_expiry':        str(contract.expiration_date),
            'put_premium':       limit_px,
            'put_order_id':      str(order.id),
            'put_sold_at':       dt.datetime.now().isoformat(),
            'entry_price':       price,
            'premium_this_cycle': round(state.get('premium_this_cycle', 0) + gross, 2),
        })
    except Exception as e:
        print(f'  [sell put error]: {e}')

    return state


# ── Stage 1 monitor ───────────────────────────────────────────────────────────

def fase_monitor_put(state):
    # Check for 50% profit early close
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

    print(f'TSLA @ ${price:.2f} | Short put ${put_strike} | {dte} DTE | {otm_pct:.1f}% OTM')

    put_qty = get_position(put_symbol)

    if dte <= 0 or put_qty == 0:
        tsla_qty = get_position(SYMBOL)
        if tsla_qty >= 100:
            # Assigned
            gross      = state.get('put_premium', 0) * 100
            cost_basis = put_strike - (state.get('premium_this_cycle', gross) / 100)
            state.update({
                'fase':            'asignado',
                'assignment_price': put_strike,
                'shares_held':     tsla_qty,
                'cost_basis':      round(cost_basis, 2),
            })
            print(f'  ASSIGNED at ${put_strike}')
            print(f'  Effective cost basis: ${cost_basis:.2f}/share (after premiums)')
        else:
            # Expired worthless
            gross = state.get('put_premium', 0) * 100
            state['premium_total'] = round(state.get('premium_total', 0) + gross, 2)
            state['ciclos']        = state.get('ciclos', 0) + 1
            state['fase']          = 'vender_put'
            print(f'  PUT EXPIRED WORTHLESS — ${gross:.0f} profit')
            print(f'  Cycle #{state["ciclos"]} done | All-time premium: ${state["premium_total"]:.2f}')
        return state

    if dte <= 5 and price <= put_strike * 1.02:
        print(f'  WARNING: {dte} DTE and near/below strike — assignment possible')

    state['ultimo_chequeo'] = dt.datetime.now().isoformat()
    state['tsla_price']     = price
    state['dte']            = dte
    return state


# ── Stage 2: Sell covered call ────────────────────────────────────────────────

def fase_vender_call(state):
    cost_basis       = state.get('cost_basis', state.get('assignment_price', 0))
    premium_per_share = state.get('premium_this_cycle', 0) / 100
    effective_cost   = cost_basis  # already adjusted in assignment

    price  = get_stock_price()
    expiry = target_expiry()
    dte    = (expiry - dt.date.today()).days

    # Rule: never sell call below cost basis
    target_strike = round_strike(max(
        price * (1 + CALL_OTM_PCT),         # 10% OTM from current price
        effective_cost * 1.005              # at least 0.5% above cost basis
    ))

    print(f'TSLA @ ${price:.2f} | Cost basis: ${effective_cost:.2f}')
    print(f'Target call: ${target_strike} strike | {expiry} ({dte} DTE)')
    print(f'  (Must stay above cost basis ${effective_cost:.2f} ✓)' if target_strike >= effective_cost
          else f'  WARNING: strike below cost basis — adjusting up')

    contract = find_contract(ContractType.CALL, target_strike, expiry)
    if not contract:
        print('No suitable call contract found — will retry')
        return state

    # Hard safety: never below cost basis
    if float(contract.strike_price) < effective_cost:
        print(f'  BLOCKED: strike ${contract.strike_price} < cost basis ${effective_cost:.2f}')
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
        total_cycle = state.get('premium_this_cycle', 0) + gross
        print(f'\n  SOLD CALL: {contract.symbol}')
        print(f'  Premium:   ${limit_px:.2f}/share  |  ${gross:.0f} total')
        print(f'  Strike:    ${contract.strike_price}  |  Expiry: {contract.expiration_date} ({dte} DTE)')
        print(f'  If called: profit = ${(float(contract.strike_price) - effective_cost)*100:.0f} stock + ${total_cycle:.0f} premium')

        state.update({
            'fase':              'call_vendida',
            'call_symbol':       contract.symbol,
            'call_strike':       float(contract.strike_price),
            'call_expiry':       str(contract.expiration_date),
            'call_premium':      limit_px,
            'call_order_id':     str(order.id),
            'call_sold_at':      dt.datetime.now().isoformat(),
            'premium_this_cycle': round(total_cycle, 2),
        })
    except Exception as e:
        print(f'  [sell call error]: {e}')

    return state


# ── Stage 2 monitor ───────────────────────────────────────────────────────────

def fase_monitor_call(state):
    # Check for 50% profit early close
    if check_early_close(state):
        state['fase'] = 'asignado'   # re-sell new call
        return state

    call_symbol  = state.get('call_symbol', '')
    call_strike  = state.get('call_strike', 0.0)
    call_expiry  = state.get('call_expiry', '')
    cost_basis   = state.get('cost_basis', 0.0)

    price    = get_stock_price()
    expiry_d = dt.date.fromisoformat(call_expiry) if call_expiry else dt.date.today()
    dte      = (expiry_d - dt.date.today()).days
    pnl      = price - cost_basis

    print(f'TSLA @ ${price:.2f} | Short call ${call_strike} | {dte} DTE | Stock PnL: ${pnl:.2f}/sh')

    call_qty = get_position(call_symbol)

    if dte <= 0 or call_qty == 0:
        tsla_qty = get_position(SYMBOL)
        if tsla_qty < 100:
            # Shares called away
            stock_profit   = (call_strike - cost_basis) * 100 * CONTRACTS
            total_premium  = state.get('premium_this_cycle', 0)
            state['premium_total'] = round(state.get('premium_total', 0) + total_premium, 2)
            state['ciclos']        = state.get('ciclos', 0) + 1
            print(f'\n  SHARES CALLED AWAY at ${call_strike}')
            print(f'  Stock profit:    ${stock_profit:.0f}')
            print(f'  Cycle premium:   ${total_premium:.0f}')
            print(f'  Cycle total:     ${stock_profit + total_premium:.0f}')
            print(f'  All-time premium: ${state["premium_total"]:.2f}')
            state.update({
                'fase':              'vender_put',
                'premium_this_cycle': 0.0,
                'cost_basis':        None,
            })
        else:
            # Call expired worthless — sell another
            gross = state.get('call_premium', 0) * 100
            state['premium_total']      = round(state.get('premium_total', 0) + gross, 2)
            state['premium_this_cycle'] = round(state.get('premium_this_cycle', 0) + gross, 2)
            state['fase']               = 'asignado'
            print(f'  CALL EXPIRED WORTHLESS — ${gross:.0f} profit | selling new call')
        return state

    state['ultimo_chequeo'] = dt.datetime.now().isoformat()
    state['tsla_price']     = price
    state['dte']            = dte
    return state


# ── Daily summary ─────────────────────────────────────────────────────────────

def generate_daily_summary(state):
    price = get_stock_price()
    try:
        acct = get_account()
        equity        = float(acct.equity)
        cash          = float(acct.cash)
        buying_power  = float(acct.buying_power)
    except Exception:
        equity = cash = buying_power = 0

    tsla_qty  = get_position(SYMBOL)
    cost_basis = state.get('cost_basis')
    stock_pnl = (price - cost_basis) * tsla_qty if cost_basis and tsla_qty else 0

    summary = {
        'date':              dt.date.today().isoformat(),
        'time_et':           now_et().strftime('%H:%M'),
        'tsla_price':        price,
        '── WHEEL STATUS ──': '─' * 30,
        'fase':              state.get('fase'),
        'ciclos_completados': state.get('ciclos', 0),
        'premium_total_all_time': f"${state.get('premium_total', 0):.2f}",
        'premium_this_cycle':     f"${state.get('premium_this_cycle', 0):.2f}",
        '── POSITIONS ──':   '─' * 30,
        'tsla_shares':       tsla_qty,
        'cost_basis':        f"${cost_basis:.2f}" if cost_basis else 'N/A',
        'stock_pnl':         f"${stock_pnl:.2f}",
        'open_put':          state.get('put_symbol', 'None') if state.get('fase') == 'put_vendida' else 'None',
        'open_call':         state.get('call_symbol', 'None') if state.get('fase') == 'call_vendida' else 'None',
        'dte_remaining':     state.get('dte', 'N/A'),
        '── ACCOUNT ──':     '─' * 30,
        'equity':            f"${equity:,.2f}",
        'cash':              f"${cash:,.2f}",
        'buying_power':      f"${buying_power:,.2f}",
        '── TOTAL RETURN ──': '─' * 30,
        'total_return':      f"${state.get('premium_total', 0) + stock_pnl:.2f}",
    }

    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2)

    print('\n' + '═' * 50)
    print('  DAILY SUMMARY — WHEEL STRATEGY TSLA')
    print('═' * 50)
    for k, v in summary.items():
        if '──' not in str(k):
            print(f'  {k:<30} {v}')
    print('═' * 50)

    return summary


# ── Entry point ───────────────────────────────────────────────────────────────

if not is_market_hours():
    print(f'[{dt.datetime.now().isoformat()}] Outside market hours — exiting')
    exit(0)

state = load_state()
fase  = state.get('fase', 'vender_put')

print(f'[{now_et().strftime("%Y-%m-%d %H:%M ET")}]  WHEEL STRATEGY — TSLA')
print(f'Fase: {fase} | Ciclos: {state.get("ciclos",0)} | Premium total: ${state.get("premium_total",0):.2f}')
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

# Daily summary at market close window (3:45-4:00 PM ET)
if is_close_window():
    generate_daily_summary(state)

save_state(state)
