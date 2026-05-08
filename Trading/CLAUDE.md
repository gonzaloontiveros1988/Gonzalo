# Sistema de Trading Alpaca - Gonzalo

## Cómo funciona

Las órdenes se ejecutan mediante **GitHub Actions**. Para operar, modifico `comando.json` y hago push — el workflow se dispara automáticamente y ejecuta la orden en Alpaca.

## Credenciales

- Las credenciales de Alpaca están guardadas como **secrets del repositorio** en GitHub:
  - `APCA_API_KEY_ID`
  - `APCA_API_SECRET_KEY`
- Cuenta: **paper trading** (simulada) en `paper-api.alpaca.markets`

## Ejecutar una orden

Editar `comando.json` y hacer push a la rama `claude/alpaca-account-setup-zUFmC`:

```json
{
  "accion": "comprar",   // o "vender" o "cuenta"
  "symbol": "TSLA",
  "qty": 1,
  "id": 6
}
```

> Incrementar `id` cada vez para que git detecte el cambio aunque los demás campos sean iguales.

## Acciones disponibles

| accion    | descripción                        |
|-----------|------------------------------------|
| `comprar` | Compra `qty` acciones de `symbol`  |
| `vender`  | Vende `qty` acciones de `symbol`   |
| `cuenta`  | Consulta el estado de la cuenta    |

## Resultado

Después de cada ejecución, el workflow guarda el resultado en `resultado.json`:

```json
{
  "estado": "ok",
  "id": "...",
  "symbol": "TSLA",
  "qty": "1",
  "accion": "comprar",
  "status": "filled",
  "timestamp": "..."
}
```

## Archivos clave

| Archivo | Descripción |
|---------|-------------|
| `comando.json` | Orden a ejecutar (modificar para operar) |
| `resultado.json` | Resultado de la última orden ejecutada |
| `ejecutar_orden.py` | Script Python que llama a la API de Alpaca |
| `.github/workflows/trade.yml` | Workflow que se dispara al cambiar `comando.json` |

## Servidor Replit (alternativo)

- URL: `https://gonzalo--gonzaloor1988.replit.app`
- Endpoints: `/orden` y `/cuenta`
- Requiere header `X-Webhook-Token` (secret `WEBHOOK_TOKEN` en Replit)
- **Nota:** No accesible desde Claude Code web por restricciones de sandbox

## Rama de trabajo

`claude/alpaca-account-setup-zUFmC`
