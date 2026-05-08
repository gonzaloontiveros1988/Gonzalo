import os
import json
import sys
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
    TrailingStopOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

SYMBOL        = 'TSLA'
STATE_FILE    = 'Trading/estado_tsla.json'

# ── Position sizing ───────────────────────────────────────────────────────────
INITIAL_QTY   = 10

# Ladder levels — sized larger the deeper we go (more conviction at lower prices)
# -15%: catches normal TSLA pullbacks (frequent)
# -25%: medium correction
# -40%: major crash / max deployment
LADDERS = [
    {'drop': 0.15, 'qty': 15, 'key': 'ladder1'},
    {'drop': 0.25, 'qty': 25, 'key': 'ladder2'},
    {'drop': 0.40, 'qty': 35, 'key': 'ladder3'},
]

# ── Risk parameters ───────────────────────────────────────────────────────────
STOP_PCT      = 0.10   # -10% stop loss from average cost
TRAIL_PCT     = 5.0    # trailing % once activated
TRAIL_TRIGGER = 0.10   # activate trailing at +10% from entry


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {'modo': 'espera'}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def get_price():
    q = data_client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=SYMBOL)
    )
    return float(q[SYMBOL].ask_price)


def cancel_order(order_id):
    if not order_id:
        return
    try:
        trading.cancel_order_by_id(order_id)
    except Exception:
        pass


def get_order(order_id):
    try:
        return trading.get_order_by_id(order_id)
    except Exception:
        return None


def place_stop(qty, stop_price):
    return trading.submit_order(StopOrderRequest(
        symbol=SYMBOL,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=round(stop_price, 2)
    ))


def place_trailing(qty):
    return trading.submit_order(TrailingStopOrderRequest(
        symbol=SYMBOL,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=TRAIL_PCT
    ))


def cancelar_ordenes_abiertas():
    try:
        abiertas = trading.get_orders(GetOrdersRequest(
            symbol=SYMBOL, status=QueryOrderStatus.OPEN
        ))
        for o in abiertas:
            cancel_order(str(o.id))
        if abiertas:
            print(f'Canceladas {len(abiertas)} ordenes abiertas de {SYMBOL}')
    except Exception as e:
        print(f'Error cancelando ordenes: {e}')


def place_ladders(entry_price):
    """Place all ladder limit buy orders. Returns list of order dicts."""
    placed = []
    for l in LADDERS:
        price = round(entry_price * (1 - l['drop']), 2)
        order = trading.submit_order(LimitOrderRequest(
            symbol=SYMBOL,
            qty=l['qty'],
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=price
        ))
        placed.append({
            'key':      l['key'],
            'drop_pct': l['drop'],
            'price':    price,
            'qty':      l['qty'],
            'order_id': str(order.id),
            'done':     False
        })
        print(f"  {l['key']}: compra {l['qty']} acciones a ${price} (-{int(l['drop']*100)}%)")
    return placed


def calc_avg_cost(state):
    """Weighted average cost across entry + any filled ladders."""
    total_cost   = state['entry_price'] * INITIAL_QTY
    total_shares = INITIAL_QTY
    for l in state.get('ladders', []):
        if l['done']:
            total_cost   += l['price'] * l['qty']
            total_shares += l['qty']
    return total_cost / total_shares if total_shares else state['entry_price']


# ── Modes ─────────────────────────────────────────────────────────────────────

def iniciar():
    cancelar_ordenes_abiertas()

    price      = get_price()
    stop_price = price * (1 - STOP_PCT)
    fake_tp    = round(price * 3.0, 2)

    # Bracket order: buy + stop loss + take_profit (Alpaca requires both legs)
    bracket = trading.submit_order(MarketOrderRequest(
        symbol=SYMBOL,
        qty=INITIAL_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_loss={'stop_price': round(stop_price, 2)},
        take_profit={'limit_price': fake_tp}
    ))

    stop_leg_id = None
    tp_leg_id   = None
    if bracket.legs:
        for leg in bracket.legs:
            if leg.side == OrderSide.SELL:
                detail = get_order(str(leg.id))
                if detail and getattr(detail, 'stop_price', None):
                    stop_leg_id = str(leg.id)
                else:
                    tp_leg_id = str(leg.id)

    print('Colocando ladders:')
    ladders = place_ladders(price)

    state = {
        'modo':                  'activo',
        'symbol':                SYMBOL,
        'entry_price':           round(price, 2),
        'total_shares':          INITIAL_QTY,
        'stop_price':            round(stop_price, 2),
        'stop_order_id':         stop_leg_id,
        'tp_order_id':           tp_leg_id,
        'bracket_order_id':      str(bracket.id),
        'trailing_active':       False,
        'trailing_trigger_price': round(price * (1 + TRAIL_TRIGGER), 2),
        'ladders':               ladders,
        'inicio':                datetime.now().isoformat()
    }
    save_state(state)

    print(json.dumps({
        'ESTRATEGIA TSLA INICIADA': True,
        'precio_entrada': round(price, 2),
        'ordenes': [
            {'N': 1, 'tipo': 'COMPRA MERCADO',    'qty': INITIAL_QTY,    'precio': round(price, 2),      'id': str(bracket.id)},
            {'N': 2, 'tipo': 'STOP LOSS -10%',    'stop': round(stop_price, 2),                           'id': stop_leg_id},
            {'N': 3, 'tipo': 'TRAILING +10%→5%',  'activa_en': round(price * 1.10, 2),                   'id': '(pendiente)'},
        ] + [
            {'N': 4+i, 'tipo': f'LADDER {i+1} -{int(l["drop_pct"]*100)}%',
             'qty': l['qty'], 'precio': l['price'], 'id': l['order_id']}
            for i, l in enumerate(ladders)
        ],
        'nota': 'Stop recalcula desde coste medio cuando se ejecutan los ladders.'
    }, indent=2, ensure_ascii=False))


