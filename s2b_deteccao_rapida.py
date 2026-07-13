"""
s2b_deteccao_rapida.py — Detector paralelo de 5 min (terceira via de gatilho)
================================================================================

Nasceu 13/07/2026. Investigação sobre "porque entramos tarde" no S2b
(Amostra Real, 32 trades reais, monitorização a 1min) encontrou
|var_preco_gatilho| como o sinal mais forte de todos (correlação -0.506
com PnL) — sinais que disparam com movimento já grande na própria vela
do gatilho consomem a maior parte do movimento antes de sequer entrarmos.
14.3% win rate acima de 20% de movimento vs 60% dentro do tecto.

Teste exploratório (13/07/2026, FORA deste sistema — reconstruído via
histórico da MEXC, não dados próprios registados ao vivo) sugeriu que
verificar a cada 5 min, com threshold mais baixo, apanha o mesmo
movimento ~94 min mais cedo em média, com magnitude ~5x menor (2.71%
vs 13.13%). Simulação (15min forward, ATR aproximado) deu +$1.75 vs
-$0.06 nos mesmos 27 tokens comparados ao resultado real actual — mas
isto é só simulação. Este script existe para gerar dados REAIS sobre a
ideia, em paralelo ao gatilho clássico e ao sobe-desce, sem os substituir.

DESENHO:
  - Cadência própria: 5 em 5 min (cron-job.org + workflow dedicado),
    independente dos 15 min do s2b_v2.py.
  - Estado próprio (s2b_historico_5min.json) — não escreve no
    s2b_historico.json clássico, para não arriscar condição de corrida
    com o gatilho de 15 min (que já lê/escreve esse ficheiro a cada
    execução sua, a cada 15 min).
  - Antes de disparar, LÊ (nunca escreve) o s2b_historico.json clássico,
    para não abrir posição num token já em observação por outro
    mecanismo. Não escreve lá — por isso é tecnicamente possível
    (esperado raro) que o clássico dispare no mesmo token pouco depois.
    A vigiar com dados reais antes de decidir se precisa de resolver.
  - Gatilho: preço move >=1% vs a leitura de 5 min antes, qualquer
    direcção. SEM confirmação de volume — o teste exploratório de
    13/07/2026 não incluiu volume, por isso não se acrescenta aqui sem
    validar primeiro; é o candidato óbvio a testar a seguir, uma vez
    acumulada amostra real.
  - Reaproveita snapshot_completo() e fetch_todos_tickers() do
    s2b_v2.py — não duplica essa lógica. Mesma elegibilidade (volume
    >=250k, filtro de sintéticos *STOCK_USDT).
  - Regista no MESMO s2b_outcomes_v2.json que os outros dois mecanismos,
    tipo_gatilho="deteccao_rapida_5min". O scan_s2b() clássico (que já
    corre a cada 15 min) trata dos checkpoints e da conclusão às 24h
    para QUALQUER registo do outcomes file, independente do tipo_gatilho
    — não precisa de lógica própria aqui para isso. Só precisa de
    libertar o PRÓPRIO em_observacao (no ficheiro próprio) às 24h,
    porque o clássico não sabe deste ficheiro.
  - O módulo de execução paper (execucao_paper/s2b_execucao_paper.py) já
    apanha qualquer sinal novo do outcomes file automaticamente, sem
    precisar de nenhuma alteração.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from notificacoes import _enviar
from s2b_v2 import (
    fetch_todos_tickers,
    snapshot_completo,
    _carregar_json_github,
    _guardar_json_github,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("s2b_deteccao_rapida")

# =============================================================================
S2B_RAPIDA_VOLUME_MIN_USD = 250_000   # mesma elegibilidade do gatilho clássico
S2B_RAPIDA_PRECO_MIN_PCT  = 1.0       # testado 13/07/2026, ver docstring
S2B_RAPIDA_BUFFER_MAX     = 3         # só precisa da leitura de 5 min antes + margem
S2B_RAPIDA_JANELA_OBS_MIN = 1440      # 24h, mesma janela do resto do sistema

HISTORICO_RAPIDA_PATH   = "s2b_historico_5min.json"
HISTORICO_CLASSICO_PATH = "s2b_historico.json"   # só leitura, nunca escrito por este script
OUTCOMES_PATH            = "s2b_outcomes_v2.json"  # partilhado com os outros dois mecanismos

TIPO_GATILHO = "deteccao_rapida_5min"


def _eh_sintetico(symbol: str) -> bool:
    return "STOCK" in symbol.upper()


def _enviar_alerta_rapida(disparos: list[dict], agora: datetime) -> None:
    hora = agora.strftime("%H:%M")
    linhas = [f"⚡ <b>S2b detecção rápida (5 min) — {hora} UTC</b>", ""]
    for d in disparos:
        seta = "🟢 LONG" if d["direccao"] == "LONG" else "🔴 SHORT"
        linhas.append(f"• <b>{d['symbol']}</b> {seta} — preço {d['var_preco']:+.2f}% (vs 5min antes)")
    _enviar("\n".join(linhas))


def scan_rapido() -> None:
    agora = datetime.now(timezone.utc)
    log.info("=" * 50)
    log.info("S2b detecção rápida (5 min) — %s", agora.isoformat())

    tickers = fetch_todos_tickers()
    if not tickers:
        log.error("Falha a obter tickers da MEXC — scan abortado")
        return

    historico_rapida, sha_rapida = _carregar_json_github(HISTORICO_RAPIDA_PATH, {})
    historico_classico, _ = _carregar_json_github(HISTORICO_CLASSICO_PATH, {})
    outcomes, sha_outcomes = _carregar_json_github(OUTCOMES_PATH, [])

    novos_disparos: list[dict] = []
    alterado_hist = False
    alterado_out = False

    # ── 1) Elegibilidade + gatilho, para todos os tokens ────────────────────
    for symbol, ticker in tickers.items():
        if _eh_sintetico(symbol):
            continue
        volume = float(ticker.get("volume24", 0))
        if volume < S2B_RAPIDA_VOLUME_MIN_USD:
            continue
        preco = float(ticker.get("lastPrice", 0))
        if preco <= 0:
            continue

        registo = historico_rapida.setdefault(symbol, {"precos": [], "em_observacao": False})

        if registo.get("em_observacao"):
            continue  # já em observação por este mecanismo, não actualiza buffer nem verifica gatilho

        # já em observação pelo mecanismo clássico? (só leitura, nunca escreve lá)
        if historico_classico.get(symbol, {}).get("em_observacao"):
            continue

        precos_ant = registo.get("precos", [])

        if precos_ant and precos_ant[-1]:
            var_preco = (preco - precos_ant[-1]) / precos_ant[-1] * 100
            if abs(var_preco) >= S2B_RAPIDA_PRECO_MIN_PCT:
                direccao = "LONG" if var_preco > 0 else "SHORT"
                snap = snapshot_completo(symbol, direccao, ticker)
                registo["em_observacao"] = True
                alterado_hist = True

                outcomes.append({
                    "symbol":             symbol,
                    "direccao":           direccao,
                    "preco_entrada":      preco,
                    "volume_entrada":     volume,
                    "var_preco_gatilho":  var_preco,
                    "var_volume_gatilho": None,  # este mecanismo não usa volume
                    "tipo_gatilho":       TIPO_GATILHO,
                    "sinais_lancamento":  snap,
                    "buffer_pre_gatilho": {"precos": list(precos_ant)},
                    "timestamp_entrada":  agora.isoformat(),
                    "checkpoints":        {},
                    "completo":           False,
                })
                novos_disparos.append({"symbol": symbol, "direccao": direccao, "var_preco": var_preco})
                alterado_out = True
                log.info("[%s] Detecção rápida DISPAROU %s | preço %+.2f%% (vs 5min antes)",
                          symbol, direccao, var_preco)

        precos_ant.append(preco)
        if len(precos_ant) > S2B_RAPIDA_BUFFER_MAX:
            del precos_ant[: len(precos_ant) - S2B_RAPIDA_BUFFER_MAX]
        registo["precos"] = precos_ant
        alterado_hist = True

    # ── 2) Libertar em_observacao própria às 24h ─────────────────────────────
    # (checkpoints e "completo" já são tratados pelo scan_s2b() clássico,
    # que percorre TODO o outcomes file independentemente do tipo_gatilho)
    proprios_abertos = [o for o in outcomes if o.get("tipo_gatilho") == TIPO_GATILHO and not o.get("completo")]
    for o in proprios_abertos:
        try:
            entrada = datetime.fromisoformat(o["timestamp_entrada"])
        except Exception:
            continue
        minutos = (agora - entrada).total_seconds() / 60
        if minutos >= S2B_RAPIDA_JANELA_OBS_MIN:
            symbol = o["symbol"]
            if symbol in historico_rapida and historico_rapida[symbol].get("em_observacao"):
                historico_rapida[symbol]["em_observacao"] = False
                alterado_hist = True
                log.info("[%s] Detecção rápida — libertado após 24h", symbol)

    # ── 3) Notificar + gravar ────────────────────────────────────────────────
    if novos_disparos:
        _enviar_alerta_rapida(novos_disparos, agora)

    if alterado_hist:
        _guardar_json_github(HISTORICO_RAPIDA_PATH, historico_rapida, sha_rapida,
                              f"S2b rápida: histórico {agora.strftime('%Y-%m-%dT%H:%M')}")
    if alterado_out:
        _guardar_json_github(OUTCOMES_PATH, outcomes, sha_outcomes,
                              f"S2b rápida: {len(novos_disparos)} sinal(is) novo(s) {agora.strftime('%Y-%m-%dT%H:%M')}")

    log.info("S2b detecção rápida concluída — %d disparo(s) novo(s), %d tickers avaliados",
              len(novos_disparos), len(tickers))


if __name__ == "__main__":
    scan_rapido()
