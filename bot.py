#!/usr/bin/env python3 
# =============================================================================
# bot.py — Bot Telegram Andreya (Railway)
# Dispatcher de comandos + reencaminhamento para GitHub Actions
#
# Comandos:
#   /scansimple   → dispara scan leve
#   /scanfull     → dispara scan pesado
#   /analise_token SYMBOL → dispara análise de token específico (via Actions)
#   /supervisao   → lista todos os tokens em E2+ com scores
#   /token SYMBOL → info detalhada de um token do state.json
#   /status       → estado geral do sistema
# =============================================================================

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger("bot")

# ── Configuração ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHATS  = set(os.environ.get("ALLOWED_CHAT_IDS", "-1003811042997").split(","))
GITHUB_TOKEN   = os.environ["GITHUB_TOKEN"]
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "malaquiastimoteocompany/andreya_2.0")
STATE_JSON_PATH = os.environ.get("STATE_JSON_PATH", "state.json")
TZ_LISBOA      = ZoneInfo("Europe/Lisbon")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# =============================================================================
# TELEGRAM — helpers
# =============================================================================

def _tg_request(method: str, payload: dict) -> dict:
    url  = f"{TELEGRAM_API}/{method}"
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def enviar_mensagem(chat_id: str, texto: str) -> None:
    try:
        _tg_request("sendMessage", {
            "chat_id":    chat_id,
            "text":       texto,
            "parse_mode": "HTML",
        })
    except Exception as e:
        log.error(f"Falha ao enviar mensagem: {e}")


def get_updates(offset: int) -> list[dict]:
    try:
        resp = _tg_request("getUpdates", {
            "offset":  offset,
            "timeout": 30,
        })
        return resp.get("result", [])
    except urllib.error.URLError as e:
        if "timed out" in str(e).lower():
            return []  # timeout normal — sem mensagens
        log.error(f"getUpdates falhou: {e}")
        return []
    except Exception as e:
        log.error(f"getUpdates falhou: {e}")
        return []

# =============================================================================
# GITHUB ACTIONS — dispatch
# =============================================================================

def _dispatch_workflow(workflow: str, inputs: dict) -> bool:
    """Dispara um workflow GitHub Actions via workflow_dispatch."""
    url  = (f"https://api.github.com/repos/{GITHUB_REPO}"
            f"/actions/workflows/{workflow}/dispatches")
    data = json.dumps({"ref": "main", "inputs": inputs}).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
            "User-Agent":    "andreya-bot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 204
    except urllib.error.HTTPError as e:
        log.error(f"GitHub dispatch {workflow} falhou: {e.code} {e.read()}")
        return False
    except Exception as e:
        log.error(f"GitHub dispatch {workflow} erro: {e}")
        return False


# =============================================================================
# GITHUB — leitura do state.json
# =============================================================================

def _carregar_estado() -> dict:
    """Lê state.json do GitHub. Retorna {} em caso de erro."""
    import base64
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_JSON_PATH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "User-Agent":    "andreya-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            conteudo = base64.b64decode(resp["content"]).decode()
            return json.loads(conteudo)
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return {}


def _carregar_s2b_outcomes() -> list[dict]:
    """Lê s2b_outcomes.json do GitHub. Retorna [] se ainda não existir ou em caso de erro."""
    import base64
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/s2b_outcomes.json"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "User-Agent":    "andreya-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            conteudo = base64.b64decode(resp["content"]).decode()
            return json.loads(conteudo)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []  # ainda não houve nenhum sinal S2b registado
        log.error(f"Falha ao carregar s2b_outcomes.json: {e}")
        return []
    except Exception as e:
        log.error(f"Falha ao carregar s2b_outcomes.json: {e}")
        return []


