import os
import json
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

client = TradingClient(
    os.environ['APCA_API_KEY_ID'],
    os.environ['APCA_API_SECRET_KEY'],
    paper=True
)

with open('Trading/comando.json') as f:
    cmd = json.load(f)

if cmd['accion'] == 'comprar' or cmd['accion'] == 'vender':
    orden = MarketOrderRequest(
        symbol=cmd['symbol'],
        qty=cmd['qty'],
        side=OrderSide.BUY if cmd['accion'] == 'comprar' else OrderSide.SELL,
        time_in_force=TimeInForce.DAY
    )
    resultado = client.submit_order(orden)
    salida = {
        'estado': 'ok',
        'id': str(resultado.id),
        'symbol': resultado.symbol,
        'qty': str(resultado.qty),
        'accion': cmd['accion'],
        'status': str(resultado.status),
        'timestamp': datetime.now().isoformat()
    }
elif cmd['accion'] == 'cuenta':
    acc = client.get_account()
    salida = {
        'estado': 'ok',
        'equity': str(acc.equity),
        'cash': str(acc.cash),
        'buying_power': str(acc.buying_power),
        'timestamp': datetime.now().isoformat()
    }

with open('Trading/resultado.json', 'w') as f:
    json.dump(salida, f, indent=2)

print(json.dumps(salida, indent=2))
