# =============================================================================
# notificacoes.py — Formatação e envio de mensagens Telegram
# Manual CFI v2.0 — Secção 9.1 e 9.2
#
# Filosofia:
#   - Scan pesado  → 1 mensagem sempre (resumo consolidado)
#   - Scan leve    → até 2 mensagens (eventos + estado actual)
#   - Breakout     → 1 mensagem imediata por token (única excepção)
#   - Tudo o resto → lista consolidada, nunca flood
#
# Formato: HTML (<b>, <i>, <code>) — nunca MarkdownV2
# =============================================================================

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Optional
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

def _hora_lisboa() -> str:
    return datetime.now(TZ_LISBOA).strftime("%H:%M")


def _linha_sinais(sinais) -> str:
    """Sempre os 6 sinais. Ex: S1OK S2-- S3OK S4OK S5-- S6OK"""
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


def _linhas_niveis(lev_r) -> str:
    if lev_r is None:
        return ""
    tp2_str = (
        f"\nTP2: <code>{_formatar_preco(lev_r.tp2_preco)}</code> "
        f"(<b>{_formatar_pct(lev_r.tp2_pct)}</b>)"
        if lev_r.tp_tipo == "escalonado" and lev_r.tp2_preco else ""
    )
    return (
        f"SL: <code>{_formatar_preco(lev_r.sl_preco)}</code> "
        f"(<b>-{_formatar_pct(lev_r.sl_pct, sinal=False)}</b>)\n"
        f"TP1: <code>{_formatar_preco(lev_r.tp1_preco)}</code> "
        f"(<b>{_formatar_pct(lev_r.tp1_pct)}</b>)"
        f"{tp2_str}"
    )


def _linha_leverage(lev_r) -> str:
    if lev_r is None:
        return ""
    return f"Leverage: <b>{lev_r.leverage}x</b> | R/R: <b>{lev_r.rr_ratio:.1f}:1</b>"


def _btc_str(btc_acima_ema21: bool) -> str:
    return "↑ Longs e Shorts" if btc_acima_ema21 else "↓ Só Shorts"


# =============================================================================
# SCAN PESADO — 1 mensagem única no fim
# =============================================================================

def enviar_resumo_scan_pesado(
    hora_lisboa: int,
    novos_e2: list[dict],
    novos_e3: list[dict],
    saidos_universo: list[str],
    total_e2: int,
    total_e3: int,
    total_e4: int,
    btc_acima_ema21: bool,
) -> bool:
    """
    1 mensagem única no fim do scan pesado.

    novos_e2: [{symbol, direccao, score, sinais, funding_flag}]
    novos_e3: [{symbol, direccao, score, sinais, lev_r, funding_flag}]
    saidos_universo: [symbol, ...]
    """
    hora_str = f"{hora_lisboa:02d}:00"
    btc      = _btc_str(btc_acima_ema21)
    rodape   = f"Em supervisão: E2={total_e2} E3={total_e3} E4={total_e4} | BTC: {btc}"

    linhas = []

    # ── Novos E3 ─────────────────────────────────────────────────────────────
    if novos_e3:
        linhas.append(f"🔴 <b>ACUMULAÇÃO COMPLETA — {len(novos_e3)} token(s) em E3</b>")
        for item in novos_e3:
            sym      = item["symbol"]
            dir_     = item["direccao"]
            score    = item["score"]
            sinais   = item.get("sinais")
            lev_r    = item.get("lev_r")
            flag     = item.get("funding_flag", "")
            flag_str = f" ⚠️{flag}" if flag else ""
            linha_s  = _linha_sinais(sinais)
            linhas.append(
                f"\n• <b>{sym}</b> {dir_} {score}/6{flag_str}\n"
                f"  <code>{linha_s}</code>"
            )
            if lev_r:
                metodo = "Método A — heatmap" if lev_r.target_metodo == "A" else "Método C — 3×ATR"
                linhas.append(
                    f"  Entry: <code>{_formatar_preco(lev_r.preco_entry)}</code> | "
                    f"{_linha_leverage(lev_r)}\n"
                    f"  {_linhas_niveis(lev_r)}\n"
                    f"  {metodo} | Sizing: {lev_r.sizing*100:.0f}%"
                )
        linhas.append("\n<i>NÃO É SINAL DE ENTRADA — aguardar breakout</i>")

    # ── Novos E2 ─────────────────────────────────────────────────────────────
    if novos_e2:
        if linhas:
            linhas.append("")
        linhas.append(f"🟡 <b>{len(novos_e2)} novo(s) em radar</b>")
        for item in novos_e2:
            sym      = item["symbol"]
            dir_     = item["direccao"]
            score    = item["score"]
            sinais   = item.get("sinais")
            flag     = item.get("funding_flag", "")
            flag_str = f" ⚠️{flag}" if flag else ""
            linha_s  = _linha_sinais(sinais)
            linhas.append(
                f"• <b>{sym}</b> {dir_} {score}/6{flag_str}\n"
                f"  <code>{linha_s}</code>"
            )

    # ── Saídos do universo ────────────────────────────────────────────────────
    if saidos_universo:
        if linhas:
            linhas.append("")
        linhas.append(f"🚫 <b>{len(saidos_universo)} saíram do universo</b>")
        for sym in saidos_universo:
            linhas.append(f"• {sym}")

    # ── Sem eventos ───────────────────────────────────────────────────────────
    if not linhas:
        texto = (
            f"📊 <b>Scan Pesado {hora_str} Lisboa</b>\n"
            f"Sem novos tokens\n"
            f"{rodape}"
        )
        log.info("Telegram → RESUMO_SCAN_PESADO (sem eventos)")
        return _enviar(texto)

    # ── Com eventos ───────────────────────────────────────────────────────────
    texto = f"📊 <b>Scan Pesado {hora_str} Lisboa</b>\n\n" + "\n".join(linhas) + f"\n\n{rodape}"
    log.info(f"Telegram → RESUMO_SCAN_PESADO (E3={len(novos_e3)} E2={len(novos_e2)} saídos={len(saidos_universo)})")
    return _enviar(texto)