def _formatar_s2b_stats(outcomes: list[dict]) -> str:
    if not outcomes:
        return "📊 <b>S2b — Dados Recolhidos</b>\n\nAinda não há nenhum sinal S2b registado."

    completos = [o for o in outcomes if o.get("completo")]
    abertos   = [o for o in outcomes if not o.get("completo")]

    linhas = [
        "📊 <b>S2b — Dados Recolhidos</b>",
        "",
        f"Total de sinais: <b>{len(outcomes)}</b>",
        f"Com as 24h completas: <b>{len(completos)}</b>",
        f"Ainda a decorrer: <b>{len(abertos)}</b>",
    ]

    if abertos:
        media_pontos = sum(len(o.get("precos", {})) for o in abertos) / len(abertos)
        linhas.append(f"  — em média {media_pontos:.0f}/48 pontos (30 em 30 min) já guardados")

    linhas.append("")
    linhas.append("<i>Estatística de sucesso/timing por indicador faz-se à parte, quando houver mais dados.</i>")

    return "\n".join(linhas)


# =============================================================================
# FORMATADORES — importados inline para não depender de módulos do scanner
# =============================================================================

def _formatar_supervisao(estado_json: dict) -> str:
    e3 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 3]
    e2 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 2]
    e4 = [(s, c) for s, c in estado_json.items() if c.get("estado") == 4]

    if not any([e2, e3, e4]):
        return "👁 Sem tokens em supervisão activa."

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
        scores_vistos = set()
        for sym, c in e2_sorted:
            sc = c.get("score_actual", 0)
            if sc not in scores_vistos:
                linhas.append(f"\n🟡 <b>{sc}/6</b>")
                scores_vistos.add(sc)
            dir_   = c.get("direccao", "—")
            scans  = c.get("scans_consecutivos", 0)
            linhas.append(f"  • {sym} {dir_} ({scans} scans)")

    return "\n".join(linhas)


def _formatar_token(symbol: str, estado_json: dict) -> str:
    sym_upper = symbol.upper()
    if not sym_upper.endswith("_USDT"):
        sym_upper = sym_upper + "_USDT"

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


def _formatar_status(estado_json: dict) -> str:
    contagem = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for c in estado_json.values():
        e = c.get("estado", 1)
        contagem[e] = contagem.get(e, 0) + 1
    total = sum(contagem.values())
    hora = datetime.now(TZ_LISBOA).strftime("%H:%M")

    return (
        f"📊 <b>CFI v2.0 — Estado do Sistema</b>\n"
        f"<code>{hora} Lisboa</code>\n\n"
        f"Universo: <b>{total} tokens</b>\n"
        f"⚪ E1 (passivo): {contagem[1]}\n"
        f"🟡 E2 (radar): {contagem[2]}\n"
        f"🔴 E3 (prioritário): {contagem[3]}\n"
        f"🚨 E4 (breakout activo): {contagem[4]}\n"
        f"✅ E5 (concluído): {contagem[5]}"
    )


# =============================================================================
# PROCESSAMENTO DE COMANDOS
# =============================================================================

