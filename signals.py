# =============================================================================
# signals.py — Cálculo dos 6 sinais de acumulação (S1-S6)
# Manual CFI v2.0 — Secção 3
#
# Lógica determinística pura. Sem I/O, sem chamadas externas.
# Recebe dados pré-fetchados e devolve ResultadoSinais.
#
# Scan pesado (5×/dia): calcula S1-S6 completos (MEXC + Coinglass).
# Scan leve (horário):  recalcula S1, S4, S5 (MEXC); herda S2, S3, S6.
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional


# -----------------------------------------------------------------------------
# Estruturas de dados de entrada
# -----------------------------------------------------------------------------

@dataclass
class DadosMEXC:
    """
    Dados obtidos da MEXC API (futuros USDT-M).
    Disponíveis em scan pesado E scan leve (fonte rápida, sem Playwright).

    candles_1h: lista de dicts em ordem cronológica (índice 0 = mais antigo).
                Mínimo recomendado: 25 candles (EMA21 estável + 4 para HL/LH).
                Cada dict: {'open', 'high', 'low', 'close', 'volume'} — floats.

    atr_1h:     ATR(14) em % do preço actual (ex: 0.008 = 0.8%).
                Calculado pelo fetcher a partir dos candles_1h.
                Não usado em S1-S6 — passado para leverage.py via state.
    """
    ticker: str
    preco_actual: float
    preco_change_24h_pct: float     # ex: +0.025 = +2.5%,  -0.03 = -3.0%
    volume_24h: float               # volume 24h total em USD
    volume_media_7d: float          # média de volume diário dos últimos 7 dias (USD)
    high_24h: float
    low_24h: float
    atr_1h: float                   # ATR(14) 1h em % do preço (ex: 0.008)
    candles_1h: list[dict]          # mínimo 25 candles; ver docstring acima


@dataclass
class DadosCoinglass:
    """
    Dados obtidos do Coinglass via Playwright.
    Apenas disponíveis no scan pesado (custo alto).
    Em scan leve, S2/S3/S6 são herdados do último scan pesado via state.json.
    """
    ticker: str
    oi_change_24h_pct: float        # ex: +0.05 = +5.0%,  -0.10 = -10.0%
    funding_rate: float             # taxa 8h actual (ex: +0.0001 = +0.01%)
    ls_ratio: float                 # ex: 1.5 = 60% longs / 40% shorts


@dataclass
class SinaisHerdados:
    """
    Valores de S2/S3/S6 do último scan pesado, para uso no scan leve.
    Carregados do state.json antes de chamar calcular_sinais_scan_leve().
    S3 é igual para LONG e SHORT — guardado uma vez.
    """
    s2_long: bool
    s2_short: bool
    s3: bool
    s6_long: bool
    s6_short: bool


@dataclass
class ResultadoSinais:
    """
    Resultado dos 6 sinais para UMA direcção (LONG ou SHORT).
    O score é calculado automaticamente no __post_init__.

    Os campos _valor guardam as métricas reais para logging na Base 3 do Notion.
    Em scan leve, os campos herdados (s2_*, s3_*, s6_*) ficam a 0.0 / False —
    os valores reais estão no último scan pesado registado na Base 3.
    """
    direccao: str       # "LONG" ou "SHORT"
    s1: bool
    s2: bool
    s3: bool
    s4: bool
    s5: bool
    s6: bool

    # Valores reais — logging
    s1_ratio_vol: float = 0.0           # volume_24h / media_7d
    s2_oi_pct: float = 0.0
    s2_preco_pct: float = 0.0
    s2_vol_dir_pct: float = 0.0         # % volume na direcção correcta
    s3_funding: float = 0.0
    s4_range_pct: float = 0.0
    s5_ema9: float = 0.0
    s5_ema21: float = 0.0
    s5_estrutura_ok: bool = False       # True se HL (LONG) ou LH (SHORT) confirma
    s6_ls_ratio: float = 0.0
    s6_preco_pct: float = 0.0

    # Calculado automaticamente
    score: int = field(init=False)

    def __post_init__(self) -> None:
        self.score = sum([self.s1, self.s2, self.s3, self.s4, self.s5, self.s6])

    def resumo(self) -> str:
        """Linha de resumo para Telegram/PDF. Ex: 'S1 OK S2 -- S3 OK S4 OK S5 -- S6 OK'"""
        sinais = [self.s1, self.s2, self.s3, self.s4, self.s5, self.s6]
        partes = [
            f"S{i + 1} {'OK' if v else '--'}"
            for i, v in enumerate(sinais)
        ]
        return "  ".join(partes)


