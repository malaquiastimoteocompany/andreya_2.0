# =============================================================================
# triggers.py — Condições de breakout e de conclusão do Estado 4
# Manual CFI v2.0 — Secções 6.1, 6.2 e 6.4
#
# Três responsabilidades:
#   1. Verificar se as 3 condições de breakout estão satisfeitas (scan breakout)
#   2. Gerir BTC volátil/normalizado (pausa e retoma de alertas)
#   3. Verificar as 4 condições de conclusão do Estado 4 (scan leve horário)
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import (
    TRIGGER_VOLUME_30M_VS_6H_RATIO,
    TRIGGER_OI_30M_MIN_PCT,
    BTC_VOLATILIDADE_EXTREMA_PCT,
    BTC_NORMALIZADO_VAR_MAX_PCT,
    BTC_NORMALIZADO_N_CANDLES,
    CONCLUSAO_COND1_TEMPO_MIN_H,
    CONCLUSAO_COND2_TEMPO_MIN_H,
    CONCLUSAO_COND3_TEMPO_MIN_H,
    CONCLUSAO_COND4_TEMPO_MIN_H,
    CONCLUSAO_COND2_REVERSAO_FRACCAO,
    CONCLUSAO_CHECKPOINTS_H,
)


# -----------------------------------------------------------------------------
# Estruturas de dados
# -----------------------------------------------------------------------------

@dataclass
class DadosBreakout:
    """
    Dados para o scan de breakout (a cada 30 min, só Estado 3).
    Fontes: MEXC API (volume, preço) + Coinglass Playwright (OI 30m).

    volume_media_30m_6h: média dos últimos 12 candles de 30 min (= 6h de referência).
                         Calculado pelo fetcher antes de chamar verificar_trigger_breakout().
    oi_change_30m_pct:   variação % de OI nos últimos 30 min (ex: +0.04 = +4%).
    """
    ticker: str
    volume_ultimo_30m: float        # volume do candle de 30min mais recente
    volume_media_30m_6h: float      # média dos 12 candles de 30min anteriores
    oi_change_30m_pct: float        # variação OI últimos 30min
    preco_atual: float
    high_24h: float
    low_24h: float


@dataclass
class ResultadoBreakout:
    """Resultado da verificação de trigger (manual 6.1)."""
    trigger_ativo: bool
    direccao: str                   # "LONG" ou "SHORT"
    condicao1_ok: bool              # volume > 200% da média 6h
    condicao2_ok: bool              # OI 30m > +3%
    condicao3_ok: bool              # preço > High 24h (LONG) ou < Low 24h (SHORT)
    volume_ratio: float             # volume_30m / media_30m (ex: 2.5 = 250%)
    oi_change_pct: float            # valor real
    nivel_pre_breakout: float       # High 24h (LONG) ou Low 24h (SHORT) — para gravar no JSON


@dataclass
class DadosEstado4:
    """
    Dados para monitorização do Estado 4 (scan leve horário).
    Fontes: state.json + MEXC API (preço atual).

    checkpoints: dict com horas desde o trigger como chave e gain % como valor.
                 Chaves válidas: 1, 2, 4, 8, 24.
                 Gain = (preco - trigger) / trigger para LONG
                      = (trigger - preco) / trigger para SHORT
                 Positivo = favorável para a posição.
    """
    ticker: str
    direccao: str                   # "LONG" ou "SHORT"
    preco_atual: float
    preco_trigger: float            # preço no momento do trigger (Momento 2)
    nivel_pre_breakout: float       # High 24h (LONG) ou Low 24h (SHORT) no trigger
    target_pct: float               # distância do target em % (positivo; ex: 0.062 = 6.2%)
    timestamp_trigger_utc: str      # ISO UTC
    timestamp_atual_utc: str        # ISO UTC
    checkpoints: dict = field(default_factory=dict)   # {1: 0.032, 2: 0.058, ...}


@dataclass
class ResultadoConclusao:
    """Resultado da verificação de conclusão do Estado 4 (manual 6.4)."""
    concluido: bool
    condicao: int                   # 1, 2, 3 ou 4
    tipo: str                       # "POSITIVA", "NEUTRA" ou "NEGATIVA"
    ganho_atual_pct: float          # gain % actual desde o trigger
    ganho_maximo_pct: float         # maior gain registado nos checkpoints
    horas_decorridas: float         # horas desde o trigger até à conclusão


# -----------------------------------------------------------------------------
# BTC — volatilidade e normalização (manual 10.1 e 6.2)
# -----------------------------------------------------------------------------