def actualizar_ladders(state):
    """Cancel existing ladder orders and replace with updated levels."""
    entry = state['entry_price']

    print('Cancelando ladders existentes...')
    for l in state.get('ladders', []):
        if not l.get('done'):
            cancel_order(l.get('order_id'))

    print('Colocando nuevos ladders:')
    new_ladders = place_ladders(entry)

    state['ladders'] = new_ladders
    state['modo']    = 'activo'
    save_state(state)

    print(json.dumps({
        'LADDERS ACTUALIZADOS': True,
        'entry_price': entry,
        'nuevos_ladders': new_ladders
    }, indent=2))


def monitorear(state):
    price  = get_price()
    entry  = state['entry_price']
    change = (price - entry) / entry * 100
    updates = []

    # ── Trailing stop activation at +10% ─────────────────────────────────────
    if not state['trailing_active'] and change >= TRAIL_TRIGGER * 100:
        cancel_order(state.get('stop_order_id'))
        cancel_order(state.get('tp_order_id'))
        trail = place_trailing(state['total_shares'])
        state['trailing_active']   = True
        state['stop_order_id']     = str(trail.id)
        state['trailing_order_id'] = str(trail.id)
        updates.append(f'Trailing stop activado (+{change:.1f}%), trail 5% desde maximo')

    # ── Ladder monitoring ─────────────────────────────────────────────────────
    for i, l in enumerate(state.get('ladders', [])):
        if l.get('done'):
            continue
        order = get_order(l.get('order_id'))
        if not (order and order.status == OrderStatus.FILLED):
            continue

        l['done']              = True
        state['total_shares'] += l['qty']
        updates.append(f"{l['key']}: +{l['qty']} acciones a ${l['price']}")

        # Recalculate stop from new average cost
        cancel_order(state.get('stop_order_id'))
        avg = calc_avg_cost(state)
        if state['trailing_active']:
            trail = place_trailing(state['total_shares'])
            state['stop_order_id'] = str(trail.id)
        else:
            new_stop = avg * (1 - STOP_PCT)
            stop = place_stop(state['total_shares'], new_stop)
            state['stop_price']    = round(new_stop, 2)
            state['stop_order_id'] = str(stop.id)
            updates.append(f'Stop recalculado a ${round(new_stop, 2)} (coste medio: ${round(avg, 2)})')

    state['precio_actual']  = round(price, 2)
    state['cambio_pct']     = round(change, 2)
    state['ultimo_chequeo'] = datetime.now().isoformat()
    save_state(state)

    print(json.dumps({
        'precio_actual':   round(price, 2),
        'entry_price':     entry,
        'cambio_pct':      f'{round(change, 2)}%',
        'total_shares':    state['total_shares'],
        'stop_price':      state.get('stop_price'),
        'trailing_active': state['trailing_active'],
        'ladders': [
            {'key': l['key'], 'price': l['price'], 'qty': l['qty'], 'done': l['done']}
            for l in state.get('ladders', [])
        ],
        'updates': updates
    }, indent=2))


# ── Entry point ───────────────────────────────────────────────────────────────
state = load_state()
modo  = state.get('modo', 'espera')

if modo == 'espera':
    print('Estrategia en espera.')
    sys.exit(0)
elif modo == 'iniciar':
    iniciar()
elif modo == 'activo':
    monitorear(state)
elif modo == 'actualizar_ladders':
    actualizar_ladders(state)
else:
    print(f'Modo desconocido: {modo}')
    sys.exit(1)
