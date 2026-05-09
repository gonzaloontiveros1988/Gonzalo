"""
Estrategia RSI + OBV + VWAP — APH y ZTS
=========================================
Señal LONG cuando se cumplen las 3 condiciones:
  1. RSI(14) cruza por encima de 30 (zona de sobreventa)
  2. OBV alcista (subiendo en las últimas barras)
  3. Precio por debajo del VWAP (infravalorado)

Gestión de riesgo:
  - Stop Loss:   2× ATR por debajo de la entrada
  - Take Profit: 4× ATR por encima de la entrada (RR 1:2)
  - Riesgo máx:  3% del equity por operación

Timeframe: barras de 1 hora
Chequeo:   cada 15 minutos durante horario de mercado
"""

import os
import json
import datetime as dt
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, GetOrdersRequest, ClosePositionRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ── Parámetros ────────────────────────────────────────────────────────────────
SYMBOLS      = ['APH', 'ZTS']
RSI_PERIOD   = 14
RSI_SIGNAL   = 30       # cruce por encima de este nivel activa la señal
ATR_PERIOD   = 14
STOP_MULT    = 2.0      # stop = 2× ATR
TP_MULT      = 4.0      # take profit = 4× ATR
RISK_PCT     = 0.03     # 3% del equity por operación
BARS_NEEDED  = 80       # barras históricas para calcular indicadores

STATE_FILE   = 'RSI/estado_rsi.json'
LOG_FILE     = 'RSI/log_rsi.json'

# ── Clientes ──────────────────────────────────────────────────────────────────
API_KEY    = os.environ['APCA_API_KEY_ID']
API_SECRET = os.environ['APCA_API_SECRET_KEY']

trading    = TradingClient(API_KEY, API_SECRET, paper=True)
stock_data = StockHistoricalDataClient(API_KEY, API_SECRET)


# ── Horario ───────────────────────────────────────────────────────────────────

def now_et():
    return dt.datetime.utcnow() + dt.timedelta(hours=-4)  # EDT

def is_market_hours():
    t = now_et()
    if t.weekday() >= 5:
        return False
    open_  = t.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = t.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_ <= t <= close_


# ── Estado ────────────────────────────────────────────────────────────────────

def load_state():
    default = {sym: {
        'posicion_abierta': False,
        'entry_price': None,
        'stop':        None,
        'tp':          None,
        'shares':      0,
        'order_id':    None,
        'ultimo_rsi':  None,
    } for sym in SYMBOLS}
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for sym in SYMBOLS:
            if sym not in saved:
                saved[sym] = default[sym]
        return saved
    except FileNotFoundError:
        return default

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


# ── Datos históricos ──────────────────────────────────────────────────────────

def get_bars(symbol):
    """Obtiene las últimas BARS_NEEDED barras de 1H para el símbolo."""
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)
    try:
        result = stock_data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
        ))
        bars = result[symbol]
        if not bars or len(bars) < RSI_PERIOD + 5:
            print(f'  [datos insuficientes] {symbol}: {len(bars) if bars else 0} barras')
            return None

        closes  = np.array([float(b.close)  for b in bars])
        highs   = np.array([float(b.high)   for b in bars])
        lows    = np.array([float(b.low)    for b in bars])
        volumes = np.array([float(b.volume) for b in bars])
        return closes, highs, lows, volumes

    except Exception as e:
        print(f'  [error datos] {symbol}: {e}')
        return None


# ── Indicadores ───────────────────────────────────────────────────────────────

def calc_rsi(closes, period=RSI_PERIOD):
    """RSI de Wilder sobre el array de cierres. Devuelve los últimos 2 valores."""
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_g = np.mean(gains[:period])
    avg_l = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period

    rsi_prev_g = (avg_g * (period - 1) + gains[-2]) / period if len(gains) >= 2 else avg_g
    rsi_prev_l = (avg_l * (period - 1) + losses[-2]) / period if len(losses) >= 2 else avg_l

    def rsi_from(g, l):
        if l == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + g / l))

    # Recalcular el penúltimo y último RSI con suavizado completo
    # Recorremos desde period hasta el final
    ag, al = np.mean(gains[:period]), np.mean(losses[:period])
    for i in range(period, len(gains) - 1):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    rsi_prev = rsi_from(ag, al)

    ag = (ag * (period - 1) + gains[-1]) / period
    al = (al * (period - 1) + losses[-1]) / period
    rsi_curr = rsi_from(ag, al)

    return rsi_prev, rsi_curr

def calc_atr(highs, lows, closes, period=ATR_PERIOD):
    """ATR de Wilder."""
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    tr = np.array(tr)
    atr = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr

def calc_vwap(highs, lows, closes, volumes):
    """VWAP rolling sobre todas las barras disponibles."""
    tp  = (highs + lows + closes) / 3.0
    return np.sum(tp * volumes) / np.sum(volumes)

def calc_obv(closes, volumes):
    """On-Balance Volume."""
    obv = np.zeros(len(closes))
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    return obv


# ── Cuenta ────────────────────────────────────────────────────────────────────

def get_equity():
    try:
        return float(trading.get_account().equity)
    except Exception as e:
        print(f'  [error cuenta]: {e}')
        return 0.0

