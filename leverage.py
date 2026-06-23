# =============================================================================
# leverage.py — Cálculo de SL, TP, leverage e níveis de preço
# Manual CFI v2.0 — Secção 7
#
# Método C (base): target = 3 × ATR(1h)
# Método A (heatmap): target = cluster identificado pelo Claude
# SL = 1.5 × ATR(1h)
# Leverage = floor(10% / distância_target_%) — sempre para baixo, mínimo 2×
# TP escalonado: TP1 = target/2 (fecha 50%), TP2 = target completo
# TP único: quando TP1 < 1×ATR (split demasiado pequeno)
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from config import (
    SL_ATR_MULTIPLICADOR,
    METODO_C_ATR_MULTIPLICADOR,
    LEVERAGE_NUMERADOR,
    LEVERAGE_MIN_GLOBAL,
    LEVERAGE_MAX_POR_CATEGORIA,
    TP1_FRACCAO_TARGET,
    TP2_FRACCAO_TARGET,
    TP_UNICO_SE_TP1_LT_ATR,
    METODO_A_CLUSTER_MIN_PCT,
    METODO_A_CLUSTER_MAX_PCT,
)


# -----------------------------------------------------------------------------
# Estrutura de resultado
# -----------------------------------------------------------------------------

@dataclass
class ResultadoLeverage:
    """
    Resultado completo do cálculo de leverage para uma potencial entrada.
    Contém todos os níveis necessários para o Momento 1 e Momento 2.

    Todos os _pct são em decimal (ex: 0.024 = 2.4%).
    Todos os _preco são preços absolutos.
    tp2_pct e tp2_preco são None quando tp_tipo == "unico".
    """
    direccao: str               # "LONG" ou "SHORT"
    categoria: str
    preco_entry: float          # preço actual (base de cálculo)
    atr_1h_pct: float           # ATR(14) 1h como % do preço

    # Stop Loss
    sl_pct: float               # distância SL em % (positivo; ex: 0.012 = 1.2%)
    sl_preco: float             # preço do SL

    # Target e método
    target_pct: float           # distância do target em % (positivo)
    target_metodo: str          # "A" ou "C"

    # Take Profits
    tp_tipo: str                # "escalonado" ou "unico"
    tp1_pct: float              # distância TP1 em %
    tp1_preco: float            # preço do TP1
    tp2_pct: Optional[float]    # distância TP2 em % (None se TP único)
    tp2_preco: Optional[float]  # preço do TP2 (None se TP único)

    # Leverage
    leverage: int               # leverage final (após floor e cap)
    leverage_raw: float         # valor antes do floor (ex: 4.17)
    leverage_cap_aplicado: bool # True se o máximo da categoria foi aplicado

    # Métricas
    rr_ratio: float             # R/R = target_pct / sl_pct (ex: 2.0 com Método C)


# -----------------------------------------------------------------------------
# Funções de cálculo individuais
# -----------------------------------------------------------------------------

def calcular_sl_pct(atr_1h_pct: float) -> float:
    """
    SL = 1.5 × ATR(1h) — manual secção 7.3.
    Retorna distância em decimal (ex: 0.012 = 1.2%).
    """
    return SL_ATR_MULTIPLICADOR * atr_1h_pct


def calcular_target_metodo_c(atr_1h_pct: float) -> float:
    """
    Target Método C = 3 × ATR(1h) — manual secção 7.2.
    Retorna distância em decimal (ex: 0.024 = 2.4%).
    """
    return METODO_C_ATR_MULTIPLICADOR * atr_1h_pct


def validar_cluster_metodo_a(cluster_pct: float) -> bool:
    """
    Valida se o cluster do heatmap está dentro dos limites do Método A.
    Cluster válido: entre 1% e 15% do preço (manual 7.2).
    Fora deste range ou heatmap uniforme → usar Método C.
    """
    return METODO_A_CLUSTER_MIN_PCT <= cluster_pct <= METODO_A_CLUSTER_MAX_PCT


def calcular_leverage(
    target_pct: float,
    categoria: str,
) -> tuple[int, float, bool]:
    """
    Leverage = floor(10% / distância_target_%)
    Sempre arredondado para baixo (floor).
    Mínimo global: 2×.
    Máximo por categoria (manual 7.4).

    Retorna: (leverage_final, leverage_raw, cap_aplicado)

    Exemplos do manual:
      ATR 0.8% → target 2.4% → 10%/2.4% = 4.17× → floor = 4×
      ATR 1.5% → target 4.5% → 10%/4.5% = 2.22× → floor = 2×
      cluster 1.8% (Método A) → 10%/1.8% = 5.56× → floor = 5×
    """
    if target_pct <= 0:
        return LEVERAGE_MIN_GLOBAL, 0.0, False

    raw = LEVERAGE_NUMERADOR / target_pct          # ex: 0.10 / 0.024 = 4.167
    floored = math.floor(raw)                       # sempre para baixo: 4
    com_minimo = max(floored, LEVERAGE_MIN_GLOBAL)  # mínimo 2×

    max_cat = LEVERAGE_MAX_POR_CATEGORIA.get(categoria, LEVERAGE_MIN_GLOBAL)
    cap = com_minimo > max_cat
    final = min(com_minimo, max_cat)

    return final, raw, cap


