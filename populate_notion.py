#!/usr/bin/env python3
# =============================================================================
# populate_notion.py — População inicial do Notion com estado actual
# Executar UMA VEZ para sincronizar o Notion com o state.json do GitHub.
#
# O que faz:
#   1. Lê state.json do GitHub
#   2. Popula Base 2 (Tokens Monitorizados) com todos os tokens do universo
#   3. Cria uma entrada de snapshot na Base 1 (Scans)
#   4. Regista na Base 3 (Detecções) todos os tokens que já estão em Estado 2+
#
# Invocar: python populate_notion.py
# Ou via GitHub Actions: SCAN_TIPO=populate python populate_notion.py
# =============================================================================

import base64
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Setup de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("populate")

# Carregar config (precisa das env vars)
for k in ["GITHUB_TOKEN", "NOTION_TOKEN", "MEXC_API_KEY",
          "MEXC_API_SECRET", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY"]:
    if k not in os.environ:
        os.environ[k] = os.environ.get(k, "dummy")

sys.path.insert(0, ".")
from config import (
    GITHUB_REPO, GITHUB_TOKEN, STATE_JSON_PATH, TZ_LISBOA,
    NOTION_DB_SCANS, NOTION_DB_TOKENS, NOTION_DB_DETECCOES,
)
from notion_logger import (
    upsert_token, log_scan, _post, _converter_props,
    _titulo, _texto, _numero, _select, _checkbox, _ESTADO_LABEL,
)


# =============================================================================
# LER STATE.JSON DO GITHUB
# =============================================================================

def ler_state_json() -> dict:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_JSON_PATH}"
    req = urllib.request.Request(url, headers={
        "Authorization":  f"token {GITHUB_TOKEN}",
        "Accept":         "application/vnd.github.v3+json",
        "User-Agent":     "andreya-v2-populate",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    conteudo = base64.b64decode(resp["content"]).decode()
    return json.loads(conteudo)


# =============================================================================
# POPULAR BASE 2 — TOKENS MONITORIZADOS
# =============================================================================

def popular_base2(estado_json: dict) -> tuple[int, int]:
    """
    Para cada token no state.json, cria ou actualiza a entrada na Base 2.
    Retorna (criados, actualizados).
    """
    criados = 0
    erros   = 0
    total   = len(estado_json)

    log.info(f"Base 2 — a processar {total} tokens...")

    for i, (symbol, campos) in enumerate(estado_json.items(), 1):
        categoria         = campos.get("categoria", "Memes")
        estado            = campos.get("estado", 1)
        score             = campos.get("score_actual", 0)
        direccao          = campos.get("direccao", "") or "INDEFINIDO"
        grace_period      = campos.get("grace_period", False)
        grace_period_dias = campos.get("grace_period_dias_restantes")
        timestamp_entrada = campos.get("timestamp_entrada_estado")

        resultado = upsert_token(
            symbol=symbol,
            categoria=categoria,
            estado=estado,
            score=score,
            direccao=direccao,
            activo=True,
            grace_period=bool(grace_period),
            grace_period_dias=grace_period_dias,
            data_entrada=timestamp_entrada,
        )

        if resultado:
            criados += 1
        else:
            erros += 1

        # Log de progresso a cada 10 tokens e pausa anti-rate-limit
        if i % 10 == 0:
            log.info(f"  {i}/{total} processados...")
        time.sleep(0.35)  # Notion API: ~3 req/s no plano gratuito

    return criados, erros


# =============================================================================
# POPULAR BASE 1 — SNAPSHOT DO ESTADO ACTUAL
# =============================================================================

def popular_base1_snapshot(estado_json: dict) -> bool:
    """
    Cria uma entrada de snapshot na Base 1 com o estado actual do universo.
    """
    agora_lx   = datetime.now(TZ_LISBOA)
    hora_label = f"{agora_lx.hour:02d}h Lisboa (snapshot)"
    scan_id    = f"SC-SNAPSHOT-{agora_lx.strftime('%Y%m%dT%H%M')}"

    contagem = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for campos in estado_json.values():
        e = campos.get("estado", 1)
        contagem[e] = contagem.get(e, 0) + 1

    total = sum(contagem.values())

    props_raw = {
        "Scan ID":          _titulo(scan_id),
        "Hora":             _select("06h Lisboa"),  # usar o primeiro disponível
        "date:Data:start":  agora_lx.strftime("%Y-%m-%d"),
        "BTC Preço USDT":   _numero(0),
        "Filtro BTC":       _select("Apenas Shorts"),  # BTC estava abaixo EMA21
        "Total Analisados": _numero(total),
        "Em Estado 1":      _numero(contagem.get(1, 0)),
        "Em Estado 2":      _numero(contagem.get(2, 0)),
        "Em Estado 3":      _numero(contagem.get(3, 0)),
        "Em Estado 4":      _numero(contagem.get(4, 0)),
        "Novos Estado 2":   _numero(0),
        "Novos Estado 3":   _numero(0),
        "Breakouts":        _numero(0),
        "Concluídos":       _numero(0),
        "Grace Period":     _numero(0),
        "Misses":           _numero(0),
        "API Status":       _select("OK"),
        "Notas":            _texto("Snapshot de população inicial — dados do state.json"),
    }

    payload = {
        "parent":     {"database_id": NOTION_DB_SCANS},
        "properties": _converter_props(props_raw),
    }

    resp = _post("/pages", payload)
    if resp:
        log.info(f"Base 1 — snapshot criado: {scan_id}")
        return True
    log.error("Base 1 — falha ao criar snapshot")
    return False


# =============================================================================
# REGISTAR TOKENS EM ESTADO 2+ NA BASE 3
# =============================================================================

def popular_base3_activos(estado_json: dict) -> int:
    """
    Regista na Base 3 os tokens que já estão em Estado 2 ou superior,
    marcando-os como detecções pendentes sem scan_id histórico.
    """
    from notion_logger import _proximo_id
    agora_lx = datetime.now(TZ_LISBOA)
    hora_str = agora_lx.strftime("%H:%M")
    data_str = agora_lx.strftime("%Y-%m-%d")
    registados = 0

    tokens_activos = {
        sym: campos for sym, campos in estado_json.items()
        if campos.get("estado", 1) >= 2
    }

    log.info(f"Base 3 — a registar {len(tokens_activos)} tokens activos (Estado 2+)...")

    for symbol, campos in tokens_activos.items():
        det_id   = _proximo_id(NOTION_DB_DETECCOES, "DET", "Detecção ID")
        estado   = campos.get("estado", 2)
        score    = campos.get("score_actual", 0)
        direccao = campos.get("direccao", "INDEFINIDO") or "INDEFINIDO"

        props_raw = {
            "Detecção ID":     _titulo(det_id),
            "Token":           _texto(symbol),
            "date:Data:start": data_str,
            "Hora Lisboa":     _texto(hora_str),
            "Estado Anterior": _select("1"),
            "Estado Novo":     _select(str(min(estado, 5))),
            "Score":           _numero(score),
            "Direcção":        _select(direccao),
            "S1": _checkbox(False), "S2": _checkbox(False),
            "S3": _checkbox(False), "S4": _checkbox(False),
            "S5": _checkbox(False), "S6": _checkbox(False),
            "ATR 1h %":        _numero(round(campos.get("atr_1h_pct", 0) * 100, 4)),
            "Funding Flag":    _select("Nenhuma"),
            "BTC acima EMA21": _checkbox(False),
            "Scan ID":         _texto("SC-SNAPSHOT"),
            "Resultado Final": _select("Pendente"),
            "Horas no Estado": _numero(0),
            "Salto Directo":   _checkbox(bool(campos.get("salto_directo", False))),
            "Bloqueado Filtro BTC": _checkbox(False),
            "Miss Detectado":  _checkbox(False),
        }

        payload = {
            "parent":     {"database_id": NOTION_DB_DETECCOES},
            "properties": _converter_props(props_raw),
        }

        resp = _post("/pages", payload)
        if resp:
            registados += 1
            log.debug(f"  [Base 3] {symbol} Estado {estado} registado")
        else:
            log.warning(f"  [Base 3] {symbol} — falha")

        time.sleep(0.35)

    return registados


# =============================================================================
# MAIN
# =============================================================================

def main():
    log.info("=" * 60)
    log.info("POPULATE NOTION — população inicial do andreya_v2")
    log.info("=" * 60)

    # 1. Ler state.json
    log.info("A ler state.json do GitHub...")
    try:
        estado_json = ler_state_json()
        log.info(f"state.json lido — {len(estado_json)} tokens")
    except Exception as e:
        log.error(f"Falha ao ler state.json: {e}")
        sys.exit(1)

    if not estado_json:
        log.warning("state.json está vazio — nada a fazer")
        sys.exit(0)

    # Estatísticas rápidas
    contagem = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for c in estado_json.values():
        e = c.get("estado", 1)
        contagem[e] = contagem.get(e, 0) + 1
    log.info(f"Distribuição: E1={contagem[1]} E2={contagem[2]} "
             f"E3={contagem[3]} E4={contagem[4]} E5={contagem[5]}")

    # 2. Snapshot Base 1
    log.info("-" * 40)
    popular_base1_snapshot(estado_json)
    time.sleep(0.5)

    # 3. Popular Base 2 (todos os tokens)
    log.info("-" * 40)
    criados, erros = popular_base2(estado_json)
    log.info(f"Base 2 — concluído: {criados} tokens registados, {erros} erros")
    time.sleep(0.5)

    # 4. Registar activos na Base 3
    log.info("-" * 40)
    registados = popular_base3_activos(estado_json)
    log.info(f"Base 3 — concluído: {registados} detecções registadas")

    log.info("=" * 60)
    log.info("POPULAÇÃO CONCLUÍDA")
    log.info(f"  Base 1: 1 snapshot criado")
    log.info(f"  Base 2: {criados} tokens")
    log.info(f"  Base 3: {registados} detecções activas")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