# =============================================================================
# SCAN LEVE — até 2 mensagens
# =============================================================================

def enviar_resumo_scan_leve(
    hora_lisboa: int,
    encerrados: list[dict],
    degradados: list[dict],
    concluidos: list[dict],
    estado_json: dict,
    btc_acima_ema21: bool,
) -> bool:
    """
    Mensagem 1 — eventos (só se houve algo)
    Mensagem 2 — estado actual (só se há tokens activos)
    Silêncio total se não houve nada e não há activos.

    encerrados: [{symbol, direccao, score}]   — E2→E1
    degradados: [{symbol, direccao, score}]   — E3→E2
    concluidos: [{symbol, direccao, ganho_pct, horas, condicao}]  — E4→E5
    """
    hora_str = f"{hora_lisboa:02d}:00"
    btc      = _btc_str(btc_acima_ema21)
    ok       = True

    # ── Mensagem 1 — Eventos ─────────────────────────────────────────────────
    if encerrados or degradados or concluidos:
        linhas = [f"⚡ <b>Eventos — Scan Leve {hora_str} Lisboa</b>"]

        if concluidos:
            linhas.append("\n✅ <b>CONCLUÍDOS (E4→E5)</b>")
            for item in concluidos:
                ganho = _formatar_pct(item.get("ganho_pct", 0))
                horas = item.get("horas", 0)
                cond  = item.get("condicao", "—")
                linhas.append(
                    f"• <b>{item['symbol']}</b> {item.get('direccao','—')} "
                    f"— {ganho} em {horas:.1f}h (Cond. {cond})"
                )

        if degradados:
            linhas.append("\n🔽 <b>DEGRADAÇÕES (E3→E2)</b>")
            for item in degradados:
                linhas.append(
                    f"• <b>{item['symbol']}</b> {item.get('direccao','—')} "
                    f"— score desceu para {item.get('score',0)}/6"
                )

        if encerrados:
            linhas.append("\n⚫ <b>ENCERRADOS (E2→E1)</b>")
            for item in encerrados:
                linhas.append(
                    f"• <b>{item['symbol']}</b> {item.get('direccao','—')} "
                    f"— score desceu para {item.get('score',0)}/6"
                )

        ok = _enviar("\n".join(linhas))
        log.info(f"Telegram → LEVE_EVENTOS (enc={len(encerrados)} deg={len(degradados)} conc={len(concluidos)})")

    # ── Mensagem 2 — Estado actual ────────────────────────────────────────────
    e4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]
    e3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    e2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]

    if not any([e4, e3, e2]):
        log.debug("Scan leve: sem tokens activos — estado não enviado")
        return ok

    e2_sorted = sorted(e2, key=lambda x: x[1].get("score_actual", 0), reverse=True)

    linhas = [f"🔄 <b>Scan Leve {hora_str} Lisboa</b>"]

    if e4:
        linhas.append("\n🚨 <b>E4 — move activo</b>")
        for sym, c in e4:
            dir_ = c.get("direccao", "—")
            ts   = c.get("trigger_timestamp", "")
            try:
                delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
                horas_str = f"{delta.seconds//3600}h{(delta.seconds%3600)//60}m"
            except Exception:
                horas_str = "—"
            linhas.append(f"• <b>{sym}</b> {dir_} (há {horas_str})")

    if e3:
        linhas.append("\n🔴 <b>E3 — acumulação completa</b>")
        for sym, c in e3:
            dir_  = c.get("direccao", "—")
            score = c.get("score_actual", 0)
            scans = c.get("scans_consecutivos", 0)
            linhas.append(f"• <b>{sym}</b> {dir_} {score}/6 ({scans} scans)")

    if e2_sorted:
        linhas.append("\n🟡 <b>E2 — radar</b>")
        for sym, c in e2_sorted:
            dir_  = c.get("direccao", "—")
            score = c.get("score_actual", 0)
            s_ant = c.get("score_anterior", score)
            delta = score - s_ant
            seta  = " ↑" if delta > 0 else " ↓" if delta < 0 else " →"
            linhas.append(f"• <b>{sym}</b> {dir_} {score}/6{seta}")

    linhas.append(f"\nBTC: {btc}")

    ok = _enviar("\n".join(linhas)) and ok
    log.info("Telegram → LEVE_ESTADO")
    return ok