def calcular_tp(
    target_pct: float,
    atr_1h_pct: float,
) -> tuple[str, float, Optional[float]]:
    """
    TP escalonado (manual 7.5):
      TP1 = target / 2 → fecha 50%, move SL para break-even
      TP2 = target completo → fecha restante 50%

    TP único quando TP1 < 1 × ATR(1h):
      O split seria demasiado pequeno — usar target completo como único nível.

    Retorna: (tipo, tp1_pct, tp2_pct_ou_None)
      tipo:    "escalonado" ou "unico"
      tp2_pct: None se tipo == "unico"
    """
    tp1_pct = target_pct * TP1_FRACCAO_TARGET      # target / 2
    limiar   = TP_UNICO_SE_TP1_LT_ATR * atr_1h_pct  # 1 × ATR

    if tp1_pct < limiar:
        # TP único — target completo como único nível de saída
        return "unico", target_pct, None

    tp2_pct = target_pct * TP2_FRACCAO_TARGET       # target completo
    return "escalonado", tp1_pct, tp2_pct


def calcular_precos_absolutos(
    preco_entry: float,
    direccao: str,
    sl_pct: float,
    tp1_pct: float,
    tp2_pct: Optional[float],
) -> tuple[float, float, Optional[float]]:
    """
    Converte distâncias em % para preços absolutos.

    LONG:  SL abaixo do entry; TPs acima.
    SHORT: SL acima do entry; TPs abaixo.

    sl_pct, tp1_pct, tp2_pct são sempre positivos (% de distância do entry).
    Retorna: (sl_preco, tp1_preco, tp2_preco_ou_None)
    """
    if direccao == "LONG":
        sl_preco  = preco_entry * (1.0 - sl_pct)
        tp1_preco = preco_entry * (1.0 + tp1_pct)
        tp2_preco = preco_entry * (1.0 + tp2_pct) if tp2_pct is not None else None
    else:  # SHORT
        sl_preco  = preco_entry * (1.0 + sl_pct)
        tp1_preco = preco_entry * (1.0 - tp1_pct)
        tp2_preco = preco_entry * (1.0 - tp2_pct) if tp2_pct is not None else None

    return sl_preco, tp1_preco, tp2_preco


# -----------------------------------------------------------------------------
# Função principal
# -----------------------------------------------------------------------------

def calcular(
    preco_entry: float,
    atr_1h_pct: float,
    categoria: str,
    direccao: str,
    metodo: str = "C",
    target_pct_override: Optional[float] = None,
) -> ResultadoLeverage:
    """
    Ponto de entrada principal — calcula SL, TPs, leverage e preços absolutos.

    Parâmetros:
      preco_entry:         preço actual do token (base de cálculo)
      atr_1h_pct:          ATR(14) em 1h como decimal (ex: 0.008 = 0.8%)
      categoria:           categoria do token (para leverage máximo)
      direccao:            "LONG" ou "SHORT"
      metodo:              "A" (heatmap) ou "C" (ATR×3)
      target_pct_override: target fornecido pelo Método A em decimal.
                           Se None ou inválido, usa Método C.

    Fluxo:
      1. SL = 1.5 × ATR
      2. Target: Método A se fornecido e válido, senão Método C (3 × ATR)
      3. Leverage = floor(10% / target) → mínimo 2×, máximo por categoria
      4. TP: escalonado (target/2 + target) ou único (se TP1 < 1×ATR)
      5. Preços absolutos de SL e TPs

    Manual secções 7.2, 7.3, 7.4, 7.5.
    """
    if direccao not in ("LONG", "SHORT"):
        raise ValueError(f"Direcção inválida: '{direccao}'. Usar 'LONG' ou 'SHORT'.")

    # ── 1. Stop Loss ──────────────────────────────────────────────────────────
    sl_pct = calcular_sl_pct(atr_1h_pct)

    # ── 2. Target ─────────────────────────────────────────────────────────────
    usar_metodo_a = (
        metodo == "A"
        and target_pct_override is not None
        and validar_cluster_metodo_a(target_pct_override)
    )

    if usar_metodo_a:
        target_pct   = target_pct_override
        target_metodo = "A"
    else:
        target_pct   = calcular_target_metodo_c(atr_1h_pct)
        target_metodo = "C"

    # ── 3. Leverage ───────────────────────────────────────────────────────────
    leverage, leverage_raw, cap = calcular_leverage(target_pct, categoria)

    # ── 4. TP ─────────────────────────────────────────────────────────────────
    tp_tipo, tp1_pct, tp2_pct = calcular_tp(target_pct, atr_1h_pct)

    # ── 5. Preços absolutos ───────────────────────────────────────────────────
    sl_preco, tp1_preco, tp2_preco = calcular_precos_absolutos(
        preco_entry, direccao, sl_pct, tp1_pct, tp2_pct
    )

    rr = target_pct / sl_pct if sl_pct > 0 else 0.0

    return ResultadoLeverage(
        direccao=direccao,
        categoria=categoria,
        preco_entry=preco_entry,
        atr_1h_pct=atr_1h_pct,
        sl_pct=sl_pct,
        sl_preco=sl_preco,
        target_pct=target_pct,
        target_metodo=target_metodo,
        tp_tipo=tp_tipo,
        tp1_pct=tp1_pct,
        tp1_preco=tp1_preco,
        tp2_pct=tp2_pct,
        tp2_preco=tp2_preco,
        leverage=leverage,
        leverage_raw=leverage_raw,
        leverage_cap_aplicado=cap,
        rr_ratio=rr,
    )
