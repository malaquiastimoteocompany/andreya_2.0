# =============================================================================
# notificacoes.py — Formatação e envio de mensagens Telegram
# Manual CFI v2.0 — Secção 9.1 e 9.2
#
# Formatos de mensagem: HTML (não MarkdownV2)
# Tags usadas: <b>, <i>, <code>
#
# Momentos enviados:
#   Momento 0  — entrada em Estado 2 (Radar)
#   Momento 1  — entrada em Estado 3 (Prioridade)
#   Momento 2  — breakout confirmado
#   Momento 3A — setup encerrado sem breakout
#   Momento 3B — move concluído
#   DEGRADAÇÃO — Estado 3 → Estado 2
#   SAIU_UNIVERSO — token saiu por falta de liquidez
#   UPDATE_HORÁRIO — scan leve (só se há tokens activos)
# =============================================================================

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TZ_LISBOA,
    SIZING_VINHA_ESTADO2,
    SIZING_VINHA_ESTADO3,
    SIZING_SALTO_DIRECTO,
)

log = logging.getLogger(__name__)

_TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


# =============================================================================
# BASE — envio HTTP
# =============================================================================

def _enviar(texto: str, parse_mode: str = "HTML") -> bool:
    """
    Envia uma mensagem de texto ao chat Telegram configurado.
    Retorna True se bem-sucedido, False caso contrário.
    """
    payload = json.dumps({
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       texto,
        "parse_mode": parse_mode,
    }).encode()

    req = urllib.request.Request(
        _TELEGRAM_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if not resp.get("ok"):
                log.error(f"Telegram API erro: {resp}")
                return False
            return True
    except Exception as e:
        log.error(f"Falha ao enviar mensagem Telegram: {e}")
        return False


# =============================================================================
# FORMATADORES AUXILIARES
# =============================================================================

def _agora_lisboa() -> str:
    """Hora actual em Lisboa — formato HH:MM."""
    return datetime.now(TZ_LISBOA).strftime("%H:%M")


def _linha_sinais(sinais) -> str:
    """
    Formata a linha de sinais S1-S6 para o alerta.
    Ex: 'S1 OK  S2 OK  S3 OK  S4 OK  S5 --  S6 --'
    """
    if sinais is None:
        return "S1 --  S2 --  S3 --  S4 --  S5 --  S6 --"
    flags = [sinais.s1, sinais.s2, sinais.s3, sinais.s4, sinais.s5, sinais.s6]
    return "  ".join(f"S{i+1} {'OK' if v else '--'}" for i, v in enumerate(flags))


def _formatar_preco(preco: float) -> str:
    """Formata preço com casas decimais adequadas ao magnitude."""
    if preco == 0:
        return "$0"
    if preco >= 1000:
        return f"${preco:,.2f}"
    if preco >= 1:
        return f"${preco:.4f}"
    if preco >= 0.001:
        return f"${preco:.6f}"
    return f"${preco:.8f}"


def _formatar_pct(valor: float, sinal: bool = True) -> str:
    """Formata percentagem com sinal. Ex: +2.40%"""
    s = "+" if sinal and valor >= 0 else ""
    return f"{s}{valor*100:.2f}%"


def _linha_leverage(lev_r) -> str:
    """Formata linha de leverage e R/R."""
    if lev_r is None:
        return "Leverage: — / R/R: —"
    return f"Leverage sugerida: <b>{lev_r.leverage}x</b> / R/R: <b>{lev_r.rr_ratio:.1f}:1</b>"


def _linhas_niveis(lev_r) -> str:
    """Formata bloco de níveis de entrada, SL e TPs."""
    if lev_r is None:
        return ""
    tp2_str = (f"\nTP2: <code>{_formatar_preco(lev_r.tp2_preco)}</code> "
               f"(<b>{_formatar_pct(lev_r.tp2_pct)}</b>)"
               if lev_r.tp_tipo == "escalonado" and lev_r.tp2_preco else "")
    return (
        f"Entry estimado: <code>{_formatar_preco(lev_r.preco_entry)}</code> / "
        f"SL: <code>{_formatar_preco(lev_r.sl_preco)}</code> "
        f"(<b>-{_formatar_pct(lev_r.sl_pct, sinal=False)}</b>)\n"
        f"TP1: <code>{_formatar_preco(lev_r.tp1_preco)}</code> "
        f"(<b>{_formatar_pct(lev_r.tp1_pct)}</b>)"
        f"{tp2_str}"
    )


def _notas_prioridade(notas: list[str]) -> str:
    """
    Formata avisos de prioridade no Momento 2.
    Ordem: BTC volátil > Target actualizado > Grace period (manual 9.1).
    """
    mapa = {
        "BTC_VOLATIL_NO_TRIGGER":
            "⚠️ <b>BTC VOLÁTIL NO TRIGGER</b> — alerta atrasado por volatilidade",
        "TARGET_ACTUALIZADO":
            "ℹ️ Target actualizado face ao Momento 1 — usar este",
        "GRACE_PERIOD_ACTIVO":
            "⚠️ Token em grace period — liquidez abaixo do mínimo",
    }
    ordem = ["BTC_VOLATIL_NO_TRIGGER", "TARGET_ACTUALIZADO", "GRACE_PERIOD_ACTIVO"]
    linhas = [mapa[n] for n in ordem if n in notas and n in mapa]
    return "\n".join(linhas) + "\n" if linhas else ""


# =============================================================================
# FORMATADORES DE CADA MOMENTO
# =============================================================================

def _momento_0(symbol: str, token, resultado_scoring, funding_flag: Optional[str]) -> str:
    """
    Momento 0 — entrada em Estado 2 (Radar Activo).
    Manual secção 9.1.
    """
    direccao = token.direccao if token else "—"
    score    = token.score_actual if token else 0
    sinais   = None  # scan pesado tem LONG e SHORT — usa o dominante
    linha_s  = _linha_sinais(sinais)
    flag_str = f"\n⚠️ FLAG: {funding_flag}" if funding_flag else ""

    return (
        f"🟡 <b>RADAR ACTIVO — {symbol} {direccao} — Score {score}/6</b>{flag_str}\n"
        f"<code>{linha_s}</code>\n\n"
        f"Acumulação confirmada em 2 scans pesados consecutivos\n"
        f"A monitorizar — scan leve activo"
    )


def _momento_1(
    symbol: str,
    token,
    resultado_scoring,
    lev_r,
    funding_flag: Optional[str],
    sizing: float,
) -> str:
    """
    Momento 1 — entrada em Estado 3 (Alerta Prioritário).
    Manual secção 9.1.
    """
    direccao    = token.direccao if token else "—"
    score       = token.score_actual if token else 0
    salto_str   = "\n⚡ <b>SALTO DIRECTO</b> — sem histórico em Estado 2" if (
                    resultado_scoring and resultado_scoring.salto_directo) else ""
    flag_str    = f"\n⚠️ FUNDING {funding_flag}" if funding_flag else ""
    niveis_str  = "\n\n<b>NÍVEIS PRÉ-CALCULADOS:</b>\n" + _linhas_niveis(lev_r) if lev_r else ""
    lev_str     = "\n" + _linha_leverage(lev_r) if lev_r else ""
    metodo_str  = f"\nTarget: Método {lev_r.target_metodo} — " + (
                    "cluster heatmap" if lev_r and lev_r.target_metodo == "A"
                    else "3×ATR(1h)") if lev_r else ""
    sizing_str  = f"\nSizing sugerido: <b>{sizing*100:.0f}% da banca</b>"

    return (
        f"🔴 <b>ACUMULAÇÃO COMPLETA — {symbol} {direccao} — {score}/6</b>"
        f"{salto_str}{flag_str}"
        f"{niveis_str}"
        f"{lev_str}{metodo_str}"
        f"{sizing_str}\n\n"
        f"<i>NÃO É SINAL DE ENTRADA</i>\n"
        f"Scan de breakout activo — 1ª verificação em 30min"
    )


def _momento_2(
    symbol: str,
    token,
    lev_r,
    sizing: float,
    notas: list[str],
    janela_horas: int,
    nivel_pre_breakout: float,
) -> str:
    """
    Momento 2 — breakout confirmado.
    Manual secção 9.1.
    """
    direccao  = token.direccao if token else "—"
    notas_str = _notas_prioridade(notas)
    niveis_str = _linhas_niveis(lev_r)
    lev_str    = _linha_leverage(lev_r)
    metodo_str = ("Target: Método A — cluster heatmap"
                  if lev_r and lev_r.target_metodo == "A"
                  else "Target: Método C — 3×ATR(1h)")
    sizing_str = f"Sizing sugerido: <b>{sizing*100:.0f}%</b>"
    nivel_str  = (f"\nNível pré-breakout: <code>{_formatar_preco(nivel_pre_breakout)}</code>"
                  if nivel_pre_breakout else "")

    return (
        f"🚨 <b>BREAKOUT CONFIRMADO — {symbol} {direccao}</b>\n"
        f"<b>ENTRA AGORA OU NÃO ENTRAS</b>\n\n"
        f"{notas_str}"
        f"JANELA: <b>{janela_horas}h</b> após este alerta\n"
        f"(Se passaram mais de {janela_horas}h — verificar se preço ainda "
        f"{'>' if direccao == 'LONG' else '<'} High/Low 24h do trigger)\n"
        f"{nivel_str}\n\n"
        f"Entry: mercado\n"
        f"{niveis_str}\n"
        f"{lev_str}\n"
        f"{metodo_str}\n"
        f"{sizing_str}"
    )


def _momento_3a(symbol: str, token) -> str:
    """
    Momento 3A — setup encerrado sem breakout (score desceu a ≤3).
    Manual secção 9.1.
    """
    direccao = token.direccao if token else "—"
    score    = token.score_actual if token else 0
    return (
        f"⚫ <b>SETUP ENCERRADO — {symbol} {direccao}</b>\n"
        f"Score desceu para {score}/6 — acumulação perdida\n"
        f"Aguardar novo ciclo de acumulação"
    )


def _momento_3b(symbol: str, token, conclusao) -> str:
    """
    Momento 3B — move concluído (Estado 4 → Estado 5).
    Manual secção 9.1.
    """
    if conclusao is None:
        return f"✅ <b>CONCLUÍDO — {symbol}</b>\nMove registado."

    direccao = token.direccao if token else "—"
    emoji    = {"POSITIVA": "✅", "NEUTRA": "🔶", "NEGATIVA": "❌"}.get(conclusao.tipo, "⚪")
    ganho_max_h = max((h for h in [1, 2, 4, 8, 24]
                       if hasattr(conclusao, f"ganho_{h}h")), default=0)

    return (
        f"{emoji} <b>CONCLUÍDO — {symbol} {direccao}</b>\n\n"
        f"Move total: <b>{_formatar_pct(conclusao.ganho_atual_pct)}</b> / "
        f"Move máximo: <b>{_formatar_pct(conclusao.ganho_maximo_pct)}</b>\n"
        f"Duração: <b>{conclusao.horas_decorridas:.1f}h</b>\n"
        f"Tipo: <b>{conclusao.tipo}</b> — Condição {conclusao.condicao}"
    )


def _degradacao(symbol: str, token) -> str:
    """
    Alerta de degradação Estado 3 → Estado 2.
    Não é um Momento numerado — é um alerta informativo.
    """
    direccao = token.direccao if token else "—"
    score    = token.score_actual if token else 0
    return (
        f"🔽 <b>DEGRADAÇÃO — {symbol} {direccao}</b>\n"
        f"Score desceu para {score}/6 (era 6/6)\n"
        f"Estado 3 → Estado 2 — scan de breakout desactivado\n"
        f"Monitorização continua em radar"
    )


def _saiu_universo(symbol: str, motivo: str = "") -> str:
    """Alerta de saída do universo por grace period expirado."""
    m = f" — {motivo}" if motivo else ""
    return (
        f"🚫 <b>{symbol} saiu do universo</b>{m}\n"
        f"Grace period expirado — sem novos alertas do sistema"
    )


# =============================================================================
# UPDATE HORÁRIO — scan leve (secção 9.2)
# =============================================================================

def _formatar_update_horario(
    agora_utc: str,
    estado_json: dict,
    btc_volatil: bool,
) -> Optional[str]:
    """
    Formata o update horário do scan leve.
    Enviado apenas se há tokens activos (Estado 2-5).
    Manual secção 9.2.
    """
    hora_lisboa = datetime.now(TZ_LISBOA).strftime("%H:%M")

    estado4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]
    estado3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    estado2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]
    estado5 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 5]

    if not any([estado4, estado3, estado2, estado5]):
        return None  # Nada activo — não enviar

    linhas = [f"<b>[{hora_lisboa} Lisboa]</b>"]

    if estado4:
        linhas.append("\n🚨 <b>MOVES ACTIVOS (Estado 4)</b>")
        for sym, c in estado4:
            dir_ = c.get("direccao", "—")
            linhas.append(f"  • {sym} {dir_}")

    if estado3:
        linhas.append("\n🔴 <b>ESTADO 3 — acumulação completa</b>")
        for sym, c in estado3:
            dir_   = c.get("direccao", "—")
            score  = c.get("score_actual", 0)
            linhas.append(f"  • {sym} {dir_} ({score}/6)")

    if estado2:
        linhas.append("\n🟡 <b>RADAR — variações (Estado 2)</b>")
        for sym, c in estado2:
            dir_   = c.get("direccao", "—")
            score  = c.get("score_actual", 0)
            s_ant  = c.get("score_anterior", score)
            delta  = score - s_ant
            seta   = " ↑" if delta > 0 else " ↓" if delta < 0 else ""
            linhas.append(f"  • {sym} {dir_} {score}/6{seta}")

    if estado5:
        linhas.append("\n✅ <b>CONCLUÍDOS ESTA HORA</b>")
        for sym, c in estado5:
            linhas.append(f"  • {sym}")

    if btc_volatil:
        linhas.append("\n⚠️ <b>BTC VOLÁTIL — alertas de breakout pausados</b>")

    return "\n".join(linhas)


