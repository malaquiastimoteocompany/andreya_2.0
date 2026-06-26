#!/usr/bin/env python3
# =============================================================================
# audit_universo.py — Auditoria do universo CFI v2.0
# Compara tokens elegíveis na MEXC vs tokens no state.json
# Corre via GitHub Actions (audit.yml) — manual, sem schedule
# =============================================================================

import base64
import json
import logging
import urllib.parse
import urllib.request
import os
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("audit")

GITHUB_REPO     = os.environ.get("GITHUB_REPO", "malaquiastimoteocompany/andreya_2.0")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
STATE_JSON_PATH = os.environ.get("STATE_JSON_PATH", "state.json")
MEXC_BASE_URL   = os.environ.get("MEXC_BASE_URL", "https://contract.mexc.com/api/v1")
VOLUME_MIN      = float(os.environ.get("UNIVERSO_VOLUME_MIN_USD", "500000"))
BTC_TICKER      = "BTC_USDT"


def _mexc_get(endpoint):
    url = f"{MEXC_BASE_URL}{endpoint}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "andreya-audit", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            return resp.get("data", resp)
    except Exception as e:
        log.error(f"MEXC GET {endpoint} falhou: {e}")
        return None


def carregar_estado():
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_JSON_PATH}"
    req  = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "andreya-audit",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp     = json.loads(r.read())
        conteudo = base64.b64decode(resp["content"]).decode()
        return json.loads(conteudo)


def main():
    log.info("=== AUDITORIA DO UNIVERSO CFI v2.0 ===")
    log.info(f"Volume mínimo: ${VOLUME_MIN:,.0f}")

    # Carregar state.json
    try:
        estado_json = carregar_estado()
        log.info(f"state.json: {len(estado_json)} tokens")
    except Exception as e:
        log.error(f"Falha ao carregar state.json: {e}")
        return

    # Buscar tickers MEXC
    dados = _mexc_get("/contract/ticker")
    if not dados or not isinstance(dados, list):
        log.error("MEXC falhou — auditoria abortada")
        return

    tickers_mexc = {
        t["symbol"]: t for t in dados
        if "_USDT" in t.get("symbol", "") and t["symbol"] != BTC_TICKER
    }
    log.info(f"MEXC tickers USDT-M disponíveis: {len(tickers_mexc)}")

    # Separar em categorias
    no_universo     = []  # está no state.json E na MEXC
    em_falta        = []  # elegível na MEXC mas NÃO no state.json
    abaixo_volume   = []  # na MEXC mas volume insuficiente
    so_no_estado    = []  # no state.json mas não na MEXC (delisted?)
    grace_period    = []  # no state.json em grace period

    for symbol, ticker in tickers_mexc.items():
        vol = float(ticker.get("volume24", 0))
        if symbol in estado_json:
            campos = estado_json[symbol]
            if campos.get("grace_period"):
                grace_period.append({
                    "symbol": symbol,
                    "volume": vol,
                    "dias_restantes": campos.get("grace_period_dias_restantes"),
                    "estado": campos.get("estado", 1),
                })
            else:
                no_universo.append(symbol)
        else:
            if vol >= VOLUME_MIN:
                em_falta.append({
                    "symbol": symbol,
                    "volume": vol,
                    "oi":     float(ticker.get("holdVol", 0)),
                    "funding": float(ticker.get("fundingRate", 0)),
                })
            else:
                abaixo_volume.append(symbol)

    for symbol in estado_json:
        if symbol not in tickers_mexc and symbol != BTC_TICKER:
            so_no_estado.append(symbol)

    # Ordenar em_falta por volume desc
    em_falta.sort(key=lambda x: x["volume"], reverse=True)

    # ── RELATÓRIO ─────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"RESUMO:")
    log.info(f"  No universo (state.json + MEXC): {len(no_universo)}")
    log.info(f"  Em grace period:                 {len(grace_period)}")
    log.info(f"  EM FALTA (elegíveis, não no universo): {len(em_falta)}")
    log.info(f"  Abaixo do volume mínimo:         {len(abaixo_volume)}")
    log.info(f"  Só no state.json (possível delisted): {len(so_no_estado)}")
    log.info("=" * 60)

    if em_falta:
        log.info(f"\n🚨 TOKENS ELEGÍVEIS EM FALTA ({len(em_falta)}):")
        log.info(f"{'Symbol':<25} {'Volume 24h':>15} {'OI':>15} {'Funding':>10}")
        log.info("-" * 70)
        for t in em_falta:
            log.info(
                f"{t['symbol']:<25} "
                f"${t['volume']:>14,.0f} "
                f"${t['oi']:>14,.0f} "
                f"{t['funding']*100:>9.4f}%"
            )

    if grace_period:
        log.info(f"\n⚠️ EM GRACE PERIOD ({len(grace_period)}):")
        for t in grace_period:
            log.info(f"  {t['symbol']} — E{t['estado']} — {t['dias_restantes']} dias restantes — vol=${t['volume']:,.0f}")

    if so_no_estado:
        log.info(f"\n❓ SÓ NO STATE.JSON — possível delisted ({len(so_no_estado)}):")
        for s in so_no_estado:
            e = estado_json[s].get("estado", 1)
            log.info(f"  {s} — E{e}")

    log.info("\n=== AUDITORIA CONCLUÍDA ===")


if __name__ == "__main__":
    main()
