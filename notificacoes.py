# =============================================================================
# notificacoes.py — Formatação e envio de mensagens Telegram
# Manual CFI v2.0 — Secção 9.1 e 9.2
#
# Formatos de mensagem: HTML (não MarkdownV2)
# Tags usadas: <b>, <i>, <code>
#
# Momentos enviados:
#   Momento 0  — entrada em Estado 2 (Radar) — agora em resumo consolidado
#   Momento 1  — entrada em Estado 3 (Prioridade)
#   Momento 2  — breakout confirmado
#   Momento 3A — setup encerrado sem breakout
#   Momento 3B — move concluído
#   DEGRADAÇÃO — Estado 3 → Estado 2
#   SAIU_UNIVERSO — token saiu por falta de liquidez
#   UPDATE_HORÁRIO — scan leve (só se há tokens activos)
#   RESUMO_SCAN_PESADO — lista consolidada de novos E2 + totais
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
    return datetime.now(TZ_LISBOA).strftime("%H:%M")


def _linha_sinais(sinais) -> str:
    """
    Formata linha S1-S6. Garante sempre os 6 sinais.
    Ex: 'S1 OK  S2 --  S3 OK  S4 OK  S5 --  S6 OK'
    """
    if sinais is None:
        return "S1 --  S2 --  S3 --  S4 --  S5 --  S6 --"
    flags = [sinais.s1, sinais.s2, sinais.s3, sinais.s4, sinais.s5, sinais.s6]
    return "  ".join(f"S{i+1} {'OK' if v else '--'}" for i, v in enumerate(flags))


def _linha_sinais_compacta(sinais) -> str:
    """
    Versão compacta para listas: 'S1 OK  S2 --  S3 OK  S4 OK  S5 --  S6 OK'
    Igual ao normal mas sem espaço duplo — para caber em linha de lista.
    """
    if sinais is None:
        return "S1-- S2-- S3-- S4-- S5-- S6--"
    flags = [sinais.s1, sinais.s2, sinais.s3, sinais.s4, sinais.s5, sinais.s6]
    return " ".join(f"S{i+1}{'OK' if v else '--'}" for i, v in enumerate(flags))


def _formatar_preco(preco: float) -> str:
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
    s = "+" if sinal and valor >= 0 else ""
    return f"{s}{valor*100:.2f}%"


def _linha_leverage(lev_r) -> str:
    if lev_r is None:
        return "Leverage: — / R/R: —"
    return f"Leverage sugerida: <b>{lev_r.leverage}x</b> / R/R: <b>{lev_r.rr_ratio:.1f}:1</b>"


def _linhas_niveis(lev_r) -> str:
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

def _momento_1(symbol, token, resultado_scoring, lev_r, funding_flag, sizing) -> str:
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


def _momento_2(symbol, token, lev_r, sizing, notas, janela_horas, nivel_pre_breakout) -> str:
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


def _momento_3a(symbol, token) -> str:
    direccao = token.direccao if token else "—"
    score    = token.score_actual if token else 0
    return (
        f"⚫ <b>SETUP ENCERRADO — {symbol} {direccao}</b>\n"
        f"Score desceu para {score}/6 — acumulação perdida\n"
        f"Aguardar novo ciclo de acumulação"
    )


def _momento_3b(symbol, token, conclusao) -> str:
    if conclusao is None:
        return f"✅ <b>CONCLUÍDO — {symbol}</b>\nMove registado."

    direccao = token.direccao if token else "—"
    emoji    = {"POSITIVA": "✅", "NEUTRA": "🔶", "NEGATIVA": "❌"}.get(conclusao.tipo, "⚪")

    return (
        f"{emoji} <b>CONCLUÍDO — {symbol} {direccao}</b>\n\n"
        f"Move total: <b>{_formatar_pct(conclusao.ganho_atual_pct)}</b> / "
        f"Move máximo: <b>{_formatar_pct(conclusao.ganho_maximo_pct)}</b>\n"
        f"Duração: <b>{conclusao.horas_decorridas:.1f}h</b>\n"
        f"Tipo: <b>{conclusao.tipo}</b> — Condição {conclusao.condicao}"
    )


def _degradacao(symbol, token) -> str:
    direccao = token.direccao if token else "—"
    score    = token.score_actual if token else 0
    return (
        f"🔽 <b>DEGRADAÇÃO — {symbol} {direccao}</b>\n"
        f"Score desceu para {score}/6 (era 6/6)\n"
        f"Estado 3 → Estado 2 — scan de breakout desactivado\n"
        f"Monitorização continua em radar"
    )


