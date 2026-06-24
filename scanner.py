#!/usr/bin/env python3
# =============================================================================
# scanner.py — Orchestrador principal do CFI v2.0
# Manual CFI v2.0 — Secção 8
#
# Invocado via GitHub Actions com variável de ambiente:
#   SCAN_TIPO=pesado | leve | breakout
#
# Scan pesado  (5×/dia, 06h/10h/13h/18h/22h Lisboa): S1-S6 completos
# Scan leve    (horário, Estados 2-5): recalcula S1/S4/S5 via MEXC API
# Scan breakout (30 min, só Estado 3): verifica 3 condições de trigger
#
# Fonte de dados:
#   MEXC API (pública) → preços, volume, OI, funding, L/S, candles
#   Claude API + Playwright → heatmap Coinglass (só Método A, Estado 3)
# =============================================================================

from __future__ import annotations

import json
import logging
import math
import os
import base64
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

# ── Módulos do sistema ────────────────────────────────────────────────────────
from config import (
    GITHUB_REPO, GITHUB_TOKEN, STATE_JSON_PATH,
    MEXC_BASE_URL, MEXC_API_KEY, MEXC_API_SECRET,
    SCAN_PESADO_HORAS_LISBOA, UNIVERSO_REVISAO_HORA_LISBOA,
    UNIVERSO_VOLUME_MIN_USD, UNIVERSO_DIAS_LISTADO_MIN,
    UNIVERSO_MARKET_CAP_MIN_USD, UNIVERSO_GRACE_PERIOD_DIAS,
    MISS_THRESHOLD_PCT, VOLUME_MIN_POR_CATEGORIA,
    BTC_TICKER, BTC_EMA_PERIODO, BTC_VOLATILIDADE_EXTREMA_PCT,
    BTC_NORMALIZADO_VAR_MAX_PCT, BTC_NORMALIZADO_N_CANDLES,
    ATR_PERIODO, COINGLASS_FALHA_SCORE_MAX,
    S2_OI_CHANGE_MIN_PCT, S2_OI_CHANGE_MAX_PCT,
    S3_FUNDING_MIN, S3_FUNDING_MAX,
    S6_LONG_LS_RATIO_MIN, S6_SHORT_LS_RATIO_MAX,
    TRIGGER_VOLUME_REFERENCIA_CANDLES,
    TZ_LISBOA, TZ_UTC,
    SIZING_VINHA_ESTADO2, SIZING_VINHA_ESTADO3, SIZING_SALTO_DIRECTO,
    MOMENTO2_JANELA_ENTRADA_H,
    CONCLUSAO_CHECKPOINTS_H,
)
from signals import (
    DadosMEXC, DadosCoinglass, SinaisHerdados,
    calcular_sinais_scan_pesado, calcular_sinais_scan_leve,
    verificar_funding_flag,
    _ema,
)
from scoring import (
    EstadoToken, ResultadoScoring,
    processar_scan_pesado, processar_scan_leve,
    calcular_direccao, Alerta,
    ESTADO_PASSIVA, ESTADO_RADAR, ESTADO_PRIORITARIO,
    ESTADO_BREAKOUT, ESTADO_CONCLUIDO,
)
from triggers import (
    DadosBreakout, DadosEstado4,
    verificar_btc_volatil, verificar_btc_normalizado,
    verificar_trigger_breakout, verificar_trigger_ainda_valido,
    actualizar_checkpoint, verificar_conclusao,
)
from leverage import calcular as calcular_leverage_niveis
from heatmap_claude import obter_target_metodo_a

# ── Placeholders para módulos ainda não escritos ──────────────────────────────
try:
    from notificacoes import enviar_momento, enviar_update_horario, enviar_alerta_degradacao
except ImportError:
    def enviar_momento(*a, **k): pass
    def enviar_update_horario(*a, **k): pass
    def enviar_alerta_degradacao(*a, **k): pass

try:
    from notion_logger import log_scan, log_deteccao, log_move_update, log_miss
except ImportError:
    def log_scan(*a, **k): pass
    def log_deteccao(*a, **k): pass
    def log_move_update(*a, **k): pass
    def log_miss(*a, **k): pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("scanner")


# =============================================================================
# GITHUB — leitura e escrita do state.json
# =============================================================================

def _github_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "andreya-v2-scanner",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def carregar_estado() -> tuple[dict[str, dict], str]:
    """
    Lê state.json do GitHub.
    Retorna (estado_dict, sha) onde estado_dict é {ticker: campos_dict}.
    """
    resp = _github_request("GET", STATE_JSON_PATH)
    conteudo = base64.b64decode(resp["content"]).decode()
    sha = resp["sha"]
    return json.loads(conteudo), sha


def guardar_estado(estado: dict[str, dict], sha: str, mensagem: str) -> str:
    """
    Escreve state.json no GitHub.
    Retorna o novo SHA.
    """
    conteudo_b64 = base64.b64encode(
        json.dumps(estado, indent=2, ensure_ascii=False).encode()
    ).decode()
    resp = _github_request("PUT", STATE_JSON_PATH, {
        "message": mensagem,
        "content": conteudo_b64,
        "sha": sha,
    })
    return resp["content"]["sha"]