# -----------------------------------------------------------------------------
# Funções auxiliares internas
# -----------------------------------------------------------------------------

def _ema(valores: list[float], periodo: int) -> float:
    """
    EMA(periodo) sobre lista de closes em ordem cronológica.
    Seed = SMA dos primeiros `periodo` valores.
    Lança ValueError se candles insuficientes.
    """
    if len(valores) < periodo:
        raise ValueError(
            f"Candles insuficientes para EMA{periodo}: "
            f"necessário {periodo}, disponível {len(valores)}."
        )
    k = 2 / (periodo + 1)
    ema = sum(valores[:periodo]) / periodo      # seed
    for v in valores[periodo:]:
        ema = (v - ema) * k + ema
    return ema


def _volume_direcional_pct(candles: list[dict], direccao: str) -> float:
    """
    % do volume total nos candles da direcção indicada.

    direccao "subida": candles com close > open (bullish).
    direccao "descida": candles com close < open (bearish).
    Candles com close == open não contam para nenhum lado.
    """
    volume_total = sum(c["volume"] for c in candles)
    if volume_total == 0:
        return 0.0
    if direccao == "subida":
        vol_dir = sum(c["volume"] for c in candles if c["close"] > c["open"])
    else:
        vol_dir = sum(c["volume"] for c in candles if c["close"] < c["open"])
    return vol_dir / volume_total


def _estrutura_hl_lh(candles: list[dict], n: int, tipo: str) -> bool:
    """
    Verifica estrutura de Higher Lows (tipo "hl") ou Lower Highs (tipo "lh")
    nos últimos `n` candles fechados (ordem cronológica, 0 = mais antigo).

    "hl": Low[i] > Low[i-1] para todo i em [1..n-1]  — bullish, LONG.
    "lh": High[i] < High[i-1] para todo i em [1..n-1] — bearish, SHORT.

    Retorna False se candles insuficientes.
    """
    if len(candles) < n:
        return False
    recentes = candles[-n:]     # últimos n, cronológico
    if tipo == "hl":
        return all(recentes[i]["low"] > recentes[i - 1]["low"] for i in range(1, n))
    else:
        return all(recentes[i]["high"] < recentes[i - 1]["high"] for i in range(1, n))


# -----------------------------------------------------------------------------
# Cálculo individual de cada sinal
# (funções privadas chamadas pelas funções principais em baixo)
# -----------------------------------------------------------------------------

def _s1_long(mexc: DadosMEXC) -> tuple[bool, float]:
    """S1 LONG: Volume seco — volume_24h < 60% da média 7 dias."""
    from config import S1_LONG_VOLUME_MAX_PCT_MEDIA7D
    if mexc.volume_media_7d == 0:
        return False, 0.0
    ratio = mexc.volume_24h / mexc.volume_media_7d
    return ratio < S1_LONG_VOLUME_MAX_PCT_MEDIA7D, ratio


def _s1_short(mexc: DadosMEXC) -> tuple[bool, float]:
    """S1 SHORT: Volume controlado crescente — entre 80% e 150% da média 7 dias."""
    from config import S1_SHORT_VOLUME_MIN_PCT_MEDIA7D, S1_SHORT_VOLUME_MAX_PCT_MEDIA7D
    if mexc.volume_media_7d == 0:
        return False, 0.0
    ratio = mexc.volume_24h / mexc.volume_media_7d
    return (
        S1_SHORT_VOLUME_MIN_PCT_MEDIA7D <= ratio <= S1_SHORT_VOLUME_MAX_PCT_MEDIA7D,
        ratio,
    )


def _s2(
    mexc: DadosMEXC,
    coinglass: DadosCoinglass,
    direccao: str,
) -> tuple[bool, float, float, float]:
    """
    S2 LONG/SHORT: OI a entrar com pressão direccional.
    Condições comuns: OI change 24h +2%…+15%, preço change 24h -3%…+3%.
    Condição específica: % volume em candles bullish (LONG) ou bearish (SHORT) > 60%.

    Retorna: (passa, oi_change_pct, preco_change_pct, vol_dir_pct)
    """
    from config import (
        S2_OI_CHANGE_MIN_PCT, S2_OI_CHANGE_MAX_PCT,
        S2_PRECO_CHANGE_MIN_PCT, S2_PRECO_CHANGE_MAX_PCT,
        S2_LONG_VOLUME_SUBIDA_MIN_PCT, S2_SHORT_VOLUME_DESCIDA_MIN_PCT,
    )

    oi_ok    = S2_OI_CHANGE_MIN_PCT  <= coinglass.oi_change_24h_pct <= S2_OI_CHANGE_MAX_PCT
    preco_ok = S2_PRECO_CHANGE_MIN_PCT <= mexc.preco_change_24h_pct  <= S2_PRECO_CHANGE_MAX_PCT

    # Últimas 24 candles de 1h para cálculo de volume direccional
    candles_24h = mexc.candles_1h[-24:] if len(mexc.candles_1h) >= 24 else mexc.candles_1h

    if direccao == "LONG":
        vol_dir_pct = _volume_direcional_pct(candles_24h, "subida")
        volume_ok   = vol_dir_pct > S2_LONG_VOLUME_SUBIDA_MIN_PCT
    else:
        vol_dir_pct = _volume_direcional_pct(candles_24h, "descida")
        volume_ok   = vol_dir_pct > S2_SHORT_VOLUME_DESCIDA_MIN_PCT

    return (
        oi_ok and preco_ok and volume_ok,
        coinglass.oi_change_24h_pct,
        mexc.preco_change_24h_pct,
        vol_dir_pct,
    )