def _saiu_universo(symbol, motivo="") -> str:
    m = f" — {motivo}" if motivo else ""
    return (
        f"🚫 <b>{symbol} saiu do universo</b>{m}\n"
        f"Grace period expirado — sem novos alertas do sistema"
    )


# =============================================================================
# RESUMO SCAN PESADO — substitui Momento 0 individual
# =============================================================================

def enviar_resumo_scan_pesado(
    hora_lisboa: int,
    novos_e2: list[dict],
    total_e2: int,
    total_e3: int,
    btc_acima_ema21: bool,
) -> bool:
    """
    Envia uma única mensagem consolidada no fim do scan pesado.

    novos_e2: lista de dicts com chaves:
        symbol, direccao, score, sinais (ResultadoSinais), funding_flag

    Se não houver novos tokens em E2, envia apenas o sumário (sem flood).
    """
    hora_str = f"{hora_lisboa:02d}:00"
    btc_str  = "↑ Longs e Shorts" if btc_acima_ema21 else "↓ Só Shorts"

    if not novos_e2:
        # Sem novos — mensagem silenciosa mínima
        texto = (
            f"📊 <b>Scan Pesado {hora_str} Lisboa</b>\n"
            f"Sem novos tokens em radar\n"
            f"Em supervisão: E2={total_e2} E3={total_e3} | BTC: {btc_str}"
        )
        log.info("Telegram → RESUMO_SCAN_PESADO (sem novos)")
        return _enviar(texto)

    # Com novos tokens
    linhas = [
        f"🟡 <b>Scan Pesado {hora_str} Lisboa — {len(novos_e2)} novo(s) em radar</b>\n"
    ]

    for item in novos_e2:
        symbol      = item["symbol"]
        direccao    = item["direccao"]
        score       = item["score"]
        sinais      = item.get("sinais")
        flag        = item.get("funding_flag", "")
        flag_str    = f" ⚠️{flag}" if flag else ""
        linha_s     = _linha_sinais_compacta(sinais)

        linhas.append(
            f"• <b>{symbol}</b> {direccao} {score}/6{flag_str}\n"
            f"  <code>{linha_s}</code>"
        )

    linhas.append(
        f"\nEm supervisão: E2={total_e2} E3={total_e3} | BTC: {btc_str}"
    )

    texto = "\n".join(linhas)
    log.info(f"Telegram → RESUMO_SCAN_PESADO ({len(novos_e2)} novos)")
    return _enviar(texto)


# =============================================================================
# UPDATE HORÁRIO — scan leve (secção 9.2)
# =============================================================================

def _formatar_update_horario(agora_utc, estado_json, btc_volatil) -> Optional[str]:
    hora_lisboa = datetime.now(TZ_LISBOA).strftime("%H:%M")

    estado4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]
    estado3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    estado2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]
    estado5 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 5]

    if not any([estado4, estado3, estado2, estado5]):
        return None

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
# FUNÇÕES PÚBLICAS
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
    sinais=None,
    **kwargs,
) -> bool:
    """
    Dispatcher principal.
    NOTA: MOMENTO_0 já não é enviado aqui individualmente.
    Usar enviar_resumo_scan_pesado() no fim do scan pesado.
    """
    notas = notas_prioridade or []

    if tipo == "MOMENTO_0":
        # Suprimido — acumulado e enviado via enviar_resumo_scan_pesado
        log.debug(f"[{symbol}] MOMENTO_0 suprimido — será incluído no resumo")
        return True

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


def enviar_update_horario(agora_utc, estado_json, btc_volatil) -> bool:
    texto = _formatar_update_horario(agora_utc, estado_json, btc_volatil)
    if texto is None:
        log.debug("Update horário: sem tokens activos — não enviado")
        return True
    log.info("Telegram → UPDATE_HORÁRIO")
    return _enviar(texto)


def enviar_alerta_degradacao(symbol, token, resultado_scoring) -> bool:
    return enviar_momento("DEGRADACAO", symbol, token, resultado_scoring)


# =============================================================================
# FORMATADORES PARA BOT — /supervisao e /token
# =============================================================================

