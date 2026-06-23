# =============================================================================
# scoring.py — Gestão de estados, scoring de direcção e transições
# Manual CFI v2.0 — Secções 4 e 5
#
# Lógica determinística pura. Sem I/O, sem chamadas externas.
# Recebe ResultadoSinais (de signals.py) + estado actual do token.
# Devolve ResultadoScoring com o novo estado e alertas a despachar.
#
# Regra de ouro — BTC filter (secção 10.1):
#   Scoring e contadores correm sempre para TODOS os tokens.
#   Apenas a progressão formal de estado e o envio de alertas LONG
#   ficam bloqueados enquanto BTC < EMA21.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from config import (
    SCORE_ESTADO1_MAX,
    SCORE_ESTADO2_MIN,
    SCORE_ESTADO2_MAX,
    SCORE_ESTADO3,
    SCORE_DIRECAO_DELTA_MIN,
    ESTADO2_SCANS_CONSECUTIVOS_MIN,
)
from signals import ResultadoSinais, SinaisHerdados


# -----------------------------------------------------------------------------
# Constantes de estado (manual secção 5)
# -----------------------------------------------------------------------------
ESTADO_PASSIVA     = 1   # Observação Passiva
ESTADO_RADAR       = 2   # Radar Activo
ESTADO_PRIORITARIO = 3   # Alerta Prioritário
ESTADO_BREAKOUT    = 4   # Breakout confirmado
ESTADO_CONCLUIDO   = 5   # Move concluído


# -----------------------------------------------------------------------------
# Tipos de alerta (despachados por notificacoes.py)
# -----------------------------------------------------------------------------
class Alerta:
    MOMENTO_0  = "MOMENTO_0"    # entrada em Estado 2
    MOMENTO_1  = "MOMENTO_1"    # entrada em Estado 3
    MOMENTO_3A = "MOMENTO_3A"   # saída de Estado 3 sem breakout (score ≤3)
    DEGRADACAO = "DEGRADACAO"   # Estado 3 → Estado 2 (score 4-5)