def _s3(coinglass: DadosCoinglass) -> tuple[bool, float]:
    """S3: Funding neutro — entre -0.01% e +0.01% (igual LONG e SHORT)."""
    from config import S3_FUNDING_MIN, S3_FUNDING_MAX
    f = coinglass.funding_rate
    return S3_FUNDING_MIN <= f <= S3_FUNDING_MAX, f


def _s4(mexc: DadosMEXC) -> tuple[bool, float]:
    """S4: Range apertado — (High24h - Low24h) / Low24h < 5% (igual LONG e SHORT)."""
    from config import S4_RANGE_MAX_PCT
    if mexc.low_24h == 0:
        return False, 0.0
    range_pct = (mexc.high_24h - mexc.low_24h) / mexc.low_24h
    return range_pct < S4_RANGE_MAX_PCT, range_pct


def _s5(mexc: DadosMEXC, direccao: str) -> tuple[bool, float, float, bool]:
    """
    S5 LONG:  EMA9(1h) > EMA21(1h) E últimos 4 candles com Low crescente (HL).
    S5 SHORT: EMA9(1h) < EMA21(1h) E últimos 4 candles com High decrescente (LH).

    Retorna: (passa, ema9, ema21, estrutura_ok)
    Retorna (False, 0, 0, False) se candles insuficientes para EMA21.
    """
    from config import (
        S5_EMA_RAPIDA, S5_EMA_LENTA,
        S5_LONG_N_CANDLES_HL, S5_SHORT_N_CANDLES_LH,
    )

    closes = [c["close"] for c in mexc.candles_1h]

    if len(closes) < S5_EMA_LENTA:
        return False, 0.0, 0.0, False

    ema9  = _ema(closes, S5_EMA_RAPIDA)
    ema21 = _ema(closes, S5_EMA_LENTA)

    if direccao == "LONG":
        ema_ok    = ema9 > ema21
        estrutura = _estrutura_hl_lh(mexc.candles_1h, S5_LONG_N_CANDLES_HL,  "hl")
    else:
        ema_ok    = ema9 < ema21
        estrutura = _estrutura_hl_lh(mexc.candles_1h, S5_SHORT_N_CANDLES_LH, "lh")

    return ema_ok and estrutura, ema9, ema21, estrutura


def _s6(
    mexc: DadosMEXC,
    coinglass: DadosCoinglass,
    direccao: str,
) -> tuple[bool, float, float]:
    """
    S6 LONG:  L/S ratio > 1.3 E preço change 24h < +3%.
    S6 SHORT: L/S ratio < 0.7 E preço change 24h > -3%.

    Retorna: (passa, ls_ratio, preco_change_pct)
    """
    from config import (
        S6_LONG_LS_RATIO_MIN,  S6_LONG_PRECO_CHANGE_MAX_PCT,
        S6_SHORT_LS_RATIO_MAX, S6_SHORT_PRECO_CHANGE_MIN_PCT,
    )

    ls    = coinglass.ls_ratio
    preco = mexc.preco_change_24h_pct

    if direccao == "LONG":
        passa = (ls > S6_LONG_LS_RATIO_MIN) and (preco < S6_LONG_PRECO_CHANGE_MAX_PCT)
    else:
        passa = (ls < S6_SHORT_LS_RATIO_MAX) and (preco > S6_SHORT_PRECO_CHANGE_MIN_PCT)

    return passa, ls, preco


# -----------------------------------------------------------------------------
# Flag de funding extremo (secção 3.3 — não bloqueante)
# -----------------------------------------------------------------------------