def estado_para_token(campos: dict, ticker: str) -> EstadoToken:
    """Reconstrói EstadoToken a partir do dict lido do JSON."""
    agora = datetime.now(TZ_UTC).isoformat()
    try:
        return EstadoToken.from_dict({**campos, "ticker": ticker})
    except Exception:
        return EstadoToken.novo(ticker, agora)


def token_para_dict(token: EstadoToken, extra: dict) -> dict:
    """Serializa EstadoToken + campos operacionais extras para o JSON."""
    d = token.to_dict()
    d.update(extra)
    return d


# =============================================================================
# MEXC API — HTTP helpers
# =============================================================================

def _mexc_get(endpoint: str, params: dict | None = None) -> Optional[Any]:
    """GET à MEXC Futures API (pública — sem autenticação necessária para dados de mercado)."""
    url = f"{MEXC_BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "andreya-v2-scanner", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if resp.get("success", True) is False:
                log.warning(f"MEXC API erro em {endpoint}: {resp.get('message')}")
                return None
            return resp.get("data", resp)
    except Exception as e:
        log.error(f"MEXC GET {endpoint} falhou: {e}")
        return None


def fetch_todos_tickers() -> dict[str, dict]:
    """
    Devolve dict {symbol: ticker_dict} com todos os futuros USDT-M activos.
    Campos relevantes por ticker: lastPrice, bid1, ask1, volume24, amount24,
    holdVol (OI), fundingRate, lower24Price, upper24Price, priceChangeRate.
    """
    dados = _mexc_get("/contract/ticker")
    if not dados:
        return {}
    if isinstance(dados, list):
        return {t["symbol"]: t for t in dados if "_USDT" in t.get("symbol", "")}
    return {}


def fetch_candles(symbol: str, intervalo: str, count: int) -> list[dict]:
    """
    Devolve lista de candles OHLCV em ordem cronológica (0 = mais antigo).
    intervalo: "Min60" para 1h, "Min30" para 30m.
    Cada candle: {open, high, low, close, volume, timestamp}.
    """
    dados = _mexc_get(f"/contract/kline/{symbol}", {
        "interval": intervalo,
        "count": count,
    })
    if not dados or not isinstance(dados, dict):
        return []
    # MEXC devolve arrays paralelos
    try:
        opens  = dados.get("open", [])
        highs  = dados.get("high", [])
        lows   = dados.get("low", [])
        closes = dados.get("close", [])
        vols   = dados.get("vol", [])
        times  = dados.get("time", [])
        n = min(len(opens), len(highs), len(lows), len(closes), len(vols))
        return [
            {
                "open":      float(opens[i]),
                "high":      float(highs[i]),
                "low":       float(lows[i]),
                "close":     float(closes[i]),
                "volume":    float(vols[i]),
                "timestamp": int(times[i]) if i < len(times) else 0,
            }
            for i in range(n)
        ]
    except Exception as e:
        log.error(f"Parsing candles {symbol} {intervalo}: {e}")
        return []


# =============================================================================
# Cálculos auxiliares
# =============================================================================