# -----------------------------------------------------------------------------
# Estado do token — espelha o JSON (manual secção 8.2)
# -----------------------------------------------------------------------------
@dataclass
class EstadoToken:
    """
    Estado completo de um token, persistido no state.json do GitHub.
    Campos extra vs manual: sinais_herdados (suporte ao scan leve).
    Dois contadores distintos (manual secção 8.2):
      scans_consecutivos  — permanência genérica no estado actual.
      contador_estado2    — específico da promoção Estado 1→2.
                            Reinicia a 0 quando score < 4 ou após degradação.
    """
    ticker: str
    estado: int                        # 1–5
    direccao: str                      # "LONG" | "SHORT" | "INDEFINIDO" | ""
    score_actual: int
    score_anterior: int
    scans_consecutivos: int
    contador_estado2: int
    timestamp_entrada_estado: str      # ISO UTC
    ultimo_scan: str                   # ISO UTC
    salto_directo: bool
    grace_period: bool
    grace_period_dias_restantes: Optional[int]
    trigger_pendente: bool
    trigger_timestamp: Optional[str]
    trigger_volume: Optional[float]
    trigger_OI: Optional[float]
    trigger_preco: Optional[float]
    btc_volatil_no_trigger: bool
    # Níveis calculados no Momento 1 (para comparação no Momento 2 — secção 7.2)
    momento1_target_pct: Optional[float]
    momento1_sl_preco: Optional[float]
    momento1_tp1_preco: Optional[float]
    momento1_tp2_preco: Optional[float]
    momento1_leverage: Optional[int]
    momento1_metodo: Optional[str]     # "A" ou "C"
    # Sinais herdados para scan leve (S2/S3/S6 do último scan pesado)
    sinais_herdados: dict = field(default_factory=lambda: {
        "s2_long": False, "s2_short": False,
        "s3": False,
        "s6_long": False, "s6_short": False,
    })

    # ------------------------------------------------------------------
    @classmethod
    def novo(cls, ticker: str, agora_utc: str) -> EstadoToken:
        """Estado inicial para token a entrar no universo (Estado 1)."""
        return cls(
            ticker=ticker,
            estado=ESTADO_PASSIVA,
            direccao="",
            score_actual=0,
            score_anterior=0,
            scans_consecutivos=0,
            contador_estado2=0,
            timestamp_entrada_estado=agora_utc,
            ultimo_scan=agora_utc,
            salto_directo=False,
            grace_period=False,
            grace_period_dias_restantes=None,
            trigger_pendente=False,
            trigger_timestamp=None,
            trigger_volume=None,
            trigger_OI=None,
            trigger_preco=None,
            btc_volatil_no_trigger=False,
            momento1_target_pct=None,
            momento1_sl_preco=None,
            momento1_tp1_preco=None,
            momento1_tp2_preco=None,
            momento1_leverage=None,
            momento1_metodo=None,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> EstadoToken:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def get_sinais_herdados(self) -> SinaisHerdados:
        """Extrai SinaisHerdados para passar ao scan leve."""
        h = self.sinais_herdados
        return SinaisHerdados(
            s2_long=h.get("s2_long", False),
            s2_short=h.get("s2_short", False),
            s3=h.get("s3", False),
            s6_long=h.get("s6_long", False),
            s6_short=h.get("s6_short", False),
        )

    def _limpar_momento1(self) -> None:
        """Limpa campos do Momento 1 (após 3A ou conclusão de move)."""
        self.momento1_target_pct = None
        self.momento1_sl_preco   = None
        self.momento1_tp1_preco  = None
        self.momento1_tp2_preco  = None
        self.momento1_leverage   = None
        self.momento1_metodo     = None


# -----------------------------------------------------------------------------
# Resultado do scoring
# -----------------------------------------------------------------------------
@dataclass
class ResultadoScoring:
    """
    Saída de processar_scan_pesado() / processar_scan_leve().
    Contém o novo estado para gravar no JSON e os alertas a despachar.
    """
    estado_anterior: int
    estado_novo: int
    score_long: int
    score_short: int
    direccao: str
    alertas: list[str]             # lista de Alerta.*; pode ser vazia
    salto_directo: bool
    bloqueado_filtro_btc: bool     # setup LONG existiu mas BTC < EMA21 impediu
    degradacao: bool               # Estado 3 → Estado 2
    novo_estado_token: EstadoToken


# -----------------------------------------------------------------------------
# Funções auxiliares
# -----------------------------------------------------------------------------

def calcular_direccao(score_long: int, score_short: int) -> str:
    """
    Regra de direcção — manual secção 4.2.
    LONG  se score_long  >= score_short + 2.
    SHORT se score_short >= score_long  + 2.
    INDEFINIDO se diferença < 2 (incluindo 6 LONG + 6 SHORT).
    """
    if score_long  >= score_short + SCORE_DIRECAO_DELTA_MIN:
        return "LONG"
    if score_short >= score_long  + SCORE_DIRECAO_DELTA_MIN:
        return "SHORT"
    return "INDEFINIDO"


def _agora_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reset_estado1(token: EstadoToken, agora: str) -> EstadoToken:
    """
    Repõe o token em Estado 1. Preserva grace_period e sinais_herdados.
    Limpa todos os campos de trigger, Momento 1 e contadores.
    """
    token.estado               = ESTADO_PASSIVA
    token.direccao             = ""
    token.score_actual         = 0
    token.score_anterior       = 0
    token.scans_consecutivos   = 0
    token.contador_estado2     = 0
    token.timestamp_entrada_estado = agora
    token.salto_directo        = False
    token.trigger_pendente     = False
    token.trigger_timestamp    = None
    token.trigger_volume       = None
    token.trigger_OI           = None
    token.trigger_preco        = None
    token.btc_volatil_no_trigger = False
    token._limpar_momento1()
    return token


def _guardar_sinais_herdados(
    token: EstadoToken,
    sl: ResultadoSinais,
    ss: ResultadoSinais,
) -> None:
    """Grava S2/S3/S6 do scan pesado no token para uso posterior no scan leve."""
    token.sinais_herdados = {
        "s2_long":  sl.s2,
        "s2_short": ss.s2,
        "s3":       sl.s3,   # S3 idêntico LONG e SHORT
        "s6_long":  sl.s6,
        "s6_short": ss.s6,
    }


# -----------------------------------------------------------------------------
# SCAN PESADO — processa todos os tokens 5× por dia
# -----------------------------------------------------------------------------

def processar_scan_pesado(
    token: EstadoToken,
    sinais_long: ResultadoSinais,
    sinais_short: ResultadoSinais,
    btc_acima_ema21: bool,
    agora_utc: str,
) -> ResultadoScoring:
    """
    Aplica a lógica completa de scoring ao resultado do scan pesado.

    Chamado para cada token 5× por dia (06h/10h/13h/18h/22h Lisboa).
    Deve receber sinais calculados para AMBAS as direcções.

    Transições tratadas:
    • Estado 1 → Estado 2 (2 scans consecutivos com score 4-5)
    • Estado 1 → Estado 3 (salto directo, score 6/6 de imediato)
    • Estado 2 → Estado 3 (score atinge 6/6)
    • Estado 3 → Estado 2 (degradação: score desce para 4-5)
    • Estado 3 → Estado 1 (Momento 3A: score desce para ≤3)
    • Filtro BTC: bloqueia progressão e alertas LONG quando BTC < EMA21
    """
    estado_anterior  = token.estado
    alertas: list[str] = []
    salto_directo    = False
    bloqueado_btc    = False
    degradacao       = False

    score_long  = sinais_long.score
    score_short = sinais_short.score
    direccao    = calcular_direccao(score_long, score_short)

    # Score activo (da direcção dominante; 0 se INDEFINIDO)
    score = (
        score_long  if direccao == "LONG"  else
        score_short if direccao == "SHORT" else 0
    )

    # Guardar sinais herdados para scan leve (sempre, independente de estado)
    _guardar_sinais_herdados(token, sinais_long, sinais_short)
    token.ultimo_scan    = agora_utc
    token.score_anterior = token.score_actual
    token.score_actual   = score

    # ── CASO 1: sem direcção ou score ≤3 — reset para Estado 1 ──────────────
    if direccao == "INDEFINIDO" or score <= SCORE_ESTADO1_MAX:

        if estado_anterior == ESTADO_PRIORITARIO:
            # Estava em Estado 3 → Momento 3A (manual 6.3)
            alertas.append(Alerta.MOMENTO_3A)

        token = _reset_estado1(token, agora_utc)
        # scans_consecutivos = 1 (este scan conta)
        token.scans_consecutivos = 1

    # ── CASO 2: score 4-5 ───────────────────────────────────────────────────
    elif SCORE_ESTADO2_MIN <= score <= SCORE_ESTADO2_MAX:

        token.scans_consecutivos += 1

        if estado_anterior == ESTADO_PRIORITARIO:
            # ── 2a: degradação Estado 3 → Estado 2 (manual secção 5) ────────
            degradacao = True
            alertas.append(Alerta.DEGRADACAO)
            token.estado               = ESTADO_RADAR
            token.direccao             = direccao
            token.contador_estado2     = 1      # entrada "fresca" (manual 5)
            token.scans_consecutivos   = 1
            token.timestamp_entrada_estado = agora_utc
            token.salto_directo        = False
            token._limpar_momento1()

        elif estado_anterior == ESTADO_RADAR:
            # ── 2b: já em Estado 2 — manter; contador não interfere ──────────
            # BTC filter: tokens LONG já em Estado 2 mantêm estado (manual 10.1)
            if direccao == "LONG" and not btc_acima_ema21:
                bloqueado_btc = True
            token.direccao = direccao
            # (não enviamos Momento 0 novamente)

        else:
            # ── 2c: promoção Estado 1 → Estado 2 ────────────────────────────
            if direccao == "LONG" and not btc_acima_ema21:
                # Filtro BTC: scoring corre, contador incrementa, estado NÃO avança
                bloqueado_btc = True
                token.contador_estado2 += 1
                token.direccao = "LONG"
                # token.estado permanece ESTADO_PASSIVA
            else:
                token.contador_estado2 += 1
                token.direccao = direccao
                if token.contador_estado2 >= ESTADO2_SCANS_CONSECUTIVOS_MIN:
                    token.estado = ESTADO_RADAR
                    token.timestamp_entrada_estado = agora_utc
                    token.scans_consecutivos = 1
                    alertas.append(Alerta.MOMENTO_0)

    # ── CASO 3: score 6/6 ───────────────────────────────────────────────────
    elif score == SCORE_ESTADO3:

        token.scans_consecutivos += 1

        if estado_anterior == ESTADO_PRIORITARIO:
            # ── 3a: já em Estado 3 — manter (score confirmado novamente) ─────
            if direccao == "LONG" and not btc_acima_ema21:
                bloqueado_btc = True
            token.direccao = direccao

        else:
            # ── 3b: promoção para Estado 3 ───────────────────────────────────
            if direccao == "LONG" and not btc_acima_ema21:
                # Filtro BTC bloqueia progressão e Momento 1
                bloqueado_btc = True
                token.direccao = "LONG"
                # contador_estado2 só é relevante para threshold 4-5;
                # não incrementamos aqui (score 6 seria Estado 3, não 2)
                # token.estado permanece inalterado (PASSIVA ou RADAR)
            else:
                # Detectar salto directo: vinha de Estado 1 (nunca em Estado 2)
                if estado_anterior == ESTADO_PASSIVA:
                    salto_directo = True
                    token.salto_directo = True

                token.estado               = ESTADO_PRIORITARIO
                token.direccao             = direccao
                token.contador_estado2     = 0
                token.timestamp_entrada_estado = agora_utc
                token.scans_consecutivos   = 1
                alertas.append(Alerta.MOMENTO_1)

    return ResultadoScoring(
        estado_anterior=estado_anterior,
        estado_novo=token.estado,
        score_long=score_long,
        score_short=score_short,
        direccao=direccao,
        alertas=alertas,
        salto_directo=salto_directo,
        bloqueado_filtro_btc=bloqueado_btc,
        degradacao=degradacao,
        novo_estado_token=token,
    )


# -----------------------------------------------------------------------------
# SCAN LEVE — tokens em Estado 2, 3, 4, 5 (horário)
# -----------------------------------------------------------------------------

def processar_scan_leve(
    token: EstadoToken,
    sinais: ResultadoSinais,
    btc_acima_ema21: bool,
    agora_utc: str,
) -> ResultadoScoring:
    """
    Processa o scan leve (horário) para um token activo.

    Transições possíveis por estado:
    • Estado 2: nenhuma (só heavy scan promove Estado 2 → 3).
                Score actualizado para display no update horário.
    • Estado 3: score ≤3 → Momento 3A → Estado 1.
                score 4-5 → degradação → Estado 2.
                score 6 → mantém Estado 3 (sem alerta adicional).
    • Estado 4: conclusão verificada em triggers.py (não aqui).
    • Estado 5: reset e Momento 3B despachados pelo orchestrador.

    Usa sinais com S1/S4/S5 recalculados e S2/S3/S6 herdados (manual secção 5).
    """
    estado_anterior = token.estado
    alertas: list[str] = []
    bloqueado_btc = False
    degradacao    = False

    score    = sinais.score
    direccao = token.direccao          # mantém a direcção registada no estado actual

    token.ultimo_scan    = agora_utc
    token.score_anterior = token.score_actual
    token.score_actual   = score
    token.scans_consecutivos += 1

    # ── Estado 3 — único com transições no scan leve (manual secção 5) ──────
    if estado_anterior == ESTADO_PRIORITARIO:

        if score <= SCORE_ESTADO1_MAX:
            # Queda drástica → Momento 3A → Estado 1
            alertas.append(Alerta.MOMENTO_3A)
            token = _reset_estado1(token, agora_utc)
            token.scans_consecutivos = 1

        elif SCORE_ESTADO2_MIN <= score <= SCORE_ESTADO2_MAX:
            # Degradação parcial → Estado 2 (manual secção 5)
            degradacao = True
            alertas.append(Alerta.DEGRADACAO)
            token.estado               = ESTADO_RADAR
            token.contador_estado2     = 1     # entrada "fresca" (manual 5)
            token.scans_consecutivos   = 1
            token.timestamp_entrada_estado = agora_utc
            token.salto_directo        = False
            token._limpar_momento1()

        # score == 6: Estado 3 mantém-se, sem alerta
        # (o scan de breakout corre em paralelo — triggers.py)

    # ── Estados 2, 4, 5: sem transições de estado no scan leve ─────────────
    # Estado 2: score registado para update horário; heavy scan decide promoção
    # Estado 4: triggers.py verifica condições de conclusão
    # Estado 5: orchestrador emite Momento 3B e repõe Estado 1

    # Filtro BTC em scan leve: aplicável apenas se score subiu e direccao é LONG
    # (neste scan não há novas promoções, mas registamos o bloqueio se relevante)
    if direccao == "LONG" and not btc_acima_ema21:
        bloqueado_btc = True

    return ResultadoScoring(
        estado_anterior=estado_anterior,
        estado_novo=token.estado,
        score_long=score if direccao == "LONG"  else 0,
        score_short=score if direccao == "SHORT" else 0,
        direccao=direccao,
        alertas=alertas,
        salto_directo=False,
        bloqueado_filtro_btc=bloqueado_btc,
        degradacao=degradacao,
        novo_estado_token=token,
    )