# =============================================================================
# BREAKOUT — 1 mensagem imediata por token
# =============================================================================

def enviar_momento_breakout(
    symbol: str,
    token,
    lev_r,
    sizing: float,
    notas: list[str],
    janela_horas: int,
    nivel_pre_breakout: float = 0.0,
) -> bool:
    """Momento 2 — única mensagem individual do sistema."""
    direccao  = token.direccao if token else "—"
    notas_str = ""
    if "BTC_VOLATIL_NO_TRIGGER" in notas:
        notas_str += "⚠️ <b>BTC VOLÁTIL NO TRIGGER</b> — alerta atrasado\n"
    if "TARGET_ACTUALIZADO" in notas:
        notas_str += "ℹ️ Target actualizado face ao Momento 1\n"
    if "GRACE_PERIOD_ACTIVO" in notas:
        notas_str += "⚠️ Token em grace period\n"

    nivel_str  = (
        f"Nível pré-breakout: <code>{_formatar_preco(nivel_pre_breakout)}</code>\n"
        if nivel_pre_breakout else ""
    )
    metodo_str = (
        "Método A — cluster heatmap"
        if lev_r and lev_r.target_metodo == "A"
        else "Método C — 3×ATR"
    )

    texto = (
        f"🚨 <b>BREAKOUT — {symbol} {direccao}</b>\n"
        f"<b>ENTRA AGORA OU NÃO ENTRAS</b>\n\n"
        f"{notas_str}"
        f"Janela: <b>{janela_horas}h</b> após este alerta\n"
        f"{nivel_str}\n"
        f"Entry: mercado\n"
        f"{_linhas_niveis(lev_r)}\n"
        f"{_linha_leverage(lev_r)}\n"
        f"{metodo_str} | Sizing: <b>{sizing*100:.0f}%</b>"
    )

    log.info(f"[{symbol}] Telegram → BREAKOUT")
    return _enviar(texto)