def processar_comando(chat_id: str, texto: str) -> None:
    partes  = texto.strip().split()
    comando = partes[0].lower().split("@")[0]  # remove @botname se presente

    log.info(f"Comando: {comando} | Chat: {chat_id}")

    # ── /scansimple — scan leve ───────────────────────────────────────────────
    if comando == "/scansimple":
        ok = _dispatch_workflow("scanner.yml", {"scan_tipo": "leve"})
        if ok:
            enviar_mensagem(chat_id, "✅ Scan leve iniciado.")
        else:
            enviar_mensagem(chat_id, "❌ Falha ao iniciar scan leve.")

    # ── /scanfull — scan pesado ───────────────────────────────────────────────
    elif comando == "/scanfull":
        ok = _dispatch_workflow("scanner.yml", {"scan_tipo": "pesado"})
        if ok:
            enviar_mensagem(chat_id, "✅ Scan pesado iniciado.")
        else:
            enviar_mensagem(chat_id, "❌ Falha ao iniciar scan pesado.")

    # ── /analise_token SYMBOL ─────────────────────────────────────────────────
    elif comando == "/analise_token":
        if len(partes) < 2:
            enviar_mensagem(chat_id, "Uso: /analise_token SYMBOL\nEx: /analise_token SHIB")
            return
        symbol = partes[1].upper()
        ok = _dispatch_workflow("scanner.yml", {
            "scan_tipo": "analise_token",
            "symbol":    symbol,
        })
        if ok:
            enviar_mensagem(chat_id, f"✅ Análise de {symbol} iniciada.")
        else:
            enviar_mensagem(chat_id, f"❌ Falha ao iniciar análise de {symbol}.")

    # ── /supervisao — lista E2+ ───────────────────────────────────────────────
    elif comando == "/supervisao":
        enviar_mensagem(chat_id, "⏳ A carregar state.json...")
        estado = _carregar_estado()
        if not estado:
            enviar_mensagem(chat_id, "❌ Não foi possível carregar o estado.")
            return
        texto_resp = _formatar_supervisao(estado)
        # Dividir se demasiado longo (limite Telegram: 4096 chars)
        if len(texto_resp) > 4000:
            partes_msg = [texto_resp[i:i+4000] for i in range(0, len(texto_resp), 4000)]
            for parte in partes_msg:
                enviar_mensagem(chat_id, parte)
                time.sleep(0.5)
        else:
            enviar_mensagem(chat_id, texto_resp)

    # ── /s2b_stats — dados recolhidos pelo gatilho S2b ──────────────────────────
    elif comando == "/s2b_stats":
        outcomes = _carregar_s2b_outcomes()
        enviar_mensagem(chat_id, _formatar_s2b_stats(outcomes))

    # ── /token SYMBOL — info detalhada ────────────────────────────────────────
    elif comando == "/token":
        if len(partes) < 2:
            enviar_mensagem(chat_id, "Uso: /token SYMBOL\nEx: /token SHIB\nEx: /token SHIB_USDT")
            return
        symbol = partes[1]
        estado = _carregar_estado()
        if not estado:
            enviar_mensagem(chat_id, "❌ Não foi possível carregar o estado.")
            return
        texto_resp = _formatar_token(symbol, estado)
        enviar_mensagem(chat_id, texto_resp)

    # ── /status — sumário geral ───────────────────────────────────────────────
    elif comando == "/status":
        estado = _carregar_estado()
        if not estado:
            enviar_mensagem(chat_id, "❌ Não foi possível carregar o estado.")
            return
        enviar_mensagem(chat_id, _formatar_status(estado))

    # ── Comando desconhecido ──────────────────────────────────────────────────
    else:
        enviar_mensagem(
            chat_id,
            "Comandos disponíveis:\n"
            "/scanfull — scan pesado manual\n"
            "/scansimple — scan leve manual\n"
            "/analise_token SYMBOL — análise de token\n"
            "/supervisao — lista tokens em radar\n"
            "/token SYMBOL — info detalhada de token\n"
            "/s2b_stats — dados recolhidos pelo gatilho S2b\n"
            "/status — estado geral do sistema"
        )


# =============================================================================
# LOOP PRINCIPAL
# =============================================================================

def main() -> None:
    log.info("Andreya bot iniciado.")
    offset = 0

    while True:
        updates = get_updates(offset)

        for update in updates:
            offset = update["update_id"] + 1

            msg = update.get("message") or update.get("edited_message") or {}
            if not msg:
                continue

            chat_id = str(msg.get("chat", {}).get("id", ""))
            texto   = msg.get("text", "").strip()

            # Segurança — só chats autorizados
            if chat_id not in ALLOWED_CHATS:
                log.warning(f"Chat não autorizado: {chat_id}")
                continue

            if not texto.startswith("/"):
                continue

            try:
                processar_comando(chat_id, texto)
            except Exception as e:
                log.error(f"Erro a processar comando: {e}")
                enviar_mensagem(chat_id, f"❌ Erro interno: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