def verificar_btc_volatil(variacao_1h_pct: float) -> bool:
    """
    BTC volátil se a variação absoluta do último candle de 1h > 3%.
    Quando True: scan de breakout regista trigger mas NÃO envia Momento 2.
    Manual secção 10.1.
    """
    return abs(variacao_1h_pct) > BTC_VOLATILIDADE_EXTREMA_PCT


def verificar_btc_normalizado(
    var_candle_atual: float,
    var_candle_anterior: float,
) -> bool:
    """
    BTC normalizado quando 2 candles consecutivos de 1h
    com variação absoluta < 1.5% cada (manual 6.2).
    Quando True: verificar se trigger pendente ainda é válido → enviar Momento 2.

    var_candle_atual:    variação absoluta do candle de 1h mais recente fechado.
    var_candle_anterior: variação absoluta do candle de 1h anterior.
    """
    return (
        abs(var_candle_atual)    < BTC_NORMALIZADO_VAR_MAX_PCT and
        abs(var_candle_anterior) < BTC_NORMALIZADO_VAR_MAX_PCT
    )


# -----------------------------------------------------------------------------
# Trigger de breakout (manual 6.1)
# -----------------------------------------------------------------------------

def verificar_trigger_breakout(
    dados: DadosBreakout,
    direccao: str,
) -> ResultadoBreakout:
    """
    Verifica se as 3 condições de breakout estão satisfeitas em simultâneo.
    Deve ser chamada a cada 30 min para tokens em Estado 3.

    Condição 1: volume último 30m > 200% da média das últimas 6h (12 candles de 30m).
    Condição 2: OI change 30m > +3%.
    Condição 3 LONG:  preço atual > High 24h.
    Condição 3 SHORT: preço atual < Low 24h.

    As 3 têm de ser True em simultâneo para o trigger activar.
    Manual secção 6.1.
    """
    if direccao not in ("LONG", "SHORT"):
        raise ValueError(f"Direcção inválida: '{direccao}'. Usar 'LONG' ou 'SHORT'.")

    # Condição 1 — Volume
    volume_ratio = (
        dados.volume_ultimo_30m / dados.volume_media_30m_6h
        if dados.volume_media_30m_6h > 0 else 0.0
    )
    cond1 = volume_ratio > TRIGGER_VOLUME_30M_VS_6H_RATIO

    # Condição 2 — OI
    cond2 = dados.oi_change_30m_pct > TRIGGER_OI_30M_MIN_PCT

    # Condição 3 — Preço vs High/Low 24h
    if direccao == "LONG":
        cond3  = dados.preco_atual > dados.high_24h
        nivel  = dados.high_24h
    else:
        cond3  = dados.preco_atual < dados.low_24h
        nivel  = dados.low_24h

    return ResultadoBreakout(
        trigger_ativo=cond1 and cond2 and cond3,
        direccao=direccao,
        condicao1_ok=cond1,
        condicao2_ok=cond2,
        condicao3_ok=cond3,
        volume_ratio=volume_ratio,
        oi_change_pct=dados.oi_change_30m_pct,
        nivel_pre_breakout=nivel,
    )


def verificar_trigger_ainda_valido(
    dados: DadosBreakout,
    direccao: str,
) -> bool:
    """
    Após BTC normalizar, verifica se o trigger pendente ainda é válido.
    Critério: preço ainda acima de High 24h (LONG) ou abaixo de Low 24h (SHORT).
    Manual secção 6.2: "verifica se trigger ainda válido".
    Não re-verifica condições de volume nem OI (já não são 'últimos 30min').
    """
    if direccao == "LONG":
        return dados.preco_atual > dados.high_24h
    return dados.preco_atual < dados.low_24h


# -----------------------------------------------------------------------------
# Funções auxiliares para Estado 4
# -----------------------------------------------------------------------------

def _calcular_ganho_pct(
    preco_atual: float,
    preco_trigger: float,
    direccao: str,
) -> float:
    """
    Ganho actual em % desde o trigger.
    LONG:  ganho = (atual - trigger) / trigger   → positivo se subiu
    SHORT: ganho = (trigger - atual) / trigger   → positivo se desceu
    Positivo = favorável para a posição.
    """
    if preco_trigger == 0:
        return 0.0
    if direccao == "LONG":
        return (preco_atual - preco_trigger) / preco_trigger
    return (preco_trigger - preco_atual) / preco_trigger


def _horas_decorridas(ts_trigger: str, ts_atual: str) -> float:
    """Calcula horas decorridas entre dois timestamps ISO UTC."""
    try:
        t0 = datetime.fromisoformat(ts_trigger)
        t1 = datetime.fromisoformat(ts_atual)
        return (t1 - t0).total_seconds() / 3600.0
    except Exception:
        return 0.0