# =============================================================================
# COMPATIBILIDADE COM scanner.py
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
    Dispatcher de compatibilidade.
    Só MOMENTO_2 é enviado individualmente.
    Todos os outros são acumulados no scanner e enviados via enviar_resumo_*.
    """
    notas = notas_prioridade or []

    if tipo == "MOMENTO_2":
        return enviar_momento_breakout(
            symbol=symbol,
            token=token,
            lev_r=resultado_leverage,
            sizing=sizing,
            notas=notas,
            janela_horas=janela_horas,
            nivel_pre_breakout=nivel_pre_breakout,
        )

    # Todos os outros acumulados no scanner
    log.debug(f"[{symbol}] {tipo} — acumulado para resumo")
    return True


def enviar_update_horario(agora_utc: str, estado_json: dict, btc_volatil: bool) -> bool:
    """Mantido para compatibilidade — gerido via enviar_resumo_scan_leve."""
    return True


def enviar_alerta_degradacao(symbol: str, token, resultado_scoring) -> bool:
    """Mantido para compatibilidade — acumulado no scanner."""
    log.debug(f"[{symbol}] Degradação acumulada")
    return True


# =============================================================================
# FORMATADORES PARA BOT — /supervisao e /token
# =============================================================================

def formatar_supervisao(estado_json: dict) -> str:
    e4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]
    e3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    e2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]

    if not any([e4, e3, e2]):
        return "👁 Sem tokens em supervisão activa."

    e2_sorted = sorted(e2, key=lambda x: x[1].get("score_actual", 0), reverse=True)
    total = len(e4) + len(e3) + len(e2)
    linhas = [f"👁 <b>SUPERVISÃO ACTIVA — {total} tokens</b>\n"]

    if e4:
        linhas.append("🚨 <b>E4 — move activo</b>")
        for sym, c in e4:
            linhas.append(f"  • {sym} {c.get('direccao','—')}")

    if e3:
        linhas.append("\n🔴 <b>E3 — acumulação completa</b>")
        for sym, c in e3:
            linhas.append(f"  • {sym} {c.get('direccao','—')} {c.get('score_actual',0)}/6")

    if e2_sorted:
        scores_vistos = set()
        for sym, c in e2_sorted:
            sc = c.get("score_actual", 0)
            if sc not in scores_vistos:
                linhas.append(f"\n🟡 <b>{sc}/6</b>")
                scores_vistos.add(sc)
            linhas.append(f"  • {sym} {c.get('direccao','—')} ({c.get('scans_consecutivos',0)} scans)")

    return "\n".join(linhas)


def formatar_token(symbol: str, estado_json: dict) -> str:
    sym_upper = symbol.upper()
    if not sym_upper.endswith("_USDT"):
        sym_upper += "_USDT"

    campos = estado_json.get(sym_upper)
    if campos is None:
        return f"❓ <b>{sym_upper}</b> não encontrado no universo activo."

    estado    = campos.get("estado", 1)
    direccao  = campos.get("direccao") or "—"
    score     = campos.get("score_actual", 0)
    scans     = campos.get("scans_consecutivos", 0)
    oi_atual  = campos.get("oi_atual", 0)
    oi_inicio = campos.get("oi_inicio_dia", 0)
    vol_7d    = campos.get("volume_media_7d", 0)
    atr       = campos.get("atr_1h_pct", 0)
    categoria = campos.get("categoria", "—")
    ts_entrada = campos.get("timestamp_entrada_estado", "—")

    oi_change_str = "—"
    if oi_inicio > 0:
        oi_change = (oi_atual - oi_inicio) / oi_inicio * 100
        sinal = "+" if oi_change >= 0 else ""
        oi_change_str = f"{sinal}{oi_change:.1f}%"

    emoji_estado = {1: "⚪", 2: "🟡", 3: "🔴", 4: "🚨", 5: "✅"}.get(estado, "⚪")

    sinais_str = ""
    sh = campos.get("sinais_herdados", {})
    if sh and estado >= 2:
        s2 = "OK" if sh.get("s2_long") or sh.get("s2_short") else "--"
        s3 = "OK" if sh.get("s3") else "--"
        s6 = "OK" if sh.get("s6_long") or sh.get("s6_short") else "--"
        sinais_str = f"\nSinais herdados: S2 {s2}  S3 {s3}  S6 {s6}"

    grace_str = ""
    if campos.get("grace_period"):
        dias = campos.get("grace_period_dias_restantes", "?")
        grace_str = f"\n⚠️ Grace period — {dias} dia(s) restante(s)"

    try:
        ts = datetime.fromisoformat(ts_entrada)
        ts_str = ts.strftime("%d/%m %H:%M UTC")
    except Exception:
        ts_str = str(ts_entrada)[:16]

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