def calcular_atr_pct(candles: list[dict], periodo: int = ATR_PERIODO) -> float:
    """
    ATR(periodo) em % do preço actual.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    if len(candles) < periodo + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h  = candles[i]["high"]
        l  = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    atr_abs = sum(trs[-periodo:]) / periodo
    preco   = candles[-1]["close"]
    return atr_abs / preco if preco > 0 else 0.0


def calcular_volume_media_7d(candles_1h: list[dict]) -> float:
    """
    Média do volume diário das últimas ~7 dias a partir de candles 1h.
    Agrega os últimos 168 candles (7×24) em dias e devolve a média.
    """
    if len(candles_1h) < 24:
        return sum(c["volume"] for c in candles_1h) if candles_1h else 0.0
    ultimos = candles_1h[-168:]  # 7 dias
    n_dias  = max(1, len(ultimos) // 24)
    total   = sum(c["volume"] for c in ultimos)
    return total / n_dias


def construir_dados_mexc(
    symbol: str,
    ticker: dict,
    candles_1h: list[dict],
    oi_24h_anterior: float,
) -> tuple[DadosMEXC, DadosCoinglass, float]:
    """
    Constrói DadosMEXC e DadosCoinglass a partir dos dados MEXC.

    DadosCoinglass é populado com:
      - OI change 24h: comparação com oi_24h_anterior (guardado no estado)
      - Funding rate: directo do ticker MEXC
      - L/S ratio: endpoint MEXC ou neutral (1.0) se indisponível

    Retorna: (DadosMEXC, DadosCoinglass, atr_1h_pct)
    """
    preco    = float(ticker.get("lastPrice", 0))
    vol_24h  = float(ticker.get("volume24", 0))       # em contratos
    oi_atual = float(ticker.get("holdVol", 0))
    high_24h = float(ticker.get("upper24Price", preco * 1.02))
    low_24h  = float(ticker.get("lower24Price", preco * 0.98))
    preco_change_pct = float(ticker.get("priceChangeRate", 0))  # decimal

    # Calcular ATR e volume médio 7d
    atr_pct       = calcular_atr_pct(candles_1h)
    vol_media_7d  = calcular_volume_media_7d(candles_1h)

    mexc = DadosMEXC(
        ticker=symbol,
        preco_actual=preco,
        preco_change_24h_pct=preco_change_pct,
        volume_24h=vol_24h,
        volume_media_7d=vol_media_7d,
        high_24h=high_24h,
        low_24h=low_24h,
        atr_1h=atr_pct,
        candles_1h=candles_1h,
    )

    # OI change 24h (proxy: vs valor guardado no estado anterior)
    if oi_24h_anterior > 0:
        oi_change = (oi_atual - oi_24h_anterior) / oi_24h_anterior
    else:
        oi_change = 0.0

    # Funding rate
    funding = float(ticker.get("fundingRate", 0))

    # L/S ratio — tentar endpoint dedicado; fallback neutral
    ls_ratio = _obter_ls_ratio(symbol)

    coinglass = DadosCoinglass(
        ticker=symbol,
        oi_change_24h_pct=oi_change,
        funding_rate=funding,
        ls_ratio=ls_ratio,
    )

    return mexc, coinglass, atr_pct


def _obter_ls_ratio(symbol: str) -> float:
    """
    Tenta obter L/S ratio via MEXC API.
    O endpoint /contract/long_short_ratio/ retorna 404 na MEXC para a maioria
    dos tokens — fallback silencioso para 1.0 (neutral).
    Com L/S neutral, S6 fica sempre False: comportamento conservador aceite.
    """
    url = f"{MEXC_BASE_URL}/contract/long_short_ratio/{symbol}"
    try:
        req = urllib.request.Request(url + "?period=1h&limit=1", headers={
    "User-Agent": "andreya-v2-scanner",
    "ApiKey": mx0vglGstOK6nihksE,
})
            
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
            dados = resp.get("data", resp)
            if isinstance(dados, list) and dados:
                return float(dados[0].get("longShortRatio", 1.0))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.debug(f"[{symbol}] L/S ratio: endpoint não disponível (404) → neutral 1.0")
        else:
            log.debug(f"[{symbol}] L/S ratio: HTTP {e.code} → neutral 1.0")
    except Exception:
        log.debug(f"[{symbol}] L/S ratio: falhou → neutral 1.0")
    return 1.0


# =============================================================================
# BTC — regime e volatilidade
# =============================================================================

def obter_regime_btc(candles_btc_1h: list[dict]) -> tuple[bool, float, float]:
    """
    Verifica se BTC está acima da EMA21(1h) e mede volatilidade recente.

    Retorna: (btc_acima_ema21, var_candle_actual_pct, var_candle_anterior_pct)
    var_candle = |close - open| / open do candle 1h
    """
    if len(candles_btc_1h) < BTC_EMA_PERIODO + 2:
        return True, 0.0, 0.0  # dados insuficientes → permissivo

    closes = [c["close"] for c in candles_btc_1h]
    ema21  = _ema(closes, BTC_EMA_PERIODO)
    preco  = candles_btc_1h[-1]["close"]

    # Variação dos últimos 2 candles fechados (para normalização)
    def var_pct(c: dict) -> float:
        return abs(c["close"] - c["open"]) / c["open"] if c["open"] > 0 else 0.0

    var_actual   = var_pct(candles_btc_1h[-2])  # candle anterior ao actual em curso
    var_anterior = var_pct(candles_btc_1h[-3])

    return preco > ema21, var_actual, var_anterior


# =============================================================================
# SCAN PESADO — 5× por dia
# =============================================================================

def scan_pesado(hora_lisboa: int) -> None:
    """
    Scan pesado completo:
    1. Carrega estado e tickers MEXC
    2. Revê universo (só às 06h)
    3. Para cada token: calcula S1-S6 LONG+SHORT, aplica scoring, actualiza estado
    4. Miss detection em Estado 1
    5. Guarda estado no GitHub
    6. Log Notion Base 1 + PDF + Telegram

    Manual secção 8.1 — Scan Pesado.
    """
    agora_utc = datetime.now(TZ_UTC).isoformat()
    scan_id   = f"SC-{agora_utc[:10].replace('-','')}T{hora_lisboa:02d}h"
    log.info(f"=== SCAN PESADO {hora_lisboa}h Lisboa — {scan_id} ===")

    # ── 1. Carregar estado ────────────────────────────────────────────────────
    try:
        estado_json, sha = carregar_estado()
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    # ── 2. Buscar todos os tickers MEXC ──────────────────────────────────────
    tickers = fetch_todos_tickers()
    if not tickers:
        log.error("MEXC falhou — scan abortado (manual: API Status = 'MEXC falhou')")
        return

    # ── 3. Regime BTC ─────────────────────────────────────────────────────────
    candles_btc = fetch_candles(BTC_TICKER, "Min60", 50)
    btc_acima_ema21, btc_var_actual, btc_var_anterior = obter_regime_btc(candles_btc)
    btc_volatil = verificar_btc_volatil(btc_var_actual)
    log.info(f"BTC: {'↑ acima' if btc_acima_ema21 else '↓ abaixo'} EMA21 | "
             f"volátil={btc_volatil} | var={btc_var_actual:.2%}")

    # ── 4. Revisão do universo (apenas 06h) ───────────────────────────────────
    if hora_lisboa == UNIVERSO_REVISAO_HORA_LISBOA:
        _rever_universo(estado_json, tickers, agora_utc)

    # ── 5. Loop por token ─────────────────────────────────────────────────────
    novos_estado2 = []
    novos_estado3 = []
    misses        = []

    for symbol, campos in list(estado_json.items()):
        if symbol == BTC_TICKER:
            continue

        ticker = tickers.get(symbol)
        if not ticker:
            log.debug(f"[{symbol}] Não encontrado nos tickers MEXC — skip")
            continue

        categoria = campos.get("categoria", "Memes")

        # Buscar candles 1h
        candles_1h = fetch_candles(symbol, "Min60", 200)
        if len(candles_1h) < 25:
            log.warning(f"[{symbol}] Candles insuficientes ({len(candles_1h)}) — skip")
            continue

        # Construir dados
        oi_24h_anterior = campos.get("oi_atual", 0.0)
        mexc_d, cg_d, atr_pct = construir_dados_mexc(
            symbol, ticker, candles_1h, oi_24h_anterior
        )

        # Reconstruir token
        token = estado_para_token(campos, symbol)
        estado_antes = token.estado

        # ── 5a. Sinais S1-S6 ─────────────────────────────────────────────────
        sl = calcular_sinais_scan_pesado(mexc_d, cg_d, "LONG")
        ss = calcular_sinais_scan_pesado(mexc_d, cg_d, "SHORT")

        # Funding flag (não bloqueante)
        funding_flag = verificar_funding_flag(cg_d, categoria)
        if funding_flag:
            log.info(f"[{symbol}] FLAG: {funding_flag} (funding={cg_d.funding_rate:.4%})")

        # ── 5b. Scoring e transições ──────────────────────────────────────────
        resultado = processar_scan_pesado(
            token, sl, ss, btc_acima_ema21, agora_utc
        )
        token = resultado.novo_estado_token

        # ── 5c. Miss detection (Estado 1) ────────────────────────────────────
        if estado_antes == ESTADO_PASSIVA and token.estado == ESTADO_PASSIVA:
            limiar = MISS_THRESHOLD_PCT.get(categoria, 0.10)
            high, low = ticker.get("upper24Price", 0), ticker.get("lower24Price", 0)
            if low > 0:
                amplitude = (high - low) / low
                if amplitude > limiar:
                    misses.append({
                        "symbol": symbol, "amplitude": amplitude,
                        "direccao": "LONG" if calcular_direccao(sl.score, ss.score) == "LONG" else "SHORT",
                    })
                    log.info(f"[{symbol}] MISS: amplitude {amplitude:.1%} > {limiar:.0%}")

        # ── 5d. Heatmap (Método A) em Estado 3 novo ──────────────────────────
        leverage_resultado = None
        if token.estado == ESTADO_PRIORITARIO and estado_antes < ESTADO_PRIORITARIO:
            target_pct, metodo = obter_target_metodo_a(
                symbol, mexc_d.preco_actual, token.direccao, atr_pct
            )
            leverage_resultado = calcular_leverage_niveis(
                preco_entry=mexc_d.preco_actual,
                atr_1h_pct=atr_pct,
                categoria=categoria,
                direccao=token.direccao,
                metodo=metodo,
                target_pct_override=target_pct if metodo == "A" else None,
            )
            # Gravar níveis no token para comparação no Momento 2
            token.momento1_target_pct  = leverage_resultado.target_pct
            token.momento1_sl_preco    = leverage_resultado.sl_preco
            token.momento1_tp1_preco   = leverage_resultado.tp1_preco
            token.momento1_tp2_preco   = leverage_resultado.tp2_preco
            token.momento1_leverage    = leverage_resultado.leverage
            token.momento1_metodo      = leverage_resultado.target_metodo

        # ── 5e. Alertas Telegram ─────────────────────────────────────────────
        for alerta in resultado.alertas:
            import time
            sizing = (SIZING_SALTO_DIRECTO if resultado.salto_directo
                      else SIZING_VINHA_ESTADO3 if token.estado == ESTADO_PRIORITARIO
                      else SIZING_VINHA_ESTADO2)
            # Passar sinais da direcção dominante ao Momento 0
            sinais_dominantes = sl if token.direccao == "LONG" else ss
            enviar_momento(
                tipo=alerta,
                symbol=symbol,
                token=token,
                resultado_scoring=resultado,
                resultado_leverage=leverage_resultado,
                funding_flag=funding_flag,
                sizing=sizing,
                sinais=sinais_dominantes,
            )
            if alerta == Alerta.MOMENTO_0:
                novos_estado2.append(symbol)
                time.sleep(1)   # anti-flood: 1s entre Momento 0 consecutivos
            elif alerta == Alerta.MOMENTO_1:
                novos_estado3.append(symbol)

        # ── 5f. Actualizar campos operacionais no estado ──────────────────────
        campos_extra = {
            "categoria":       categoria,
            "oi_atual":        float(ticker.get("holdVol", 0)),
            "oi_inicio_dia":   campos.get("oi_inicio_dia", float(ticker.get("holdVol", 0)))
                               if hora_lisboa != UNIVERSO_REVISAO_HORA_LISBOA
                               else float(ticker.get("holdVol", 0)),
            "volume_media_7d": mexc_d.volume_media_7d,
            "atr_1h_pct":      atr_pct,
            "oi_30m_anterior": float(ticker.get("holdVol", 0)),  # reset no scan pesado
        }
        estado_json[symbol] = token_para_dict(token, campos_extra)

        # ── 5g. Log Notion Base 3 (detecção) ─────────────────────────────────
        if resultado.estado_novo != resultado.estado_anterior:
            log_deteccao(
                token=token,
                resultado=resultado,
                sinais_long=sl,
                sinais_short=ss,
                atr_1h=atr_pct,
                funding_flag=funding_flag,
                btc_acima_ema21=btc_acima_ema21,
                scan_id=scan_id,
                bloqueado_filtro_btc=resultado.bloqueado_filtro_btc,
            )

    # ── 6. Guardar estado ─────────────────────────────────────────────────────
    try:
        sha = guardar_estado(estado_json, sha,
                             f"scan pesado {hora_lisboa}h Lisboa — {scan_id}")
        log.info(f"state.json guardado (SHA ...{sha[-8:]})")
    except Exception as e:
        log.error(f"Falha ao guardar state.json: {e}")

    # ── 7. Log Notion Base 1 + PDF ────────────────────────────────────────────
    _contar_e_log_scan_pesado(scan_id, hora_lisboa, estado_json,
                               novos_estado2, novos_estado3, misses,
                               btc_acima_ema21, candles_btc)


# =============================================================================
# SCAN LEVE — horário
# =============================================================================

def scan_leve() -> None:
    """
    Scan leve (horário) para tokens em Estado 2, 3, 4, 5.

    Estado 2: actualiza score (S1/S4/S5 frescos, S2/S3/S6 herdados)
    Estado 3: verifica descida de score → degradação ou 3A
    Estado 4: verifica condições de conclusão
    Estado 5: emite Momento 3B, repõe Estado 1

    Manual secção 8.1 — Scan Leve.
    """
    agora_utc = datetime.now(TZ_UTC).isoformat()
    log.info(f"=== SCAN LEVE {agora_utc[:16]} ===")

    try:
        estado_json, sha = carregar_estado()
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    tickers = fetch_todos_tickers()
    if not tickers:
        log.error("MEXC falhou — scan leve abortado")
        return

    # BTC volatilidade (pode pausar breakouts mas scan leve corre sempre)
    candles_btc = fetch_candles(BTC_TICKER, "Min60", 30)
    btc_acima_ema21, btc_var_actual, _ = obter_regime_btc(candles_btc)
    btc_volatil = verificar_btc_volatil(btc_var_actual)

    activos = {
        sym: campos for sym, campos in estado_json.items()
        if campos.get("estado", 1) >= 2
    }

    if not activos:
        log.info("Sem tokens activos — scan leve sem updates")
        return

    alterados = False

    for symbol, campos in activos.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        token = estado_para_token(campos, symbol)
        estado_antes = token.estado

        # Buscar apenas candles 1h (rápido — MEXC API)
        candles_1h = fetch_candles(symbol, "Min60", 30)
        if len(candles_1h) < 25:
            continue

        oi_atual = float(ticker.get("holdVol", 0))
        atr_pct  = campos.get("atr_1h_pct", 0.008)

        mexc_d = DadosMEXC(
            ticker=symbol,
            preco_actual=float(ticker.get("lastPrice", 0)),
            preco_change_24h_pct=float(ticker.get("priceChangeRate", 0)),
            volume_24h=float(ticker.get("volume24", 0)),
            volume_media_7d=campos.get("volume_media_7d", 0),
            high_24h=float(ticker.get("upper24Price", 0)),
            low_24h=float(ticker.get("lower24Price", 0)),
            atr_1h=atr_pct,
            candles_1h=candles_1h,
        )

        # ── Estado 4 — verificar conclusão ───────────────────────────────────
        if estado_antes == ESTADO_BREAKOUT:
            _processar_estado4(token, mexc_d, campos, estado_json, symbol, agora_utc)
            alterados = True
            continue

        # ── Estado 5 — emitir 3B e resetar ───────────────────────────────────
        if estado_antes == ESTADO_CONCLUIDO:
            enviar_momento("MOMENTO_3B", symbol, token, None)
            token = estado_para_token({}, symbol)   # reset para Estado 1
            estado_json[symbol] = token_para_dict(token, {"categoria": campos.get("categoria", "Memes")})
            alterados = True
            continue

        # ── Estados 2 e 3 — scan leve normal ─────────────────────────────────
        herdados = token.get_sinais_herdados()
        direcao  = token.direccao
        sinais   = calcular_sinais_scan_leve(mexc_d, herdados, direcao)

        resultado = processar_scan_leve(token, sinais, btc_acima_ema21, agora_utc)
        token = resultado.novo_estado_token

        for alerta in resultado.alertas:
            if alerta == Alerta.MOMENTO_3A:
                enviar_momento("MOMENTO_3A", symbol, token, resultado)
            elif alerta == Alerta.DEGRADACAO:
                enviar_alerta_degradacao(symbol, token, resultado)

        if resultado.estado_novo != resultado.estado_anterior:
            log.info(f"[{symbol}] Estado {estado_antes}→{resultado.estado_novo} no scan leve")
            alterados = True

        campos.update(token_para_dict(token, {
            "categoria":       campos.get("categoria", "Memes"),
            "oi_atual":        oi_atual,
            "volume_media_7d": campos.get("volume_media_7d", 0),
            "atr_1h_pct":      atr_pct,
        }))
        estado_json[symbol] = campos

    if alterados:
        try:
            sha = guardar_estado(estado_json, sha, f"scan leve {agora_utc[:16]}")
        except Exception as e:
            log.error(f"Falha ao guardar estado no scan leve: {e}")

    # Update horário Telegram (só se há tokens activos)
    enviar_update_horario(agora_utc, estado_json, btc_volatil)


def _processar_estado4(
    token: EstadoToken,
    mexc_d: DadosMEXC,
    campos: dict,
    estado_json: dict,
    symbol: str,
    agora_utc: str,
) -> None:
    """Verifica conclusão do Estado 4 e actualiza checkpoints hora-a-hora."""
    ts_trigger = campos.get("trigger_timestamp", agora_utc)
    target_pct = token.momento1_target_pct or 0.062
    nivel_pb   = campos.get("trigger_preco", mexc_d.preco_actual)
    checkpoints = campos.get("checkpoints", {})
    # As chaves do JSON são strings — converter para int
    checkpoints = {int(k): v for k, v in checkpoints.items()}

    checkpoints, novo_cp = actualizar_checkpoint(
        mexc_d.preco_actual, float(campos.get("trigger_preco", mexc_d.preco_actual)),
        token.direccao, ts_trigger, agora_utc, checkpoints,
    )
    if novo_cp:
        log.info(f"[{symbol}] Checkpoint +{novo_cp}h registado")
        log_move_update(symbol, novo_cp, checkpoints[novo_cp])

    dados_e4 = DadosEstado4(
        ticker=symbol,
        direccao=token.direccao,
        preco_atual=mexc_d.preco_actual,
        preco_trigger=float(campos.get("trigger_preco", mexc_d.preco_actual)),
        nivel_pre_breakout=float(campos.get("trigger_nivel_pre_breakout", nivel_pb)),
        target_pct=target_pct,
        timestamp_trigger_utc=ts_trigger,
        timestamp_atual_utc=agora_utc,
        checkpoints=checkpoints,
    )

    conclusao = verificar_conclusao(dados_e4)
    if conclusao:
        log.info(f"[{symbol}] CONCLUÍDO: Condição {conclusao.condicao} ({conclusao.tipo})")
        token.estado = ESTADO_CONCLUIDO
        enviar_momento("MOMENTO_3B_PREP", symbol, token, None,
                       conclusao=conclusao)

    campos.update({
        "estado":      token.estado,
        "checkpoints": {str(k): v for k, v in checkpoints.items()},
    })
    estado_json[symbol] = campos


# =============================================================================
# SCAN DE BREAKOUT — a cada 30 min, só Estado 3
# =============================================================================

def scan_breakout() -> None:
    """
    Scan de breakout (30 min) para tokens em Estado 3.

    Verifica as 3 condições de trigger em simultâneo:
    1. Volume 30m > 200% da média das últimas 6h
    2. OI change 30m > +3%
    3. Preço > High 24h (LONG) ou < Low 24h (SHORT)

    Se BTC volátil: regista trigger pendente, não envia Momento 2.
    Manual secção 6.1 e 8.1 — Scan de Breakout.
    """
    agora_utc = datetime.now(TZ_UTC).isoformat()
    log.info(f"=== SCAN BREAKOUT {agora_utc[:16]} ===")

    try:
        estado_json, sha = carregar_estado()
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    # Tokens em Estado 3
    candidatos = {
        sym: campos for sym, campos in estado_json.items()
        if campos.get("estado", 1) == ESTADO_PRIORITARIO
    }

    if not candidatos:
        log.info("Sem tokens em Estado 3 — scan breakout sem acção")
        return

    tickers = fetch_todos_tickers()
    candles_btc = fetch_candles(BTC_TICKER, "Min60", 10)
    _, btc_var_actual, btc_var_anterior = obter_regime_btc(candles_btc)
    btc_volatil   = verificar_btc_volatil(btc_var_actual)
    btc_normaliza = verificar_btc_normalizado(btc_var_actual, btc_var_anterior)

    alterados = False

    for symbol, campos in candidatos.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        token = estado_para_token(campos, symbol)
        oi_atual        = float(ticker.get("holdVol", 0))
        oi_30m_anterior = float(campos.get("oi_30m_anterior", oi_atual))

        # Buscar candles 30m (volume trigger)
        candles_30m = fetch_candles(symbol, "Min30", TRIGGER_VOLUME_REFERENCIA_CANDLES + 1)

        if len(candles_30m) < 2:
            continue

        vol_ultimo_30m = candles_30m[-1]["volume"]
        referencia     = candles_30m[:-1]  # os 12 anteriores
        vol_media_30m  = (sum(c["volume"] for c in referencia) / len(referencia)
                          if referencia else 0.0)

        oi_change_30m  = ((oi_atual - oi_30m_anterior) / oi_30m_anterior
                          if oi_30m_anterior > 0 else 0.0)

        dados_bo = DadosBreakout(
            ticker=symbol,
            volume_ultimo_30m=vol_ultimo_30m,
            volume_media_30m_6h=vol_media_30m,
            oi_change_30m_pct=oi_change_30m,
            preco_atual=float(ticker.get("lastPrice", 0)),
            high_24h=float(ticker.get("upper24Price", 0)),
            low_24h=float(ticker.get("lower24Price", 0)),
        )

        # ── Trigger pendente em espera de normalização BTC ───────────────────
        if campos.get("trigger_pendente") and btc_normaliza:
            valido = verificar_trigger_ainda_valido(dados_bo, token.direccao)
            if valido:
                log.info(f"[{symbol}] Trigger pendente validado após BTC normalizar → Momento 2")
                _emitir_momento2(symbol, token, campos, ticker, agora_utc,
                                 btc_volatil_trigger=True)
                token.trigger_pendente    = False
                token.btc_volatil_no_trigger = True
                token.estado              = ESTADO_BREAKOUT
            else:
                log.info(f"[{symbol}] Trigger pendente inválido após normalização — descartado")
                campos["trigger_pendente"] = False
            alterados = True
            estado_json[symbol] = token_para_dict(token, {
                "categoria":       campos.get("categoria"),
                "oi_30m_anterior": oi_atual,
            })
            continue

        # ── Verificar trigger fresco ──────────────────────────────────────────
        resultado_bo = verificar_trigger_breakout(dados_bo, token.direccao)
        log.debug(f"[{symbol}] Breakout check: vol_ratio={resultado_bo.volume_ratio:.2f}x "
                  f"OI30m={oi_change_30m:.2%} C1={resultado_bo.condicao1_ok} "
                  f"C2={resultado_bo.condicao2_ok} C3={resultado_bo.condicao3_ok}")

        if resultado_bo.trigger_ativo:
            if btc_volatil:
                # Regista mas não envia
                log.info(f"[{symbol}] Trigger detectado mas BTC volátil — trigger_pendente=True")
                campos["trigger_pendente"]    = True
                campos["trigger_volume"]      = resultado_bo.volume_ratio
                campos["trigger_OI"]          = oi_change_30m
                campos["trigger_preco"]       = dados_bo.preco_atual
                campos["trigger_nivel_pre_breakout"] = resultado_bo.nivel_pre_breakout
                campos["trigger_timestamp"]   = agora_utc
            else:
                log.info(f"[{symbol}] TRIGGER CONFIRMADO → Momento 2")
                _emitir_momento2(symbol, token, campos, ticker, agora_utc,
                                 btc_volatil_trigger=False,
                                 nivel_pre_breakout=resultado_bo.nivel_pre_breakout,
                                 trigger_volume=resultado_bo.volume_ratio,
                                 trigger_oi=oi_change_30m)
                token.estado              = ESTADO_BREAKOUT
                token.trigger_pendente    = False
                token.trigger_timestamp   = agora_utc
                token.trigger_volume      = resultado_bo.volume_ratio
                token.trigger_OI          = oi_change_30m
                token.trigger_preco       = dados_bo.preco_atual
                token.btc_volatil_no_trigger = False

            alterados = True

        # Actualizar OI para próximo ciclo de 30 min
        campos.update(token_para_dict(token, {
            "categoria":       campos.get("categoria"),
            "oi_30m_anterior": oi_atual,
            "atr_1h_pct":      campos.get("atr_1h_pct", 0.008),
        }))
        estado_json[symbol] = campos

    if alterados:
        try:
            sha = guardar_estado(estado_json, sha, f"scan breakout {agora_utc[:16]}")
        except Exception as e:
            log.error(f"Falha ao guardar estado no scan breakout: {e}")


def _emitir_momento2(
    symbol: str,
    token: EstadoToken,
    campos: dict,
    ticker: dict,
    agora_utc: str,
    btc_volatil_trigger: bool = False,
    nivel_pre_breakout: float = 0.0,
    trigger_volume: float = 0.0,
    trigger_oi: float = 0.0,
) -> None:
    """Calcula leverage (Método A/C) e envia Momento 2."""
    atr_pct = campos.get("atr_1h_pct", 0.008)
    preco   = float(ticker.get("lastPrice", 0))
    cat     = campos.get("categoria", "Memes")

    # Método A no trigger
    target_pct, metodo = obter_target_metodo_a(symbol, preco, token.direccao, atr_pct)
    leverage_r = calcular_leverage_niveis(
        preco_entry=preco,
        atr_1h_pct=atr_pct,
        categoria=cat,
        direccao=token.direccao,
        metodo=metodo,
        target_pct_override=target_pct if metodo == "A" else None,
    )

    # Notas de prioridade (manual 9.1)
    notas = []
    if btc_volatil_trigger:
        notas.append("BTC_VOLATIL_NO_TRIGGER")
    if token.momento1_target_pct and abs(leverage_r.target_pct - token.momento1_target_pct) > 0.001:
        notas.append("TARGET_ACTUALIZADO")
    if campos.get("grace_period"):
        notas.append("GRACE_PERIOD_ACTIVO")

    sizing = SIZING_SALTO_DIRECTO if token.salto_directo else SIZING_VINHA_ESTADO3

    enviar_momento(
        tipo="MOMENTO_2",
        symbol=symbol,
        token=token,
        resultado_scoring=None,
        resultado_leverage=leverage_r,
        sizing=sizing,
        notas_prioridade=notas,
        janela_horas=MOMENTO2_JANELA_ENTRADA_H,
        nivel_pre_breakout=nivel_pre_breakout,
    )

    # Gravar Momento 2 no Notion Base 4
    log_move_update(symbol, 0, 0.0,
                    trigger_preco=preco,
                    leverage=leverage_r,
                    notas=" | ".join(notas))


# =============================================================================
# Revisão do universo (06h Lisboa)
# =============================================================================

def _rever_universo(
    estado_json: dict,
    tickers: dict,
    agora_utc: str,
) -> None:
    """
    Revê o universo de tokens (manual 2.4):
    - Tokens que perderam filtros entram em grace period (3 dias)
    - Tokens novos (≥14 dias listados) adicionados em Estado 1
    - Grace period expirado: token removido com alerta
    """
    log.info("Revisão do universo (06h Lisboa)...")

    agora = datetime.fromisoformat(agora_utc)

    for symbol, ticker in tickers.items():
        if symbol == BTC_TICKER:
            continue

        volume24 = float(ticker.get("volume24", 0))

        if symbol not in estado_json:
            # Token novo — verificar elegibilidade básica
            if volume24 >= UNIVERSO_VOLUME_MIN_USD:
                log.info(f"[{symbol}] Novo token no universo — Estado 1")
                estado_json[symbol] = token_para_dict(
                    EstadoToken.novo(symbol, agora_utc),
                    {"categoria": _inferir_categoria(symbol), "oi_atual": 0.0,
                     "volume_media_7d": volume24, "atr_1h_pct": 0.008,
                     "oi_30m_anterior": 0.0},
                )
            continue

        campos = estado_json[symbol]
        em_grace = campos.get("grace_period", False)
        dias_grace = campos.get("grace_period_dias_restantes")

        if volume24 < UNIVERSO_VOLUME_MIN_USD:
            if not em_grace:
                campos["grace_period"] = True
                campos["grace_period_dias_restantes"] = UNIVERSO_GRACE_PERIOD_DIAS
                log.info(f"[{symbol}] Volume baixo → grace period {UNIVERSO_GRACE_PERIOD_DIAS} dias")
            elif dias_grace is not None:
                dias_grace -= 1
                campos["grace_period_dias_restantes"] = dias_grace
                if dias_grace <= 0:
                    log.info(f"[{symbol}] Grace period expirado → saiu do universo")
                    enviar_momento("SAIU_UNIVERSO", symbol, None, None)
                    del estado_json[symbol]
        else:
            if em_grace:
                campos["grace_period"] = False
                campos["grace_period_dias_restantes"] = None
                log.info(f"[{symbol}] Grace period encerrado — volume recuperado")


def _inferir_categoria(symbol: str) -> str:
    """Inferência simples de categoria pelo nome do ticker (heurística)."""
    s = symbol.lower()
    if any(k in s for k in ("pepe","doge","shib","floki","bonk","wif","meme","bome")):
        return "Memes"
    if any(k in s for k in ("ai","wld","fetch","agix","rndr","ocean","near")):
        return "AI"
    if any(k in s for k in ("uni","aave","crv","mkr","comp","snx","ldo","gmx")):
        return "DeFi"
    if any(k in s for k in ("sol","avax","bnb","ada","dot","atom","near","ftm")):
        return "Layer 1"
    if any(k in s for k in ("arb","op","matic","imx","zk","stx")):
        return "Layer 2"
    if any(k in s for k in ("axs","ronin","sand","mana","gala","beam","imx")):
        return "Gaming/NFT"
    return "Infrastructure"


# =============================================================================
# Logging do scan pesado (Notion Base 1 + PDF stub)
# =============================================================================

def _contar_e_log_scan_pesado(
    scan_id: str,
    hora_lisboa: int,
    estado_json: dict,
    novos_estado2: list,
    novos_estado3: list,
    misses: list,
    btc_acima_ema21: bool,
    candles_btc: list,
) -> None:
    """Conta métricas do scan e envia para Notion Base 1."""
    contagem = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for campos in estado_json.values():
        e = campos.get("estado", 1)
        contagem[e] = contagem.get(e, 0) + 1

    btc_preco = candles_btc[-1]["close"] if candles_btc else 0.0

    log.info(
        f"Scan {scan_id} concluído — "
        f"Total: {sum(contagem.values())} | "
        f"E1: {contagem[1]} E2: {contagem[2]} E3: {contagem[3]} "
        f"E4: {contagem[4]} E5: {contagem[5]} | "
        f"Novos E2: {len(novos_estado2)} Novos E3: {len(novos_estado3)} | "
        f"Misses: {len(misses)}"
    )

    log_scan(
        scan_id=scan_id,
        hora_lisboa=hora_lisboa,
        btc_preco=btc_preco,
        filtro_btc="Longs e Shorts" if btc_acima_ema21 else "Apenas Shorts",
        contagem_estados=contagem,
        novos_estado2=len(novos_estado2),
        novos_estado3=len(novos_estado3),
        misses=misses,
    )


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    tipo = os.environ.get("SCAN_TIPO", "").lower()
    if not tipo:
        log.error("SCAN_TIPO não definido. Usar: pesado | leve | breakout")
        sys.exit(1)

    if tipo == "pesado":
        agora_lisboa = datetime.now(TZ_LISBOA)
        hora = agora_lisboa.hour
        if hora not in SCAN_PESADO_HORAS_LISBOA:
            log.warning(f"Hora {hora}h não é hora de scan pesado — corre à mesma")
        scan_pesado(hora)

    elif tipo == "leve":
        scan_leve()

    elif tipo == "breakout":
        scan_breakout()

    else:
        log.error(f"SCAN_TIPO inválido: {tipo!r}. Usar: pesado | leve | breakout")
        sys.exit(1)


if __name__ == "__main__":
    main()