# -----------------------------------------------------------------------------
# Actualização de checkpoints hora-a-hora (manual 6.4 / Base 4)
# -----------------------------------------------------------------------------

def actualizar_checkpoint(
    preco_atual: float,
    preco_trigger: float,
    direccao: str,
    timestamp_trigger_utc: str,
    timestamp_atual_utc: str,
    checkpoints_existentes: dict,
) -> tuple[dict, Optional[int]]:
    """
    Verifica se um novo checkpoint (+1h/+2h/+4h/+8h/+24h) deve ser registado.
    Só regista um checkpoint por chamada (o próximo que ainda não foi preenchido).

    Retorna: (checkpoints_actualizados, hora_do_novo_checkpoint_ou_None)

    Limitação conhecida: picos intra-hora não são capturados.
    Imprecisão ocasional aceite (manual 6.4, Condição 2).
    """
    horas  = _horas_decorridas(timestamp_trigger_utc, timestamp_atual_utc)
    ganho  = _calcular_ganho_pct(preco_atual, preco_trigger, direccao)
    novos  = dict(checkpoints_existentes)
    novo_h = None

    for h in CONCLUSAO_CHECKPOINTS_H:          # [1, 2, 4, 8, 24]
        if horas >= h and h not in novos:
            novos[h] = ganho
            novo_h = h
            break                               # um de cada vez

    return novos, novo_h


# -----------------------------------------------------------------------------
# Verificação de conclusão — Estado 4 (manual 6.4)
# -----------------------------------------------------------------------------

def verificar_conclusao(dados: DadosEstado4) -> Optional[ResultadoConclusao]:
    """
    Verifica se alguma das 4 condições de conclusão está satisfeita.
    Retorna ResultadoConclusao se o move concluiu, ou None se ainda está activo.

    Condições (mutuamente exclusivas, verificadas por esta ordem):
      1 — Target atingido:                    sem tempo mínimo → POSITIVA
      3 — Breakout falso (≥1h mínimo):        preço voltou ao nível pré-breakout → NEGATIVA
      2 — Reversão após ganho (≥2h mínimo):   reverteu ≥50% do máximo → NEUTRA
      4 — Tempo esgotado:                     ≥24h → NEUTRA

    Nota: condição 3 verificada antes de 2 — se o preço voltou ao pré-breakout
    é NEGATIVA (mais específico que NEUTRA por reversão parcial).
    Manual secção 6.4.
    """
    ganho_atual  = _calcular_ganho_pct(dados.preco_atual, dados.preco_trigger, dados.direccao)
    horas        = _horas_decorridas(dados.timestamp_trigger_utc, dados.timestamp_atual_utc)

    # Ganho máximo: maior valor positivo registado nos checkpoints
    ganho_maximo = max(
        (v for v in dados.checkpoints.values() if v > 0),
        default=0.0,
    )

    def _resultado(condicao: int, tipo: str) -> ResultadoConclusao:
        return ResultadoConclusao(
            concluido=True,
            condicao=condicao,
            tipo=tipo,
            ganho_atual_pct=ganho_atual,
            ganho_maximo_pct=ganho_maximo,
            horas_decorridas=horas,
        )

    # ── Condição 1: target atingido (sem tempo mínimo) ───────────────────────
    if horas >= CONCLUSAO_COND1_TEMPO_MIN_H and ganho_atual >= dados.target_pct:
        return _resultado(1, "POSITIVA")

    # ── Condição 3: breakout falso (mínimo 1h) ───────────────────────────────
    # Preço voltou ao nível que foi quebrado no trigger.
    if horas >= CONCLUSAO_COND3_TEMPO_MIN_H:
        falso_breakout = (
            dados.preco_atual <= dados.nivel_pre_breakout
            if dados.direccao == "LONG"
            else dados.preco_atual >= dados.nivel_pre_breakout
        )
        if falso_breakout:
            return _resultado(3, "NEGATIVA")

    # ── Condição 2: reversão ≥50% do ganho máximo (mínimo 2h) ───────────────
    # Exemplo manual: max=5.8%, actual=2.5% → reverteu 3.3% > 2.9% (50%) → ACTIVA
    if horas >= CONCLUSAO_COND2_TEMPO_MIN_H and ganho_maximo > 0:
        reversao = ganho_maximo - ganho_atual
        limiar   = ganho_maximo * CONCLUSAO_COND2_REVERSAO_FRACCAO
        if reversao >= limiar:
            return _resultado(2, "NEUTRA")

    # ── Condição 4: tempo esgotado (24h) ─────────────────────────────────────
    if horas >= CONCLUSAO_COND4_TEMPO_MIN_H:
        return _resultado(4, "NEUTRA")

    return None     # move ainda activo
