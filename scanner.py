#!/usr/bin/env python3
# =============================================================================
# scanner.py — Orchestrador principal do CFI v2.0
# Manual CFI v2.0 — Secção 8
# =============================================================================

from __future__ import annotations

import html
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
    S2B_OUTCOMES_PATH, S2B_CHECKPOINT_MIN, S2B_JANELA_TOTAL_MIN,
)
from signals import (
    DadosMEXC, DadosCoinglass, SinaisHerdados,
    calcular_sinais_scan_pesado, calcular_sinais_scan_leve,
    verificar_funding_flag,
    preco_ja_em_breakout, volume_confirma_breakout, contexto_informativo_s2b,
    snapshot_sinais_s2b,
    calcular_rsi,
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
        enviar_analise_token,
    )
except ImportError:
    def enviar_momento(*a, **k): pass
    def enviar_update_horario(*a, **k): pass
    def enviar_alerta_degradacao(*a, **k): pass
    def enviar_resumo_scan_pesado(*a, **k): pass
    def enviar_resumo_scan_leve(*a, **k): pass
    def enviar_momento_breakout(*a, **k): pass
    def enviar_analise_token(*a, **k): pass

try:
    from notion_logger import log_scan, log_deteccao, log_move_update, log_miss, log_move_conclusao
except ImportError:
    def log_scan(*a, **k): pass
    def log_deteccao(*a, **k): pass

# opiniao_claude / historico_cruzado — só usados por analise_token() (comando
# /analise_token do Telegram). Guardados com fallback: se qualquer um faltar
# ou falhar ao importar, analise_token() usa o veredicto por regras (já
# existente) em vez de rebentar o comando inteiro.
try:
    from opiniao_claude import gerar_opiniao
except ImportError:
    gerar_opiniao = None

try:
    from historico_cruzado import obter_historico_s2b, obter_historico_csa
except ImportError:
    def obter_historico_s2b(*a, **k): return {"em_observacao": False, "sinais": []}
    def obter_historico_csa(*a, **k): return {"alertas": []}
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


# =============================================================================
# S2B — TRACKING AUTOMÁTICO DE RESULTADOS
#
# Resposta a "como é que vamos saber se funciona?" (03/07/2026), revista em
# 04/07/2026: guarda-se o preço de 30 em 30 minutos (S2B_CHECKPOINT_MIN),
# até completar 24h (S2B_JANELA_TOTAL_MIN), sempre a partir de velas Min30
# históricas da MEXC — não do "lastPrice" ao vivo no momento em que o scan
# corre. Isto dá dados consistentes (mesma fonte para todos os pontos,
# passados ou recentes) e não obriga a correr o scan mais vezes: cada
# chamada de velas já traz de volta todos os pontos em falta até agora.
#
# Não há classificação automática de sucesso/insucesso — só se acumulam os
# dados em bruto. A estatística de onde e quando aparece sucesso, cruzada
# com outros indicadores, faz-se à parte quando houver volume suficiente.
# =============================================================================

