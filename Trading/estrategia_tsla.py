import os
import json
import sys
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
    TrailingStopOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading     = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

SYMBOL         = 'TSLA'
STATE_FILE     = 'Trading/estado_tsla.json'
INITIAL_QTY    = 10
LADDER1_QTY    = 20
LADDER2_QTY    = 20
STOP_PCT       = 0.10   # -10% stop loss
TRAIL_PCT      = 5.0    # 5% trailing once activated
TRAIL_TRIGGER  = 0.10   # activate trailing at +10%
LADDER1_DROP   = 0.20   # ladder 1 at -20%
LADDER2_DROP   = 0.30   # ladder 2 at -30%


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


def iniciar():
    price = get_price()

    stop_price    = price * (1 - STOP_PCT)
    ladder1_price = price * (1 - LADDER1_DROP)
    ladder2_price = price * (1 - LADDER2_DROP)

    buy  = trading.submit_order(MarketOrderRequest(
        symbol=SYMBOL, qty=INITIAL_QTY,
        side=OrderSide.BUY, time_in_force=TimeInForce.DAY
    ))
    stop = place_stop(INITIAL_QTY, stop_price)
    l1   = trading.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=LADDER1_QTY, side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        limit_price=round(ladder1_price, 2)
    ))
    l2   = trading.submit_order(LimitOrderRequest(
        symbol=SYMBOL, qty=LADDER2_QTY, side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        limit_price=round(ladder2_price, 2)
    ))

    state = {
        'modo': 'activo',
        'symbol': SYMBOL,
        'entry_price': round(price, 2),
        'total_shares': INITIAL_QTY,
        'stop_price': round(stop_price, 2),
        'stop_order_id': str(stop.id),
        'trailing_active': False,
        'trailing_trigger_price': round(price * (1 + TRAIL_TRIGGER), 2),
        'ladder1_price': round(ladder1_price, 2),
        'ladder1_order_id': str(l1.id),
        'ladder1_done': False,
        'ladder2_price': round(ladder2_price, 2),
        'ladder2_order_id': str(l2.id),
        'ladder2_done': False,
        'buy_order_id': str(buy.id),
        'inicio': datetime.now().isoformat()
    }
    save_state(state)

    resumen = {
        'ESTRATEGIA TSLA INICIADA': True,
        'precio_entrada': round(price, 2),
        'ordenes': [
            {
                'N': 1,
                'tipo': 'COMPRA MERCADO',
                'accion': f'Compra {INITIAL_QTY} acciones TSLA a precio de mercado (~${round(price, 2)})',
                'orden_id': str(buy.id)
            },
            {
                'N': 2,
                'tipo': 'STOP LOSS (activo)',
                'accion': f'Vende {INITIAL_QTY} acciones si precio cae a ${round(stop_price, 2)} (-10%)',
                'stop_price': round(stop_price, 2),
                'orden_id': str(stop.id)
            },
            {
                'N': 3,
                'tipo': 'TRAILING STOP (pendiente)',
                'accion': f'Se activa cuando TSLA suba a ${round(price * 1.10, 2)} (+10%). '
                          f'El floor queda 5% por debajo del maximo y solo sube, nunca baja.',
                'activacion_en': f'${round(price * 1.10, 2)}'
            },
            {
                'N': 4,
                'tipo': 'LADDER 1 - COMPRA LIMITE',
                'accion': f'Compra {LADDER1_QTY} acciones adicionales si TSLA baja a ${round(ladder1_price, 2)} (-20%)',
                'precio': round(ladder1_price, 2),
                'orden_id': str(l1.id)
            },
            {
                'N': 5,
                'tipo': 'LADDER 2 - COMPRA LIMITE',
                'accion': f'Compra {LADDER2_QTY} acciones adicionales si TSLA baja a ${round(ladder2_price, 2)} (-30%)',
                'precio': round(ladder2_price, 2),
                'orden_id': str(l2.id)
            },
        ],
        'nota': 'El stop se recalcula desde el coste medio cuando se ejecutan los ladders.'
    }
    print(json.dumps(resumen, indent=2, ensure_ascii=False))
    return state


def monitorear(state):
    price  = get_price()
    entry  = state['entry_price']
    change = (price - entry) / entry * 100
    updates = []

    # Activate trailing stop at +10%
    if not state['trailing_active'] and change >= TRAIL_TRIGGER * 100:
        cancel_order(state['stop_order_id'])
        trail = place_trailing(state['total_shares'])
        state['trailing_active']  = True
        state['stop_order_id']    = str(trail.id)
        state['trailing_order_id'] = str(trail.id)
        updates.append(f'Trailing stop activado (+{change:.1f}%), trail 5% desde maximo')

    # Ladder 1
    if not state['ladder1_done']:
        order = get_order(state['ladder1_order_id'])
        if order and order.status == OrderStatus.FILLED:
            state['ladder1_done']  = True
            state['total_shares'] += LADDER1_QTY
            updates.append(f'Ladder 1 ejecutado: +{LADDER1_QTY} acciones a ${state["ladder1_price"]}')
            cancel_order(state['stop_order_id'])
            avg = (entry * INITIAL_QTY + state['ladder1_price'] * LADDER1_QTY) / state['total_shares']
            if state['trailing_active']:
                trail = place_trailing(state['total_shares'])
                state['stop_order_id'] = str(trail.id)
            else:
                new_stop = avg * (1 - STOP_PCT)
                stop = place_stop(state['total_shares'], new_stop)
                state['stop_price']    = round(new_stop, 2)
                state['stop_order_id'] = str(stop.id)

    # Ladder 2
    if not state['ladder2_done'] and state['ladder1_done']:
        order = get_order(state['ladder2_order_id'])
        if order and order.status == OrderStatus.FILLED:
            state['ladder2_done']  = True
            state['total_shares'] += LADDER2_QTY
            updates.append(f'Ladder 2 ejecutado: +{LADDER2_QTY} acciones a ${state["ladder2_price"]}')
            cancel_order(state['stop_order_id'])
            avg = (
                entry * INITIAL_QTY +
                state['ladder1_price'] * LADDER1_QTY +
                state['ladder2_price'] * LADDER2_QTY
            ) / state['total_shares']
            if state['trailing_active']:
                trail = place_trailing(state['total_shares'])
                state['stop_order_id'] = str(trail.id)
            else:
                new_stop = avg * (1 - STOP_PCT)
                stop = place_stop(state['total_shares'], new_stop)
                state['stop_price']    = round(new_stop, 2)
                state['stop_order_id'] = str(stop.id)

    state['precio_actual']   = round(price, 2)
    state['cambio_pct']      = round(change, 2)
    state['ultimo_chequeo']  = datetime.now().isoformat()
    save_state(state)

    print(json.dumps({
        'precio_actual':   round(price, 2),
        'entry_price':     entry,
        'cambio_pct':      round(change, 2),
        'total_shares':    state['total_shares'],
        'stop_price':      state.get('stop_price'),
        'trailing_active': state['trailing_active'],
        'updates':         updates
    }, indent=2))


# ── Entry point ──────────────────────────────────────────────────────────────
state = load_state()
modo  = state.get('modo', 'espera')

if modo == 'espera':
    print('Estrategia en espera. Cambiar modo a "iniciar" para comenzar.')
    sys.exit(0)
elif modo == 'iniciar':
    iniciar()
elif modo == 'activo':
    monitorear(state)
else:
    print(f'Modo desconocido: {modo}')
    sys.exit(1)