def get_open_position(symbol):
    try:
        for pos in trading.get_all_positions():
            if pos.symbol == symbol:
                return int(float(pos.qty))
    except Exception:
        pass
    return 0


# ── Lógica principal por símbolo ──────────────────────────────────────────────

def analizar_simbolo(symbol, state_sym, equity):
    data = get_bars(symbol)
    if data is None:
        return state_sym

    closes, highs, lows, volumes = data
    precio  = closes[-1]
    atr     = calc_atr(highs, lows, closes)
    vwap    = calc_vwap(highs, lows, closes, volumes)
    obv     = calc_obv(closes, volumes)
    rsi_prev, rsi_curr = calc_rsi(closes)

    # OBV alcista: subiendo en las últimas 5 barras
    obv_alcista = bool(obv[-1] > obv[-6]) if len(obv) >= 6 else False

    print(f'\n  {symbol} @ ${precio:.2f}')
    print(f'  RSI: {rsi_prev:.1f} → {rsi_curr:.1f} | ATR: {atr:.2f} | VWAP: {vwap:.2f}')
    print(f'  OBV alcista: {"Sí" if obv_alcista else "No"} | Bajo VWAP: {"Sí" if precio < vwap else "No"}')

    state_sym['ultimo_rsi'] = round(rsi_curr, 2)

    # ── Verificar si posición existente fue cerrada por SL o TP ──────────────
    if state_sym['posicion_abierta']:
        shares_en_cuenta = get_open_position(symbol)
        if shares_en_cuenta == 0:
            pnl = (precio - state_sym['entry_price']) * state_sym['shares']
            print(f'  Posición {symbol} cerrada | PnL estimado: ${pnl:.2f}')
            state_sym.update({
                'posicion_abierta': False,
                'entry_price': None,
                'stop': None,
                'tp': None,
                'shares': 0,
                'order_id': None,
            })
        else:
            print(f'  Posición abierta: {shares_en_cuenta} acciones @ ${state_sym["entry_price"]:.2f}')
            print(f'  Stop: ${state_sym["stop"]:.2f} | TP: ${state_sym["tp"]:.2f}')
            return state_sym  # ya en posición, no hacer nada más

    # ── Verificar señal de entrada ────────────────────────────────────────────
    rsi_cruza_30  = rsi_prev < RSI_SIGNAL and rsi_curr >= RSI_SIGNAL
    precio_bajo_vwap = precio < vwap
    condiciones = rsi_cruza_30 and obv_alcista and precio_bajo_vwap

    print(f'  Señal: RSI cruza 30={rsi_cruza_30} | OBV↑={obv_alcista} | <VWAP={precio_bajo_vwap}')

    if not condiciones:
        print(f'  → Sin señal. Esperando condiciones...')
        return state_sym

    # ── Calcular orden ────────────────────────────────────────────────────────
    stop_dist   = STOP_MULT * atr
    tp_dist     = TP_MULT * atr
    stop_price  = round(precio - stop_dist, 2)
    tp_price    = round(precio + tp_dist,   2)
    risk_amount = equity * RISK_PCT
    shares      = max(1, int(risk_amount / stop_dist))

    print(f'\n  ★ SEÑAL LONG ACTIVA — {symbol}')
    print(f'  Precio entrada: ~${precio:.2f}')
    print(f'  Stop Loss:       ${stop_price:.2f}  (-{stop_dist:.2f}, 2×ATR)')
    print(f'  Take Profit:     ${tp_price:.2f}  (+{tp_dist:.2f}, 4×ATR)')
    print(f'  Acciones:        {shares}  (riesgo ${risk_amount:.0f} = {RISK_PCT*100}% de ${equity:,.0f})')
    print(f'  RR:              1:2 ({tp_dist/stop_dist:.1f}×)')

    try:
        # Entrada con limit ligeramente por encima para asegurar fill
        entry_limit = round(precio * 1.002, 2)

        order = trading.submit_order(LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=entry_limit,
            order_class='bracket',
            stop_loss={'stop_price': stop_price},
            take_profit={'limit_price': tp_price},
        ))
        print(f'  Orden enviada: {order.id}')

        state_sym.update({
            'posicion_abierta': True,
            'entry_price':      round(precio, 2),
            'stop':             stop_price,
            'tp':               tp_price,
            'shares':           shares,
            'order_id':         str(order.id),
            'entrada_ts':       dt.datetime.now().isoformat(),
        })

    except Exception as e:
        print(f'  [error orden] {symbol}: {e}')

    return state_sym


# ── Entry point ───────────────────────────────────────────────────────────────

if not is_market_hours():
    print(f'[{dt.datetime.now().isoformat()}] Fuera de horario de mercado — saliendo')
    exit(0)

state  = load_state()
equity = get_equity()

print(f'[{now_et().strftime("%Y-%m-%d %H:%M ET")}]  ESTRATEGIA RSI + OBV + VWAP')
print(f'Symbols: {", ".join(SYMBOLS)} | Equity: ${equity:,.2f}')
print('─' * 60)

for sym in SYMBOLS:
    state[sym] = analizar_simbolo(sym, state.get(sym, {}), equity)

save_state(state)
print('\nEstado guardado.')