def carregar_s2b_outcomes() -> tuple[list[dict], Optional[str]]:
    """
    Carrega o histórico de sinais S2b. Se o ficheiro ainda não existir no
    repo (primeira vez), devolve lista vazia e sha=None — guardar_s2b_
    outcomes() sabe criar o ficheiro nesse caso.
    """
    try:
        resp     = _github_request("GET", S2B_OUTCOMES_PATH)
        conteudo = base64.b64decode(resp["content"]).decode()
        return json.loads(conteudo), resp["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return [], None
        raise


def guardar_s2b_outcomes(outcomes: list[dict], sha: Optional[str], mensagem: str) -> None:
    conteudo_b64 = base64.b64encode(
        json.dumps(outcomes, indent=2, ensure_ascii=False).encode()
    ).decode()
    payload = {"message": mensagem, "content": conteudo_b64}
    if sha:
        payload["sha"] = sha
    _github_request("PUT", S2B_OUTCOMES_PATH, payload)


def actualizar_historico_precos(campos: dict, preco_actual: float, max_pontos: int = 24) -> None:
    """
    Mantém um histórico rolante do preço em campos["precos_horarios"]
    (até max_pontos leituras, ~1 por scan, ~24h no total). Sem qualquer
    chamada extra à API — reaproveita o lastPrice já obtido no ticker
    bulk, exactamente como já se faz para o OI (oi_30m_anterior).

    Substitui o riseFallRate da MEXC como base do filtro de preço do S2b.
    Motivo (descoberto 05/07/2026, caso SIREN_USDT): o riseFallRate da
    MEXC não é uma janela rolante de 24h — reinicia a um ponto fixo do
    dia (documentado como "price Change Percent in utc8"), o que faz a
    sensibilidade do filtro variar ao longo do dia sem relação com o
    movimento real do token. Esta função dá-nos uma janela verdadeiramente
    rolante, calculada por nós, ao mesmo custo (zero chamadas extra).
    """
    historico = campos.setdefault("precos_horarios", [])
    historico.append(preco_actual)
    if len(historico) > max_pontos:
        del historico[: len(historico) - max_pontos]


def calcular_variacao_rolante_preco(campos: dict) -> float:
    """
    Variação % entre o primeiro e o último ponto do histórico guardado por
    actualizar_historico_precos(). Com menos de 2 pontos (arranque, ou
    token que acabou de voltar a Estado 1) devolve 0.0 — fica sem sinal
    até acumular histórico, tal como o volume_media_7d faz nos primeiros
    dias. Ao fim de ~24h de scans (24 pontos), a janela fica completa.
    """
    historico = campos.get("precos_horarios", [])
    if len(historico) < 2:
        return 0.0
    preco_inicio, preco_fim = historico[0], historico[-1]
    if preco_inicio == 0:
        return 0.0
    return (preco_fim - preco_inicio) / preco_inicio



def registar_sinal_s2b(
    symbol: str, direccao: str, preco_entrada: float,
    contexto_score: int, timestamp_utc: str,
    sinais_lancamento: Optional[dict] = None,
) -> None:
    """
    Chamado no momento exacto em que um sinal S2b é confirmado.

    sinais_lancamento: snapshot dos 6 sinais clássicos (S1-S6) nesse
    instante — ver signals.snapshot_sinais_s2b(). None para compatibilidade
    com chamadas antigas; sinais registados sem isto ficam sem esse dado
    (não é reconstruível a posteriori, ver nota em actualizar_checkpoints_s2b).
    """
    try:
        outcomes, sha = carregar_s2b_outcomes()
        outcomes.append({
            "symbol":            symbol,
            "direccao":          direccao,
            "preco_entrada":     preco_entrada,
            "contexto_score":    contexto_score,
            "sinais_lancamento": sinais_lancamento,  # snapshot S1-S6 no momento do sinal, ou None
            "timestamp_entrada": timestamp_utc,
            "precos":            {},     # {"30": preco, "60": preco, ..., "1440": preco}
            "sinais_evolucao":   {},     # {"30": {snapshot S1-S6}, "60": {...}, ...}
            "completo":          False,  # True quando chega aos S2B_JANELA_TOTAL_MIN
        })
        guardar_s2b_outcomes(outcomes, sha, f"S2b: registar sinal {symbol} {direccao}")
        log.info(f"[{symbol}] S2b outcome registado (entrada={preco_entrada:.6g})")
    except Exception as e:
        # Nunca deixar o registo de tracking derrubar o scan — o alerta em
        # si já foi enviado, isto é só telemetria.
        log.error(f"[{symbol}] Falha a registar outcome S2b: {e}")


def _snapshot_evolucao_agora(symbol: str, direccao: str) -> Optional[dict]:
    """
    Snapshot AO VIVO dos 6 sinais (S1-S6) para um sinal S2b já aberto —
    preenche sinais_evolucao a cada checkpoint de 30 min. Pedido de
    Malaquias, 05/07/2026: "para podermos ter dados que nos permitam
    perceber os sucessos e as falhas" ao longo do tempo, não só no
    lançamento.

    Ao contrário dos preços (reconstruídos de velas históricas), o
    funding (S3) e o L/S ratio (S6) não têm histórico acessível — isto só
    reflecte o instante em que o scan corre. Se dois checkpoints de 30min
    ficarem em falta no mesmo scan (a cadência real do scan é ~1h), ambos
    recebem o mesmo snapshot — aproximação aceite, tal como já acontecia
    nos preços antes de passarmos a usar velas.

    Custo: 1 chamada de ticker + 1 de velas Min60 por sinal aberto, por
    scan leve — a somar à vela Min30 já usada para o preço.
    """
    try:
        ticker = fetch_ticker(symbol)
        candles_1h = fetch_candles(symbol, "Min60", 30)
        if not ticker or len(candles_1h) < 25:
            return None
        mexc_d, cg_d, _ = construir_dados_mexc(symbol, ticker, candles_1h, 0.0)
        return snapshot_sinais_s2b(mexc_d, cg_d, direccao)
    except Exception as e:
        log.error(f"[{symbol}] Falha a obter snapshot de evolução S2b: {e}")
        return None


def _preco_historico_mais_proximo(velas: list[dict], alvo_epoch: int) -> Optional[float]:
    """
    Devolve o close da vela Min30 mais próxima de alvo_epoch, desde que
    dentro de uma tolerância de 1 vela (30 min). None se não houver
    cobertura suficiente (ex.: símbolo muito recente ou falha da API).
    """
    if not velas:
        return None
    vela = min(velas, key=lambda c: abs(c["timestamp"] - alvo_epoch))
    if abs(vela["timestamp"] - alvo_epoch) <= S2B_CHECKPOINT_MIN * 60:
        return vela["close"]
    return None


def _preencher_precos_registo(registo: dict, velas: list[dict], agora: datetime) -> bool:
    """
    Preenche em registo["precos"] todos os checkpoints de S2B_CHECKPOINT_MIN
    em S2B_CHECKPOINT_MIN minutos que já passaram e ainda não têm valor,
    usando as velas Min30 fornecidas. Para cada checkpoint novo, também
    preenche registo["sinais_evolucao"] com um snapshot ao vivo dos 6
    sinais (ver _snapshot_evolucao_agora — 2 chamadas extra à API, só
    quando há pelo menos um checkpoint novo). Marca "completo" ao atingir
    S2B_JANELA_TOTAL_MIN. Devolve True se alterou algo.
    """
    try:
        entrada = datetime.fromisoformat(registo["timestamp_entrada"])
    except Exception:
        return False

    entrada_epoch    = int(entrada.timestamp())
    minutos_passados = int((agora - entrada).total_seconds() // 60)
    minutos_passados = min(minutos_passados, S2B_JANELA_TOTAL_MIN)

    precos             = registo.setdefault("precos", {})
    evolucao           = registo.setdefault("sinais_evolucao", {})
    alterado           = False
    novos_checkpoints  = []

    for minuto in range(S2B_CHECKPOINT_MIN, minutos_passados + 1, S2B_CHECKPOINT_MIN):
        chave = str(minuto)
        if chave in precos:
            continue
        preco = _preco_historico_mais_proximo(velas, entrada_epoch + minuto * 60)
        if preco is not None:
            precos[chave] = preco
            alterado = True
            novos_checkpoints.append(chave)

    if novos_checkpoints:
        snap = _snapshot_evolucao_agora(registo["symbol"], registo["direccao"])
        if snap is not None:
            for chave in novos_checkpoints:
                evolucao[chave] = snap
            alterado = True

    if minutos_passados >= S2B_JANELA_TOTAL_MIN and not registo.get("completo"):
        registo["completo"] = True
        alterado = True
        log.info(f"[{registo['symbol']}] S2b — janela de 24h completa ({len(precos)} pontos guardados)")

    return alterado



def actualizar_checkpoints_s2b(tickers: Optional[dict] = None) -> None:
    """
    Corre a cada scan leve. Para cada sinal ainda aberto — incluindo os do
    schema antigo (preco_1h/preco_4h/preco_24h/resultado), migrados na
    primeira passagem — vai buscar as velas Min30 da MEXC e preenche todos
    os checkpoints em falta. "tickers" já não é usado (mantido no parâmetro
    só para não obrigar a mexer no ponto de chamada) — os preços vêm sempre
    de velas históricas, não do lastPrice ao vivo.
    """
    try:
        outcomes, sha = carregar_s2b_outcomes()
    except Exception as e:
        log.error(f"Falha a carregar s2b_outcomes.json: {e}")
        return
    if not outcomes:
        return

    abertos = [o for o in outcomes if not o.get("completo", False)]
    if not abertos:
        return

    agora    = datetime.now(TZ_UTC)
    alterado = False

    for registo in abertos:
        symbol = registo.get("symbol")
        if not symbol:
            continue

        # ── Migração de registos do schema antigo ───────────────────────
        # Descarta-se a classificação GANHO/PERDA/NEUTRO antiga — os preços
        # em si são reconstruídos de raiz a partir das velas, mais precisos
        # e consistentes do que o lastPrice pontual que existia antes.
        if "precos" not in registo:
            registo["precos"] = {}
            for chave_antiga in ("preco_1h", "preco_4h", "preco_24h", "resultado"):
                registo.pop(chave_antiga, None)
            alterado = True

        try:
            velas = fetch_candles(symbol, "Min30", 60)  # cobre >24h com folga
        except Exception as e:
            log.error(f"[{symbol}] Falha a obter velas Min30 p/ S2b: {e}")
            continue
        if not velas:
            continue  # símbolo pode ter sido deslistado; tenta-se no próximo scan

        if _preencher_precos_registo(registo, velas, agora):
            alterado = True

    if alterado:
        guardar_s2b_outcomes(outcomes, sha, "S2b: actualizar preços (velas Min30)")


def calcular_taxa_sucesso_s2b() -> dict:
    """
    Contagens simples — usado pelo comando /s2b_stats do bot.py. Sem
    julgamento de sucesso/insucesso: isso faz-se à parte, fora do bot,
    quando houver dados e indicadores suficientes para começar a afunilar.
    """
    outcomes, _ = carregar_s2b_outcomes()
    completos = [o for o in outcomes if o.get("completo")]
    abertos   = [o for o in outcomes if not o.get("completo")]
    return {
        "total":     len(outcomes),
        "completos": len(completos),
        "abertos":   len(abertos),
    }


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


def fetch_ticker(symbol: str) -> Optional[dict]:
    """Ticker de um único símbolo — usado onde não faz sentido pedir o bulk todo
    (ex.: snapshot de evolução dos sinais S2b, 1x por sinal aberto por scan)."""
    dados = _mexc_get("/contract/ticker", {"symbol": symbol})
    if not dados or not isinstance(dados, dict):
        return None
    return dados


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
    preco_change_pct = float(ticker.get("riseFallRate", 0))

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

        # Histórico rolante de preço — mesma lógica do scan leve, aqui de
        # graça porque já percorremos todos os tokens com ticker em mão.
        preco_actual = float(ticker.get("lastPrice", 0))
        if preco_actual:
            actualizar_historico_precos(campos, preco_actual)

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

    # Tracking automático de resultados S2b — barato (reaproveita tickers
    # já obtidos), corre em todos os scans leve. Nunca bloqueia o resto.
    try:
        actualizar_checkpoints_s2b(tickers)
    except Exception as e:
        log.error(f"actualizar_checkpoints_s2b falhou (não bloqueante): {e}")

    activos = {sym: c for sym, c in estado_json.items() if c.get("estado", 1) >= 2}

    encerrados = []
    degradados = []
    concluidos = []
    novos_e2_s2b = []       # sinais gerados pelo gatilho S2b (fora do calendário pesado)
    alterados  = False

    # ── S2b — varrimento barato de tokens em Estado 1 ───────────────────────
    # Manual CFI v2.0 — extensão 03/07/2026. Ver docstring de
    # signals.preco_ja_em_breakout() para o problema que resolve.
    #
    # Passo 1 (grátis): filtra pela nossa própria janela rolante de preço —
    # sem qualquer chamada extra à API (reaproveita o lastPrice do ticker
    # bulk), aplicado aos ~500 tokens em E1 de uma vez. Substituiu o
    # riseFallRate da MEXC em 05/07/2026 — ver docstring de
    # actualizar_historico_precos() para o motivo.
    # Passo 2 (com custo): só os sobreviventes do passo 1 pagam o fetch de candles.
    e1_tokens = {
        sym: c for sym, c in estado_json.items()
        if c.get("estado", 1) == 1 and sym != BTC_TICKER
    }
    candidatos_s2b = []
    for symbol, campos in e1_tokens.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        preco_actual = float(ticker.get("lastPrice", 0))
        if preco_actual:
            actualizar_historico_precos(campos, preco_actual)
            alterados = True
        preco_change = calcular_variacao_rolante_preco(campos)
        passa_preco, direccao_provavel = preco_ja_em_breakout(preco_change)
        if passa_preco:
            candidatos_s2b.append((symbol, campos, ticker, direccao_provavel))

    if candidatos_s2b:
        nomes = ", ".join(f"{s}({d})" for s, _, _, d in candidatos_s2b)
        log.info(
            f"S2b: {len(candidatos_s2b)} candidato(s) passaram o filtro de preço "
            f"(grátis) — a verificar volume: {nomes}"
        )

    for symbol, campos, ticker, direccao_provavel in candidatos_s2b:
        candles_1h = fetch_candles(symbol, "Min60", 30)
        if len(candles_1h) < 25:
            continue

        passa_vol, vol_dir_pct = volume_confirma_breakout(candles_1h, direccao_provavel)
        if not passa_vol:
            log.info(
                f"[{symbol}] S2b: preço confirmou ({direccao_provavel}) mas volume "
                f"ainda não confirma ({vol_dir_pct*100:.1f}%) — ignorado"
            )
            continue

        # ── A partir daqui, S2b (preço + volume) É o sinal ──────────────────
        # Não passa pela fórmula de acumulação silenciosa (calcular_sinais_
        # scan_pesado) — decisão 03/07/2026: S1/S4 exigem mercado "quieto",
        # incompatível por definição com um breakout já confirmado. S3/S5/S6
        # ficam registados como contexto informativo, não como portão,
        # para começarmos a ter dados reais de taxa de sucesso.
        oi_24h_anterior = campos.get("oi_atual", 0.0)
        mexc_d, cg_d, atr_pct = construir_dados_mexc(symbol, ticker, candles_1h, oi_24h_anterior)
        contexto = contexto_informativo_s2b(mexc_d, cg_d, direccao_provavel)

        log.info(
            f"[{symbol}] S2b CONFIRMADO ({direccao_provavel}, "
            f"vol_dir={vol_dir_pct*100:.1f}%, contexto={contexto['contexto_score']}/3) — a gerar alerta"
        )

        token = estado_para_token(campos, symbol)
        token.estado                   = ESTADO_RADAR
        token.direccao                 = direccao_provavel
        token.score_actual             = contexto["contexto_score"]  # informativo, 0-3
        token.score_anterior           = 0
        token.scans_consecutivos       = 1
        token.contador_estado2         = 0
        token.timestamp_entrada_estado = agora_utc
        token.ultimo_scan              = agora_utc
        token.salto_directo            = False

        novos_e2_s2b.append({
            "symbol":         symbol,
            "direccao":       direccao_provavel,
            "score":          contexto["contexto_score"],
            "preco_var_pct":  float(ticker.get("riseFallRate", 0)) * 100,
            "vol_dir_pct":    vol_dir_pct * 100,
            "contexto":       contexto,
            "gatilho":        "S2b",
        })

        registar_sinal_s2b(
            symbol=symbol,
            direccao=direccao_provavel,
            preco_entrada=float(ticker.get("lastPrice", mexc_d.preco_actual)),
            contexto_score=contexto["contexto_score"],
            timestamp_utc=agora_utc,
            sinais_lancamento=snapshot_sinais_s2b(mexc_d, cg_d, direccao_provavel),
        )

        campos_extra = {
            "categoria":       campos.get("categoria", "Memes"),
            "oi_atual":        float(ticker.get("holdVol", 0)),
            "oi_inicio_dia":   campos.get("oi_inicio_dia", float(ticker.get("holdVol", 0))),
            "volume_media_7d": mexc_d.volume_media_7d,
            "atr_1h_pct":      atr_pct,
            "oi_30m_anterior": float(ticker.get("holdVol", 0)),
        }
        estado_json[symbol] = token_para_dict(token, campos_extra)
        alterados = True

    if not activos and not novos_e2_s2b:
        log.info("Sem tokens activos e sem detecções S2b — scan leve sem updates")
        return

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
            preco_change_24h_pct=float(ticker.get("riseFallRate", 0)),
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
        novos_e2_s2b=novos_e2_s2b,
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
# ANÁLISE AD-HOC — /analise_token SYMBOL (comando Telegram)
# =============================================================================

def analise_token() -> None:
    """
    Análise pontual de um único token, disparada pelo comando /analise_token
    do bot.py. Não faz parte do calendário de scans, não altera o
    estado_json — é só uma "opinião" formatada, com os mesmos dados e
    sinais que o resto do sistema usa (S1-S6, RSI, EMA, ATR, funding, OI).
    """
    symbol = os.environ.get("SYMBOL", "").strip().upper()
    if not symbol:
        log.error("SYMBOL não definido para SCAN_TIPO=analise_token")
        return
    if not symbol.endswith("_USDT"):
        symbol = symbol + "_USDT"

    log.info(f"=== ANÁLISE AD-HOC: {symbol} ===")

    tickers = fetch_todos_tickers()
    ticker  = tickers.get(symbol) if tickers else None
    if not ticker:
        enviar_analise_token(f"❓ <b>{symbol}</b> não encontrado na MEXC (futuros USDT-M).")
        return

    candles_1h = fetch_candles(symbol, "Min60", 200)
    if len(candles_1h) < 25:
        enviar_analise_token(f"❌ <b>{symbol}</b> — candles insuficientes para análise ({len(candles_1h)}/25 mín.).")
        return

    # OI de referência: se o token já está no universo, usa o último valor
    # conhecido; caso contrário usa o OI actual (variação fica a 0%, informativo).
    try:
        estado_json, _ = carregar_estado()
    except Exception:
        estado_json = {}
    campos_existentes = estado_json.get(symbol, {})
    oi_24h_anterior    = campos_existentes.get("oi_atual", float(ticker.get("holdVol", 0)))

    mexc_d, cg_d, atr_pct = construir_dados_mexc(symbol, ticker, candles_1h, oi_24h_anterior)

    candles_btc = fetch_candles(BTC_TICKER, "Min60", 30)
    btc_acima_ema21, _, _ = obter_regime_btc(candles_btc)

    sl = calcular_sinais_scan_pesado(mexc_d, cg_d, "LONG")
    ss = calcular_sinais_scan_pesado(mexc_d, cg_d, "SHORT")

    closes = [c["close"] for c in candles_1h]
    rsi14  = calcular_rsi(closes, 14)
    ema9   = _ema(closes[-30:], 9)
    ema21  = _ema(closes[-30:], 21)
    preco  = mexc_d.preco_actual
    estrutura_ema = "empilhadas ↑ (preço>EMA9>EMA21)" if preco > ema9 > ema21 else (
        "empilhadas ↓ (preço<EMA9<EMA21)" if preco < ema9 < ema21 else "entrelaçadas, sem tendência clara"
    )

    ja_no_universo = symbol in estado_json
    estado_actual  = campos_existentes.get("estado", 1) if ja_no_universo else None
    estado_str = f"Estado {estado_actual}" if ja_no_universo else "fora do universo activo do CFI"

    # ── Veredicto por regras — sempre calculado, serve de fallback ───────────
    if sl.score >= 4 and sl.score >= ss.score + 2:
        veredicto_regras = f"🟢 Sinais LONG a alinhar-se ({sl.score}/6) — vale vigiar de perto."
    elif ss.score >= 4 and ss.score >= sl.score + 2:
        veredicto_regras = f"🔴 Sinais SHORT a alinhar-se ({ss.score}/6) — vale vigiar de perto."
    elif rsi14 is not None and rsi14 >= 70:
        veredicto_regras = f"🟡 Sobrecomprado (RSI {rsi14:.0f}) — cuidado com entradas tardias em LONG."
    elif rsi14 is not None and rsi14 <= 30:
        veredicto_regras = f"🟡 Sobrevendido (RSI {rsi14:.0f}) — possível zona de ressalto, sem confirmação ainda."
    elif atr_pct < 0.01 and abs(mexc_d.preco_change_24h_pct) < 0.03:
        veredicto_regras = "⚪ Mercado morto — sem volume, sem volatilidade, sem sinal para explorar."
    else:
        veredicto_regras = "⚪ Sem confluência clara nos dois sentidos — nada de accionável agora."

    # ── Cruzamento com histórico já detectado pelo S2b/CSA (best-effort) ────
    hist_s2b = obter_historico_s2b(symbol)
    hist_csa = obter_historico_csa(symbol)

    # ── Opinião via Claude (usa o histórico acima); cai para veredicto_regras
    # se a API falhar ou o módulo não estiver disponível ──────────────────
    opiniao_texto = None
    if gerar_opiniao is not None:
        try:
            opiniao_texto = gerar_opiniao(
                symbol=symbol,
                preco=preco,
                var_24h_pct=mexc_d.preco_change_24h_pct * 100,
                funding=cg_d.funding_rate,
                oi=float(ticker.get("holdVol", 0)),
                volume_24h=mexc_d.volume_24h,
                rsi14=rsi14,
                atr_pct=atr_pct,
                estrutura_ema=estrutura_ema,
                score_long=sl.score,
                resumo_long=sl.resumo(),
                score_short=ss.score,
                resumo_short=ss.resumo(),
                no_universo=ja_no_universo,
                historico_s2b=hist_s2b,
                historico_csa=hist_csa,
            )
        except Exception as e:
            log.warning(f"[{symbol}] gerar_opiniao falhou, a usar veredicto por regras: {e}")

    # CORRECÇÃO 18/07/2026: escapar o texto livre do Claude antes de o meter
    # na mensagem — a mensagem usa parse_mode=HTML (ver notificacoes.py), e
    # um simples "<" ou "&" na opinião (ex: "RSI < 30") parte o parser do
    # Telegram. O 400 Bad Request resultante era engolido silenciosamente:
    # o scanner.py não verifica o retorno de enviar_analise_token(), por
    # isso o job do GitHub Actions reportava "success" mesmo sem a
    # mensagem chegar ao Telegram. Caso real: /analise_token SIREN,
    # 18/07/2026 14:26 UTC — opinião gerada (598 chars), Telegram
    # rejeitou com HTTP 400, scan terminou como sucesso na mesma.
    if opiniao_texto:
        opiniao_texto = html.escape(opiniao_texto)

    veredicto = opiniao_texto if opiniao_texto else veredicto_regras

    n_sinais_s2b = len(hist_s2b.get("sinais", []))
    n_alertas_csa = len(hist_csa.get("alertas", []))
    linha_historico = (
        f"Histórico: S2b {n_sinais_s2b}x"
        + (" (em observação agora)" if hist_s2b.get("em_observacao") else "")
        + f" · CSA {n_alertas_csa}x"
    )

    linhas = [
        f"🔍 <b>Análise ad-hoc — {symbol}</b>",
        "",
        f"Preço: <code>{preco:.6g}</code>  ({mexc_d.preco_change_24h_pct*100:+.1f}% 24h)",
        f"Funding: <code>{cg_d.funding_rate*100:+.4f}%</code>",
        f"OI actual: <code>${float(ticker.get('holdVol', 0)):,.0f}</code>",
        f"Volume 24h: <code>${mexc_d.volume_24h:,.0f}</code>",
        f"RSI(14): <code>{rsi14:.1f}</code>" if rsi14 is not None else "RSI(14): —",
        f"ATR(1h): <code>{atr_pct*100:.2f}%</code>",
        f"EMA9/21: {html.escape(estrutura_ema)}",
        "",
        f"<b>S1-S6 LONG:</b> {sl.score}/6  ({sl.resumo()})",
        f"<b>S1-S6 SHORT:</b> {ss.score}/6  ({ss.resumo()})",
        "",
        f"No teu universo: {estado_str}." if ja_no_universo else
        "Não está no universo activo do CFI (fora dos critérios de volume/listagem).",
        "",
        linha_historico,
        "",
        (f"🧠 {veredicto}" if opiniao_texto else veredicto),
        "",
        "<i>Leitura técnica, não é conselho de investimento.</i>",
    ]

    enviar_analise_token("\n".join(linhas))
    log.info(f"[{symbol}] Análise ad-hoc enviada — LONG {sl.score}/6 SHORT {ss.score}/6")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    tipo = os.environ.get("SCAN_TIPO", "").lower()
    if not tipo:
        log.error("SCAN_TIPO não definido. Usar: s2b | analise_token")
        sys.exit(1)

    if tipo == "s2b":
        import s2b_v2
        s2b_v2.scan_s2b()
    elif tipo == "analise_token":
        analise_token()
    elif tipo in ("pesado", "leve", "breakout"):
        # CONGELADO 05/07/2026 (decisão do Malaquias): o pipeline clássico
        # E1-E5 parou de dar sinais úteis com o mercado neste regime — o
        # S2b (s2b_v2.py) passa a ser o único mecanismo activo. Isto falha
        # alto de propósito, em vez de continuar a correr silenciosamente,
        # para o caso de um cron job antigo não ter sido removido a tempo
        # do cron-job.org. O código de scan_leve/scan_pesado/scan_breakout
        # fica no repo, intacto, para referência futura — só deixou de ser
        # chamado a partir daqui.
        log.error(
            f"SCAN_TIPO={tipo!r} está CONGELADO desde 05/07/2026 — "
            f"o único scan activo é 's2b'. Remove este cron job do "
            f"cron-job.org se ainda o vires disparar."
        )
        sys.exit(1)
    else:
        log.error(f"SCAN_TIPO inválido: {tipo!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