def verificar_funding_flag(
    coinglass: DadosCoinglass,
    categoria: str,
) -> Optional[str]:
    """
    Verifica se o funding está em zona extrema para a categoria do token.
    Devolve "LONG sobreaquecido", "SHORT sobreaquecido" ou None.
    NÃO afecta o score — apenas informativo no alerta Telegram e PDF.
    """
    from config import FUNDING_EXTREMO_LONG, FUNDING_EXTREMO_SHORT

    f = coinglass.funding_rate

    limite_long  = FUNDING_EXTREMO_LONG.get(categoria)
    limite_short = FUNDING_EXTREMO_SHORT.get(categoria)

    if limite_long  is not None and f >  limite_long:
        return "LONG sobreaquecido"
    if limite_short is not None and f <  limite_short:
        return "SHORT sobreaquecido"
    return None


# -----------------------------------------------------------------------------
# Funções principais — chamadas pelo scanner
# -----------------------------------------------------------------------------

def calcular_sinais_scan_pesado(
    mexc: DadosMEXC,
    coinglass: DadosCoinglass,
    direccao: str,
) -> ResultadoSinais:
    """
    Scan pesado: calcula todos os 6 sinais com dados frescos.
    Chamado 5× por dia (06h/10h/13h/18h/22h Lisboa).
    Deve ser chamado duas vezes por token: uma para LONG, outra para SHORT.
    """
    if direccao not in ("LONG", "SHORT"):
        raise ValueError(f"Direcção inválida: '{direccao}'. Usar 'LONG' ou 'SHORT'.")

    s1_ok, s1_ratio                     = (_s1_long(mexc) if direccao == "LONG"
                                           else _s1_short(mexc))
    s2_ok, s2_oi, s2_preco, s2_vol_dir  = _s2(mexc, coinglass, direccao)
    s3_ok, s3_funding                   = _s3(coinglass)
    s4_ok, s4_range                     = _s4(mexc)
    s5_ok, ema9, ema21, s5_estrutura    = _s5(mexc, direccao)
    s6_ok, s6_ls, s6_preco              = _s6(mexc, coinglass, direccao)

    return ResultadoSinais(
        direccao=direccao,
        s1=s1_ok, s2=s2_ok, s3=s3_ok, s4=s4_ok, s5=s5_ok, s6=s6_ok,
        s1_ratio_vol=s1_ratio,
        s2_oi_pct=s2_oi, s2_preco_pct=s2_preco, s2_vol_dir_pct=s2_vol_dir,
        s3_funding=s3_funding,
        s4_range_pct=s4_range,
        s5_ema9=ema9, s5_ema21=ema21, s5_estrutura_ok=s5_estrutura,
        s6_ls_ratio=s6_ls, s6_preco_pct=s6_preco,
    )


def calcular_sinais_scan_leve(
    mexc: DadosMEXC,
    herdados: SinaisHerdados,
    direccao: str,
) -> ResultadoSinais:
    """
    Scan leve: recalcula apenas S1, S4, S5 (MEXC API — sem Playwright).
    S2, S3, S6 são herdados do último scan pesado (SinaisHerdados, lidos do state.json).
    Chamado a cada hora para tokens em Estado 2, 3, 4, 5.

    Score leve = S1(novo) + S2(herdado) + S3(herdado)
               + S4(novo) + S5(novo)    + S6(herdado)

    Manual secção 5 — Recálculo de score pelo scan leve.
    """
    if direccao not in ("LONG", "SHORT"):
        raise ValueError(f"Direcção inválida: '{direccao}'. Usar 'LONG' ou 'SHORT'.")

    s1_ok, s1_ratio                  = (_s1_long(mexc) if direccao == "LONG"
                                        else _s1_short(mexc))
    s4_ok, s4_range                  = _s4(mexc)
    s5_ok, ema9, ema21, s5_estrutura = _s5(mexc, direccao)

    # S2/S3/S6 herdados
    if direccao == "LONG":
        s2_ok = herdados.s2_long
        s6_ok = herdados.s6_long
    else:
        s2_ok = herdados.s2_short
        s6_ok = herdados.s6_short
    s3_ok = herdados.s3

    return ResultadoSinais(
        direccao=direccao,
        s1=s1_ok, s2=s2_ok, s3=s3_ok, s4=s4_ok, s5=s5_ok, s6=s6_ok,
        # Apenas S1/S4/S5 têm valores frescos; S2/S3/S6 ficam a 0.0 (herdados)
        s1_ratio_vol=s1_ratio,
        s4_range_pct=s4_range,
        s5_ema9=ema9, s5_ema21=ema21, s5_estrutura_ok=s5_estrutura,
    )
