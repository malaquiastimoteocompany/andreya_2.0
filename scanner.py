#!/usr/bin/env python3
# =============================================================================
# scanner.py — Orchestrador principal do CFI v2.0
# Manual CFI v2.0 — Secção 8
# =============================================================================

from __future__ import annotations

import json
import logging
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

try:
    from notificacoes import (
        enviar_momento,
        enviar_update_horario,
        enviar_alerta_degradacao,
        enviar_resumo_scan_pesado,
        enviar_resumo_scan_leve,
        enviar_momento_breakout,
    )
except ImportError:
    def enviar_momento(*a, **k): pass
    def enviar_update_horario(*a, **k): pass
    def enviar_alerta_degradacao(*a, **k): pass
    def enviar_resumo_scan_pesado(*a, **k): pass
    def enviar_resumo_scan_leve(*a, **k): pass
    def enviar_momento_breakout(*a, **k): pass

try:
    from notion_logger import log_scan, log_deteccao, log_move_update, log_miss, log_move_conclusao
except ImportError:
    def log_scan(*a, **k): pass
    def log_deteccao(*a, **k): pass
    def log_move_update(*a, **k): pass
    def log_miss(*a, **k): pass
    def log_move_conclusao(*a, **k): pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("scanner")

# Mínimo de candles 1h para considerar token com histórico suficiente
# 168 = 7 dias × 24h
CANDLES_HISTORICO_MIN = 168


# =============================================================================
# GITHUB
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
    resp     = _github_request("GET", STATE_JSON_PATH)
    conteudo = base64.b64decode(resp["content"]).decode()
    sha      = resp["sha"]
    return json.loads(conteudo), sha


def guardar_estado(estado: dict[str, dict], sha: str, mensagem: str) -> str:
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
    agora = datetime.now(TZ_UTC).isoformat()
    try:
        return EstadoToken.from_dict({**campos, "ticker": ticker})
    except Exception:
        return EstadoToken.novo(ticker, agora)


def token_para_dict(token: EstadoToken, extra: dict) -> dict:
    d = token.to_dict()
    d.update(extra)
    return d


# =============================================================================
# MEXC API
# =============================================================================

def _mexc_get(endpoint: str, params: dict | None = None) -> Optional[Any]:
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
    dados = _mexc_get("/contract/ticker")
    if not dados:
        return {}
    if isinstance(dados, list):
        return {t["symbol"]: t for t in dados if "_USDT" in t.get("symbol", "")}
    return {}


def fetch_candles(symbol: str, intervalo: str, count: int) -> list[dict]:
    dados = _mexc_get(f"/contract/kline/{symbol}", {"interval": intervalo, "count": count})
    if not dados or not isinstance(dados, dict):
        return []
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


def token_tem_historico_suficiente(symbol: str) -> bool:
    """
    Verifica se o token tem pelo menos 168 candles 1h (7 dias) na MEXC.
    Usado antes de adicionar ao universo — garante dados fiáveis para sinais.
    """
    candles = fetch_candles(symbol, "Min60", CANDLES_HISTORICO_MIN)
    tem = len(candles) >= CANDLES_HISTORICO_MIN
    if not tem:
        log.info(f"[{symbol}] Histórico insuficiente ({len(candles)} candles 1h < {CANDLES_HISTORICO_MIN}) — não adicionado")
    return tem


# =============================================================================
# Cálculos auxiliares
# =============================================================================