def formatar_supervisao(estado_json: dict) -> str:
    """
    Formata lista completa de tokens em E2+ para o comando /supervisao.
    Ordenado: E3 primeiro, depois E2 por score descendente.
    """
    e3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    e2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]
    e4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]

    if not any([e2, e3, e4]):
        return "👁 Sem tokens em supervisão activa."

    # Ordenar E2 por score desc
    e2_sorted = sorted(e2, key=lambda x: x[1].get("score_actual", 0), reverse=True)

    linhas = [f"👁 <b>SUPERVISÃO ACTIVA</b> — {len(e2)+len(e3)+len(e4)} tokens\n"]

    if e4:
        linhas.append("🚨 <b>MOVES ACTIVOS (E4)</b>")
        for sym, c in e4:
            linhas.append(f"  • {sym} {c.get('direccao','—')} {c.get('score_actual',0)}/6")

    if e3:
        linhas.append("\n🔴 <b>ACUMULAÇÃO COMPLETA (E3)</b>")
        for sym, c in e3:
            linhas.append(f"  • {sym} {c.get('direccao','—')} {c.get('score_actual',0)}/6")

    if e2_sorted:
        # Agrupar por score
        scores_vistos = set()
        for sym, c in e2_sorted:
            sc = c.get("score_actual", 0)
            if sc not in scores_vistos:
                linhas.append(f"\n🟡 <b>{sc}/6</b>")
                scores_vistos.add(sc)
            dir_ = c.get("direccao", "—")
            scans = c.get("scans_consecutivos", 0)
            linhas.append(f"  • {sym} {dir_} ({scans} scans)")

    return "\n".join(linhas)


def formatar_token(symbol: str, estado_json: dict) -> str:
    """
    Formata info detalhada de um token para o comando /token SYMBOL.
    Funciona mesmo que o token esteja em E1 (sem classificação activa).
    """
    # Normalizar — aceitar com ou sem _USDT
    sym_upper = symbol.upper()
    if not sym_upper.endswith("_USDT"):
        sym_upper = sym_upper + "_USDT"

    campos = estado_json.get(sym_upper)

    if campos is None:
        return f"❓ <b>{sym_upper}</b> não encontrado no universo activo."

    estado     = campos.get("estado", 1)
    direccao   = campos.get("direccao", "—") or "—"
    score      = campos.get("score_actual", 0)
    scans      = campos.get("scans_consecutivos", 0)
    oi_atual   = campos.get("oi_atual", 0)
    oi_inicio  = campos.get("oi_inicio_dia", 0)
    vol_7d     = campos.get("volume_media_7d", 0)
    atr        = campos.get("atr_1h_pct", 0)
    categoria  = campos.get("categoria", "—")
    ts_entrada = campos.get("timestamp_entrada_estado", "—")

    # OI change dia
    oi_change_str = "—"
    if oi_inicio > 0:
        oi_change = (oi_atual - oi_inicio) / oi_inicio * 100
        sinal = "+" if oi_change >= 0 else ""
        oi_change_str = f"{sinal}{oi_change:.1f}%"

    # Estado emoji
    emoji_estado = {1: "⚪", 2: "🟡", 3: "🔴", 4: "🚨", 5: "✅"}.get(estado, "⚪")

    # Sinais herdados (E2+)
    sinais_str = ""
    sh = campos.get("sinais_herdados", {})
    if sh and estado >= 2:
        s2  = "OK" if sh.get("s2_long") or sh.get("s2_short") else "--"
        s3  = "OK" if sh.get("s3") else "--"
        s6  = "OK" if sh.get("s6_long") or sh.get("s6_short") else "--"
        sinais_str = f"\nSinais herdados: S2 {s2}  S3 {s3}  S6 {s6}"

    # Grace period
    grace_str = ""
    if campos.get("grace_period"):
        dias = campos.get("grace_period_dias_restantes", "?")
        grace_str = f"\n⚠️ Grace period — {dias} dia(s) restante(s)"

    # Timestamp entrada estado
    try:
        ts = datetime.fromisoformat(ts_entrada)
        ts_str = ts.strftime("%d/%m %H:%M UTC")
    except Exception:
        ts_str = ts_entrada[:16] if len(str(ts_entrada)) > 16 else str(ts_entrada)

    return (
        f"{emoji_estado} <b>{sym_upper}</b> — Estado {estado} {direccao}\n"
        f"Categoria: {categoria}\n\n"
        f"Score: <b>{score}/6</b> | Scans consecutivos: {scans}\n"
        f"Em estado desde: {ts_str}"
        f"{sinais_str}"
        f"{grace_str}\n\n"
        f"OI actual: <code>${oi_atual:,.0f}</code> ({oi_change_str} dia)\n"
        f"Volume média 7d: <code>${vol_7d:,.0f}</code>\n"
        f"ATR 1h: <code>{atr*100:.2f}%</code>"
    )