# =============================================================================
# FUNÇÕES PÚBLICAS — chamadas pelo scanner.py
# =============================================================================

def enviar_momento(
    tipo: str,
    symbol: str,
    token,
    resultado_scoring,
    resultado_leverage=None,
    funding_flag: Optional[str] = None,
    sizing: float = 0.20,
    notas_prioridade: Optional[list[str]] = None,
    janela_horas: int = 2,
    nivel_pre_breakout: float = 0.0,
    conclusao=None,
    **kwargs,
) -> bool:
    """
    Dispatcher principal — formata e envia o alerta correcto.

    tipo: "MOMENTO_0" | "MOMENTO_1" | "MOMENTO_2" | "MOMENTO_3A"
          | "MOMENTO_3B" | "MOMENTO_3B_PREP" | "DEGRADACAO"
          | "SAIU_UNIVERSO"
    """
    notas = notas_prioridade or []

    if tipo == "MOMENTO_0":
        texto = _momento_0(symbol, token, resultado_scoring, funding_flag)

    elif tipo == "MOMENTO_1":
        texto = _momento_1(symbol, token, resultado_scoring,
                           resultado_leverage, funding_flag, sizing)

    elif tipo == "MOMENTO_2":
        texto = _momento_2(symbol, token, resultado_leverage,
                           sizing, notas, janela_horas, nivel_pre_breakout)

    elif tipo == "MOMENTO_3A":
        texto = _momento_3a(symbol, token)

    elif tipo in ("MOMENTO_3B", "MOMENTO_3B_PREP"):
        texto = _momento_3b(symbol, token, conclusao)

    elif tipo == "DEGRADACAO":
        texto = _degradacao(symbol, token)

    elif tipo == "SAIU_UNIVERSO":
        texto = _saiu_universo(symbol)

    else:
        log.warning(f"Tipo de momento desconhecido: {tipo!r}")
        return False

    log.info(f"[{symbol}] Telegram → {tipo}")
    return _enviar(texto)


def enviar_update_horario(
    agora_utc: str,
    estado_json: dict,
    btc_volatil: bool,
) -> bool:
    """
    Envia update horário do scan leve.
    Silencioso se não há tokens activos.
    Manual secção 9.2.
    """
    texto = _formatar_update_horario(agora_utc, estado_json, btc_volatil)
    if texto is None:
        log.debug("Update horário: sem tokens activos — não enviado")
        return True
    log.info("Telegram → UPDATE_HORÁRIO")
    return _enviar(texto)


def enviar_alerta_degradacao(
    symbol: str,
    token,
    resultado_scoring,
) -> bool:
    """Atalho para alerta de degradação (Estado 3 → Estado 2)."""
    return enviar_momento("DEGRADACAO", symbol, token, resultado_scoring)