def calcular_atr_pct(candles: list[dict], periodo: int = ATR_PERIODO) -> float:
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
    if len(candles_1h) < 24:
        return sum(c["volume"] for c in candles_1h) if candles_1h else 0.0
    ultimos = candles_1h[-168:]
    n_dias  = max(1, len(ultimos) // 24)
    total   = sum(c["volume"] for c in ultimos)
    return total / n_dias


def construir_dados_mexc(symbol, ticker, candles_1h, oi_24h_anterior):
    preco    = float(ticker.get("lastPrice", 0))
    vol_24h  = float(ticker.get("volume24", 0))
    oi_atual = float(ticker.get("holdVol", 0))
    high_24h = float(ticker.get("upper24Price", preco * 1.02))
    low_24h  = float(ticker.get("lower24Price", preco * 0.98))
    preco_change_pct = float(ticker.get("priceChangeRate", 0))

    atr_pct      = calcular_atr_pct(candles_1h)
    vol_media_7d = calcular_volume_media_7d(candles_1h)

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

    oi_change = (oi_atual - oi_24h_anterior) / oi_24h_anterior if oi_24h_anterior > 0 else 0.0
    funding   = float(ticker.get("fundingRate", 0))

    if funding < -0.00005:
        ls_ratio = 0.5
    elif funding > 0.00005:
        ls_ratio = 1.5
    else:
        ls_ratio = 1.0

    coinglass = DadosCoinglass(
        ticker=symbol,
        oi_change_24h_pct=oi_change,
        funding_rate=funding,
        ls_ratio=ls_ratio,
    )

    return mexc, coinglass, atr_pct


# =============================================================================
# BTC
# =============================================================================

def obter_regime_btc(candles_btc_1h):
    if len(candles_btc_1h) < BTC_EMA_PERIODO + 2:
        return True, 0.0, 0.0
    closes = [c["close"] for c in candles_btc_1h]
    ema21  = _ema(closes, BTC_EMA_PERIODO)
    preco  = candles_btc_1h[-1]["close"]

    def var_pct(c):
        return abs(c["close"] - c["open"]) / c["open"] if c["open"] > 0 else 0.0

    return preco > ema21, var_pct(candles_btc_1h[-2]), var_pct(candles_btc_1h[-3])


# =============================================================================
# REVISÃO DO UNIVERSO — duas funções separadas
# =============================================================================

def _verificar_novos_tokens(estado_json: dict, tickers: dict, agora_utc: str) -> list[str]:
    """
    Corre em TODOS os scans pesados.
    Adiciona tokens novos elegíveis que tenham histórico suficiente (168 candles 1h).
    Retorna lista de symbols adicionados.
    """
    adicionados = []
    for symbol, ticker in tickers.items():
        if symbol == BTC_TICKER:
            continue
        if symbol in estado_json:
            continue

        volume24 = float(ticker.get("volume24", 0))
        if volume24 < UNIVERSO_VOLUME_MIN_USD:
            continue

        # Verificar histórico antes de adicionar
        if not token_tem_historico_suficiente(symbol):
            continue

        log.info(f"[{symbol}] Novo token adicionado ao universo (vol=${volume24:,.0f})")
        estado_json[symbol] = token_para_dict(
            EstadoToken.novo(symbol, agora_utc),
            {
                "categoria":       _inferir_categoria(symbol),
                "oi_atual":        float(ticker.get("holdVol", 0)),
                "oi_inicio_dia":   float(ticker.get("holdVol", 0)),
                "volume_media_7d": volume24,
                "atr_1h_pct":      0.008,
                "oi_30m_anterior": float(ticker.get("holdVol", 0)),
            },
        )
        adicionados.append(symbol)

    if adicionados:
        log.info(f"Novos tokens adicionados neste scan: {len(adicionados)} — {adicionados}")
    return adicionados


def _rever_universo_completa(estado_json: dict, tickers: dict, agora_utc: str) -> list[str]:
    """
    Corre APENAS às 06h Lisboa.
    Gere grace period, remoções e também chama _verificar_novos_tokens.
    Retorna lista de symbols removidos.
    """
    log.info("Revisão completa do universo (06h Lisboa)...")
    removidos = []

    for symbol, campos in list(estado_json.items()):
        if symbol == BTC_TICKER:
            continue

        ticker    = tickers.get(symbol)
        em_grace  = campos.get("grace_period", False)
        dias_grace = campos.get("grace_period_dias_restantes")

        # Token não encontrado na MEXC — possível delisted
        if not ticker:
            if not em_grace:
                campos["grace_period"] = True
                campos["grace_period_dias_restantes"] = UNIVERSO_GRACE_PERIOD_DIAS
                log.info(f"[{symbol}] Não encontrado na MEXC → grace period")
            elif dias_grace is not None:
                dias_grace -= 1
                campos["grace_period_dias_restantes"] = dias_grace
                if dias_grace <= 0:
                    log.info(f"[{symbol}] Grace period expirado (delisted) → removido")
                    removidos.append(symbol)
                    del estado_json[symbol]
            continue

        volume24 = float(ticker.get("volume24", 0))

        if volume24 < UNIVERSO_VOLUME_MIN_USD:
            if not em_grace:
                campos["grace_period"] = True
                campos["grace_period_dias_restantes"] = UNIVERSO_GRACE_PERIOD_DIAS
                log.info(f"[{symbol}] Volume baixo (${volume24:,.0f}) → grace period {UNIVERSO_GRACE_PERIOD_DIAS} dias")
            elif dias_grace is not None:
                dias_grace -= 1
                campos["grace_period_dias_restantes"] = dias_grace
                if dias_grace <= 0:
                    log.info(f"[{symbol}] Grace period expirado → removido")
                    removidos.append(symbol)
                    del estado_json[symbol]
        else:
            if em_grace:
                campos["grace_period"] = False
                campos["grace_period_dias_restantes"] = None
                log.info(f"[{symbol}] Grace period encerrado — volume recuperado")

    # Adicionar novos tokens
    _verificar_novos_tokens(estado_json, tickers, agora_utc)

    return removidos


# =============================================================================
# SCAN PESADO
# =============================================================================

def scan_pesado(hora_lisboa: int) -> None:
    agora_utc = datetime.now(TZ_UTC).isoformat()
    scan_id   = f"SC-{agora_utc[:10].replace('-','')}T{hora_lisboa:02d}h"
    log.info(f"=== SCAN PESADO {hora_lisboa}h Lisboa — {scan_id} ===")

    try:
        estado_json, sha = carregar_estado()
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    tickers = fetch_todos_tickers()
    if not tickers:
        log.error("MEXC falhou — scan abortado")
        return

    candles_btc = fetch_candles(BTC_TICKER, "Min60", 50)
    btc_acima_ema21, btc_var_actual, btc_var_anterior = obter_regime_btc(candles_btc)
    btc_volatil = verificar_btc_volatil(btc_var_actual)
    log.info(f"BTC: {'↑ acima' if btc_acima_ema21 else '↓ abaixo'} EMA21 | volátil={btc_volatil}")

    # ── Gestão do universo ────────────────────────────────────────────────────
    saidos_universo = []
    if hora_lisboa == UNIVERSO_REVISAO_HORA_LISBOA:
        # 06h — revisão completa (grace period, remoções, novos)
        saidos_universo = _rever_universo_completa(estado_json, tickers, agora_utc)
    else:
        # Outros pesados — só verificar novos tokens
        _verificar_novos_tokens(estado_json, tickers, agora_utc)

    # ── Loop por token ────────────────────────────────────────────────────────
    novos_e2       = []
    novos_e3       = []
    novos_e3_nomes = []
    misses         = []

    for symbol, campos in list(estado_json.items()):
        if symbol == BTC_TICKER:
            continue

        ticker = tickers.get(symbol)
        if not ticker:
            continue

        categoria  = campos.get("categoria", "Memes")
        candles_1h = fetch_candles(symbol, "Min60", 200)
        if len(candles_1h) < 25:
            log.warning(f"[{symbol}] Candles insuficientes — skip")
            continue

        oi_24h_anterior = campos.get("oi_atual", 0.0)
        mexc_d, cg_d, atr_pct = construir_dados_mexc(symbol, ticker, candles_1h, oi_24h_anterior)

        token        = estado_para_token(campos, symbol)
        estado_antes = token.estado

        sl = calcular_sinais_scan_pesado(mexc_d, cg_d, "LONG")
        ss = calcular_sinais_scan_pesado(mexc_d, cg_d, "SHORT")

        funding_flag = verificar_funding_flag(cg_d, categoria)

        resultado = processar_scan_pesado(token, sl, ss, btc_acima_ema21, agora_utc)
        token     = resultado.novo_estado_token

        # Miss detection
        if estado_antes == ESTADO_PASSIVA and token.estado == ESTADO_PASSIVA:
            limiar = MISS_THRESHOLD_PCT.get(categoria, 0.10)
            high   = ticker.get("upper24Price", 0)
            low    = ticker.get("lower24Price", 0)
            if low > 0:
                amplitude = (high - low) / low
                if amplitude > limiar:
                    misses.append({"symbol": symbol, "amplitude": amplitude})

        # Heatmap se chegou a E3
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
            token.momento1_target_pct = leverage_resultado.target_pct
            token.momento1_sl_preco   = leverage_resultado.sl_preco
            token.momento1_tp1_preco  = leverage_resultado.tp1_preco
            token.momento1_tp2_preco  = leverage_resultado.tp2_preco
            token.momento1_leverage   = leverage_resultado.leverage
            token.momento1_metodo     = leverage_resultado.target_metodo

        # Acumular alertas
        sinais_dominantes = sl if token.direccao == "LONG" else ss
        for alerta in resultado.alertas:
            if alerta == Alerta.MOMENTO_0:
                novos_e2.append({
                    "symbol":       symbol,
                    "direccao":     token.direccao,
                    "score":        token.score_actual,
                    "sinais":       sinais_dominantes,
                    "funding_flag": funding_flag,
                })
            elif alerta == Alerta.MOMENTO_1:
                novos_e3.append({
                    "symbol":       symbol,
                    "direccao":     token.direccao,
                    "score":        token.score_actual,
                    "sinais":       sinais_dominantes,
                    "lev_r":        leverage_resultado,
                    "funding_flag": funding_flag,
                })
                novos_e3_nomes.append(symbol)

        campos_extra = {
            "categoria":       categoria,
            "oi_atual":        float(ticker.get("holdVol", 0)),
            "oi_inicio_dia":   campos.get("oi_inicio_dia", float(ticker.get("holdVol", 0)))
                               if hora_lisboa != UNIVERSO_REVISAO_HORA_LISBOA
                               else float(ticker.get("holdVol", 0)),
            "volume_media_7d": mexc_d.volume_media_7d,
            "atr_1h_pct":      atr_pct,
            "oi_30m_anterior": float(ticker.get("holdVol", 0)),
        }
        estado_json[symbol] = token_para_dict(token, campos_extra)

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

    # Guardar estado
    try:
        sha = guardar_estado(estado_json, sha, f"scan pesado {hora_lisboa}h — {scan_id}")
        log.info(f"state.json guardado (SHA ...{sha[-8:]})")
    except Exception as e:
        log.error(f"Falha ao guardar state.json: {e}")

    # 1 mensagem consolidada
    contagem_e2 = sum(1 for c in estado_json.values() if c.get("estado") == 2)
    contagem_e3 = sum(1 for c in estado_json.values() if c.get("estado") == 3)
    contagem_e4 = sum(1 for c in estado_json.values() if c.get("estado") == 4)
    enviar_resumo_scan_pesado(
        hora_lisboa=hora_lisboa,
        novos_e2=novos_e2,
        novos_e3=novos_e3,
        saidos_universo=saidos_universo,
        total_e2=contagem_e2,
        total_e3=contagem_e3,
        total_e4=contagem_e4,
        btc_acima_ema21=btc_acima_ema21,
    )

    _contar_e_log_scan_pesado(
        scan_id, hora_lisboa, estado_json,
        novos_e2, novos_e3_nomes, misses,
        btc_acima_ema21, candles_btc,
    )


# =============================================================================
# SCAN LEVE
# =============================================================================

def scan_leve() -> None:
    agora_utc   = datetime.now(TZ_UTC).isoformat()
    hora_lisboa = datetime.now(TZ_LISBOA).hour
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

    candles_btc     = fetch_candles(BTC_TICKER, "Min60", 30)
    btc_acima_ema21, btc_var_actual, _ = obter_regime_btc(candles_btc)
    btc_volatil     = verificar_btc_volatil(btc_var_actual)

    activos = {sym: c for sym, c in estado_json.items() if c.get("estado", 1) >= 2}
    if not activos:
        log.info("Sem tokens activos — scan leve sem updates")
        return

    encerrados = []
    degradados = []
    concluidos = []
    alterados  = False

    for symbol, campos in activos.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        token        = estado_para_token(campos, symbol)
        estado_antes = token.estado

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

        if estado_antes == ESTADO_BREAKOUT:
            conclusao_info = _processar_estado4(
                token, mexc_d, campos, estado_json, symbol, agora_utc
            )
            if conclusao_info:
                concluidos.append(conclusao_info)
            alterados = True
            continue

        if estado_antes == ESTADO_CONCLUIDO:
            token = estado_para_token({}, symbol)
            estado_json[symbol] = token_para_dict(
                token, {"categoria": campos.get("categoria", "Memes")}
            )
            alterados = True
            continue

        herdados  = token.get_sinais_herdados()
        sinais    = calcular_sinais_scan_leve(mexc_d, herdados, token.direccao)
        resultado = processar_scan_leve(token, sinais, btc_acima_ema21, agora_utc)
        token     = resultado.novo_estado_token

        for alerta in resultado.alertas:
            if alerta == Alerta.MOMENTO_3A:
                encerrados.append({
                    "symbol":   symbol,
                    "direccao": token.direccao,
                    "score":    token.score_actual,
                })
            elif alerta == Alerta.DEGRADACAO:
                degradados.append({
                    "symbol":   symbol,
                    "direccao": token.direccao,
                    "score":    token.score_actual,
                })

        if resultado.estado_novo != resultado.estado_anterior:
            log.info(f"[{symbol}] Estado {estado_antes}→{resultado.estado_novo}")
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

    enviar_resumo_scan_leve(
        hora_lisboa=hora_lisboa,
        encerrados=encerrados,
        degradados=degradados,
        concluidos=concluidos,
        estado_json=estado_json,
        btc_acima_ema21=btc_acima_ema21,
    )


def _processar_estado4(token, mexc_d, campos, estado_json, symbol, agora_utc):
    ts_trigger  = campos.get("trigger_timestamp", agora_utc)
    target_pct  = token.momento1_target_pct or 0.062
    checkpoints = {int(k): v for k, v in campos.get("checkpoints", {}).items()}

    checkpoints, novo_cp = actualizar_checkpoint(
        mexc_d.preco_actual,
        float(campos.get("trigger_preco", mexc_d.preco_actual)),
        token.direccao, ts_trigger, agora_utc, checkpoints,
    )
    if novo_cp:
        log.info(f"[{symbol}] Checkpoint +{novo_cp}h")
        log_move_update(symbol, novo_cp, checkpoints[novo_cp])

    dados_e4 = DadosEstado4(
        ticker=symbol,
        direccao=token.direccao,
        preco_atual=mexc_d.preco_actual,
        preco_trigger=float(campos.get("trigger_preco", mexc_d.preco_actual)),
        nivel_pre_breakout=float(campos.get("trigger_nivel_pre_breakout", mexc_d.preco_actual)),
        target_pct=target_pct,
        timestamp_trigger_utc=ts_trigger,
        timestamp_atual_utc=agora_utc,
        checkpoints=checkpoints,
    )

    conclusao = verificar_conclusao(dados_e4)
    conclusao_info = None

    if conclusao:
        log.info(f"[{symbol}] CONCLUÍDO: Cond {conclusao.condicao} ({conclusao.tipo})")
        token.estado = ESTADO_CONCLUIDO
        log_move_conclusao(
            symbol=symbol,
            conclusao=conclusao,
            preco_conclusao=mexc_d.preco_actual,
            checkpoints=checkpoints,
        )
        conclusao_info = {
            "symbol":    symbol,
            "direccao":  token.direccao,
            "ganho_pct": conclusao.ganho_atual_pct,
            "horas":     conclusao.horas_decorridas,
            "condicao":  conclusao.condicao,
        }

    campos.update({
        "estado":      token.estado,
        "checkpoints": {str(k): v for k, v in checkpoints.items()},
    })
    estado_json[symbol] = campos
    return conclusao_info


# =============================================================================
# SCAN BREAKOUT
# =============================================================================

def scan_breakout() -> None:
    agora_utc = datetime.now(TZ_UTC).isoformat()
    log.info(f"=== SCAN BREAKOUT {agora_utc[:16]} ===")

    try:
        estado_json, sha = carregar_estado()
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    candidatos = {
        sym: c for sym, c in estado_json.items()
        if c.get("estado", 1) == ESTADO_PRIORITARIO
    }
    if not candidatos:
        log.info("Sem tokens em E3 — scan breakout sem acção")
        return

    tickers       = fetch_todos_tickers()
    candles_btc   = fetch_candles(BTC_TICKER, "Min60", 10)
    _, btc_var_actual, btc_var_anterior = obter_regime_btc(candles_btc)
    btc_volatil   = verificar_btc_volatil(btc_var_actual)
    btc_normaliza = verificar_btc_normalizado(btc_var_actual, btc_var_anterior)
    alterados     = False

    for symbol, campos in candidatos.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        token           = estado_para_token(campos, symbol)
        oi_atual        = float(ticker.get("holdVol", 0))
        oi_30m_anterior = float(campos.get("oi_30m_anterior", oi_atual))

        candles_30m = fetch_candles(symbol, "Min30", TRIGGER_VOLUME_REFERENCIA_CANDLES + 1)
        if len(candles_30m) < 2:
            continue

        vol_ultimo_30m = candles_30m[-1]["volume"]
        referencia     = candles_30m[:-1]
        vol_media_30m  = sum(c["volume"] for c in referencia) / len(referencia) if referencia else 0.0
        oi_change_30m  = (oi_atual - oi_30m_anterior) / oi_30m_anterior if oi_30m_anterior > 0 else 0.0

        dados_bo = DadosBreakout(
            ticker=symbol,
            volume_ultimo_30m=vol_ultimo_30m,
            volume_media_30m_6h=vol_media_30m,
            oi_change_30m_pct=oi_change_30m,
            preco_atual=float(ticker.get("lastPrice", 0)),
            high_24h=float(ticker.get("upper24Price", 0)),
            low_24h=float(ticker.get("lower24Price", 0)),
        )

        if campos.get("trigger_pendente") and btc_normaliza:
            valido = verificar_trigger_ainda_valido(dados_bo, token.direccao)
            if valido:
                log.info(f"[{symbol}] Trigger pendente validado → Momento 2")
                _emitir_momento2(symbol, token, campos, ticker, agora_utc, btc_volatil_trigger=True)
                token.trigger_pendente       = False
                token.btc_volatil_no_trigger = True
                token.estado                 = ESTADO_BREAKOUT
            else:
                campos["trigger_pendente"] = False
            alterados = True
            estado_json[symbol] = token_para_dict(token, {
                "categoria":       campos.get("categoria"),
                "oi_30m_anterior": oi_atual,
            })
            continue

        resultado_bo = verificar_trigger_breakout(dados_bo, token.direccao)

        if resultado_bo.trigger_ativo:
            if btc_volatil:
                log.info(f"[{symbol}] Trigger detectado, BTC volátil — pendente")
                campos["trigger_pendente"]           = True
                campos["trigger_volume"]             = resultado_bo.volume_ratio
                campos["trigger_OI"]                 = oi_change_30m
                campos["trigger_preco"]              = dados_bo.preco_atual
                campos["trigger_nivel_pre_breakout"] = resultado_bo.nivel_pre_breakout
                campos["trigger_timestamp"]          = agora_utc
            else:
                log.info(f"[{symbol}] TRIGGER CONFIRMADO → Momento 2")
                _emitir_momento2(
                    symbol, token, campos, ticker, agora_utc,
                    btc_volatil_trigger=False,
                    nivel_pre_breakout=resultado_bo.nivel_pre_breakout,
                    trigger_volume=resultado_bo.volume_ratio,
                    trigger_oi=oi_change_30m,
                )
                token.estado                 = ESTADO_BREAKOUT
                token.trigger_pendente       = False
                token.trigger_timestamp      = agora_utc
                token.trigger_volume         = resultado_bo.volume_ratio
                token.trigger_OI             = oi_change_30m
                token.trigger_preco          = dados_bo.preco_atual
                token.btc_volatil_no_trigger = False
            alterados = True

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


def _emitir_momento2(symbol, token, campos, ticker, agora_utc,
                     btc_volatil_trigger=False, nivel_pre_breakout=0.0,
                     trigger_volume=0.0, trigger_oi=0.0):
    atr_pct = campos.get("atr_1h_pct", 0.008)
    preco   = float(ticker.get("lastPrice", 0))
    cat     = campos.get("categoria", "Memes")

    target_pct, metodo = obter_target_metodo_a(symbol, preco, token.direccao, atr_pct)
    leverage_r = calcular_leverage_niveis(
        preco_entry=preco,
        atr_1h_pct=atr_pct,
        categoria=cat,
        direccao=token.direccao,
        metodo=metodo,
        target_pct_override=target_pct if metodo == "A" else None,
    )

    notas = []
    if btc_volatil_trigger:
        notas.append("BTC_VOLATIL_NO_TRIGGER")
    if token.momento1_target_pct and abs(leverage_r.target_pct - token.momento1_target_pct) > 0.001:
        notas.append("TARGET_ACTUALIZADO")
    if campos.get("grace_period"):
        notas.append("GRACE_PERIOD_ACTIVO")

    sizing = SIZING_SALTO_DIRECTO if token.salto_directo else SIZING_VINHA_ESTADO3

    enviar_momento_breakout(
        symbol=symbol,
        token=token,
        lev_r=leverage_r,
        sizing=sizing,
        notas=notas,
        janela_horas=MOMENTO2_JANELA_ENTRADA_H,
        nivel_pre_breakout=nivel_pre_breakout,
    )

    log_move_update(symbol, 0, 0.0,
                    trigger_preco=preco,
                    leverage=leverage_r,
                    notas=" | ".join(notas))


# =============================================================================
# INFERIR CATEGORIA
# =============================================================================

def _inferir_categoria(symbol):
    s = symbol.lower()
    if any(k in s for k in ("pepe","doge","shib","floki","bonk","wif","meme","bome")):
        return "Memes"
    if any(k in s for k in ("ai","wld","fetch","agix","rndr","ocean")):
        return "AI"
    if any(k in s for k in ("uni","aave","crv","mkr","comp","snx","ldo","gmx")):
        return "DeFi"
    if any(k in s for k in ("sol","avax","bnb","ada","dot","atom","ftm")):
        return "Layer 1"
    if any(k in s for k in ("arb","op","matic","imx","zk","stx")):
        return "Layer 2"
    if any(k in s for k in ("axs","ronin","sand","mana","gala","beam")):
        return "Gaming/NFT"
    return "Infrastructure"


# =============================================================================
# LOG SCAN PESADO
# =============================================================================

def _contar_e_log_scan_pesado(scan_id, hora_lisboa, estado_json,
                               novos_e2, novos_e3_nomes, misses,
                               btc_acima_ema21, candles_btc):
    contagem = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for campos in estado_json.values():
        e = campos.get("estado", 1)
        contagem[e] = contagem.get(e, 0) + 1

    btc_preco = candles_btc[-1]["close"] if candles_btc else 0.0

    log.info(
        f"Scan {scan_id} — "
        f"E1:{contagem[1]} E2:{contagem[2]} E3:{contagem[3]} "
        f"E4:{contagem[4]} E5:{contagem[5]} | "
        f"NovosE2:{len(novos_e2)} NovosE3:{len(novos_e3_nomes)} Misses:{len(misses)}"
    )

    log_scan(
        scan_id=scan_id,
        hora_lisboa=hora_lisboa,
        btc_preco=btc_preco,
        filtro_btc="Longs e Shorts" if btc_acima_ema21 else "Apenas Shorts",
        contagem_estados=contagem,
        novos_estado2=len(novos_e2),
        novos_estado3=len(novos_e3_nomes),
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
        hora = datetime.now(TZ_LISBOA).hour
        scan_pesado(hora)
    elif tipo == "leve":
        scan_leve()
    elif tipo == "breakout":
        scan_breakout()
    else:
        log.error(f"SCAN_TIPO inválido: {tipo!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
