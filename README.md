# Forex Binary Signal Hub

Plataforma web para monitorar sinais Forex em tempo real, com:

- catalogo de estrategias pre-programadas,
- selecao da estrategia do dia,
- ranking de assertividade,
- sinais `put/call` com expiracao de vela,
- webhook TradingView para ingestao externa.

## 1) Rodar localmente

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Acesse: `http://localhost:8000`

## 2) Variaveis de ambiente

Veja `.env.example`.

Campos principais:
- `FOREX_PAIRS`: pares a monitorar (ex.: `EURUSD,GBPUSD,USDJPY`)
- `POLLING_SECONDS`: intervalo de atualizacao de mercado
- `MIN_SIGNAL_GAP_SECONDS`: intervalo minimo entre sinais repetidos da mesma estrategia/par
- `BROKER_WEBHOOK_URL`: endpoint da sua plataforma de execucao
- `BROKER_AUTH_TOKEN`: token Bearer opcional para envio dos sinais

## 3) Webhook TradingView

Endpoint:

`POST /api/tradingview/webhook`

Payload exemplo:

```json
{
  "pair": "EURUSD",
  "direction": "call",
  "timeframe": "1m",
  "expiry_minutes": 3,
  "strategy_code": "tv-breakout",
  "confidence": 0.72,
  "reason": "TV alert: trend breakout"
}
```

## 4) Deploy no Render (online)

Este projeto ja inclui `Dockerfile` e `render.yaml`.

Passos:

1. Suba esse projeto para um repositorio GitHub.
2. No Render, clique em **New +** > **Blueprint**.
3. Selecione o repositorio.
4. O Render detecta `render.yaml` e cria o servico automaticamente.
5. Abra a URL publica do Render.

## 5) Integracao de execucao (bridge)

Quando `BROKER_WEBHOOK_URL` estiver configurada, todo sinal novo e enviado automaticamente para sua plataforma externa.

Payload enviado:

```json
{
  "signal_id": 12,
  "pair": "EURUSD",
  "direction": "call",
  "timeframe": "1m",
  "entry_price": 1.13321,
  "entry_time": "2026-04-20T15:22:00+00:00",
  "expiry_time": "2026-04-20T15:27:00+00:00",
  "confidence": 0.71,
  "strategy_code": "ema-trend",
  "reason": "EMA9>21 with bullish alignment",
  "source": "internal"
}
```

## 6) Observacao de mercado em tempo real

O feed padrao usa Yahoo Finance (`EURUSD=X`, etc.). Em alguns ativos pode haver pequeno delay da fonte. Para execucao em corretora real, troque o adaptador por feed direto da corretora/API institucional.

## 7) Aviso importante

Sinais sao probabilisticos e nao garantem resultado financeiro. Use gestao de risco e valide em conta demo antes de qualquer operacao real.
