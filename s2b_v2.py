"""
s2b_v2.py — S2b independente, substitui scan_leve/scan_pesado por completo.

Decisão 05/07/2026 (Malaquias): os scans clássicos (leve/pesado) pararam de
dar sinais úteis com o mercado neste regime ("não estou a conseguir ter
sinais, porque o mercado está baixo"). O S2b passa a ser o ÚNICO mecanismo
activo — scan próprio, correndo aos minutos :00 e :30 de cada hora.
scan_leve e scan_pesado, e tudo o que só existia para os servir (pipeline
clássico E1-E5, heatmap em E3, leverage em E4, filtro de BTC, universo
automático), ficam congelados — não são chamados a partir daqui. As bases
de Notion clássicas (Detecções/Moves/Scans/Tokens) e o bónus E2/E3 do CSA
ficam a apontar para o último estado antes do freeze, aceite
conscientemente (decisão do Malaquias, não mexer no CSA agora).

CADÊNCIA — MUDADA 07/07/2026 (segunda vez): primeiro corria aos :15/:45
(herdado do scan de breakout antigo), depois mudou para :00/:30 (alinhado
com as velas de 30 min da MEXC). Agora passa a **:00/:15/:30/:45 — a cada
15 minutos**, depois de se confirmar com dados reais (8 casos reais,
velas Min15) que isto dá, na maioria deles, alguns minutos de avanço na
detecção — e nalguns casos (YFI_USDT, OG_USDT) apanha sinais que a
cadência de 30 min falhava por completo dentro da mesma janela. Motivo
original: Malaquias reparou que os alertas chegavam "a meio da subida"
(caso concreto: US_USDT e BLUR_USDT já tinham feito 59-64% do movimento
total antes do alerta disparar) — ver conversa de 07/07/2026 para o
histórico completo desta investigação.

ELEGIBILIDADE: volume 24h > S2B_VOLUME_MIN_USD, lido directamente do
ticker bulk da MEXC — não depende do universo antigo (state.json).

GATILHO: preço actual vs a leitura de 15 min antes (a execução anterior
deste mesmo processo) E volume actual vs a média das últimas
S2B_JANELA_TRAILING leituras (1h) — sem filtro de BTC.

Porque a comparação de volume usa uma janela mais larga que a de preço:
calibração de 05/07/2026 mostrou que exigir os dois na MESMA vela falha no
ALLO_USDT (caso real já catalogado) — o preço reage rápido, o volume
confirma mais devagar. Comparar o volume com uma média mais larga resolve
isto, mesmo princípio do S2b original (janela de 6 candles Min60).

Threshold RECALIBRADO 07/07/2026 para a cadência de 15 min: preço>=2.0%
(antes 3.0% a 30 min — ajustado para baixo porque uma janela de 15 min vê
naturalmente metade do movimento de uma janela de 30 min para a mesma
tendência) E volume>=60% (mantido) vs a média das últimas 4 leituras de
15 min (1h, antes eram 6 leituras de 30 min = 3h). Testado contra ~9500
observações de ruído (40 tokens, ~60h, velas Min15) e o histórico real do
ALLO_USDT a Min15: ruído 1.31%, ALLO apanhado 21x — estatisticamente
equivalente à versão de 30 min que substituiu (ruído 1.30%, ALLO 21x),
mas com detecção mais rápida na maioria dos casos reais testados.

AO DISPARAR: alerta Telegram + o token entra em "observação" — estado
próprio deste ficheiro (campo em_observacao em s2b_historico.json),
completamente separado do "estado" clássico E1-E5. Enquanto em
observação, fica de fora de novos gatilhos (evita duplicar alertas).

EM OBSERVAÇÃO (24h, a cada execução — 15 em 15 min nativos, sem precisar
de reconstruir via velas históricas, porque o próprio scan já corre a
essa cadência): grava preço, volume, e os valores brutos dos 6 sinais
clássicos (S1-S6) + ATR% + RSI(14) — SÓ VALORES, sem ok/não-ok. A decisão
de sucesso/insucesso fica para a análise estatística feita depois com os
dados acumulados, não se grava agora (mesma filosofia "dados primeiro,
thresholds depois" já usada no resto do CFI). Checkpoints agora a cada 15
min (96 pontos em 24h, antes 48) — vem "de graça" da nova cadência, mais
densidade de dados sem custo extra de execuções.

FICHEIROS (no repo, mesmo mecanismo de commit do state.json antigo):
  s2b_historico.json  — buffers rolantes de preço/volume por token
                        elegível + flag em_observacao. Fonte de verdade
                        do gatilho.
  s2b_outcomes_v2.json — um registo por sinal disparado, com o buffer
                        pré-gatilho (permanente, não se perde) e os
                        checkpoints de 15 min durante as 24h de observação.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from config import GITHUB_REPO, GITHUB_TOKEN, MEXC_BASE_URL
from notificacoes import _enviar
from signals import (
    DadosMEXC, DadosCoinglass,
    calcular_rsi,
    _s1_long, _s1_short, _s2, _s3, _s4, _s5, _s6,
)

log = logging.getLogger("s2b_v2")

# =============================================================================
# CONFIGURAÇÃO — recalibrado 07/07/2026 para cadência de 15 min (ver docstring)
# =============================================================================
S2B_VOLUME_MIN_USD        = 250_000
S2B_PRECO_MIN_PCT         = 2.0    # variação vs a leitura de 15 min antes
S2B_VOLUME_MIN_PCT        = 60.0   # variação vs a média das últimas S2B_JANELA_TRAILING leituras
S2B_JANELA_TRAILING       = 4      # nº de leituras anteriores para a média de volume (1h)
S2B_BUFFER_MAX            = 8      # leituras guardadas por token (>= janela + margem, 2h)
S2B_JANELA_OBSERVACAO_MIN = 1440   # 24h
S2B_CHECKPOINT_MIN        = 15

HISTORICO_PATH = "s2b_historico.json"
OUTCOMES_PATH  = "s2b_outcomes_v2.json"
# CORREÇÃO 05/07/2026: usámos "s2b_outcomes.json" (o ficheiro antigo) até
# esta versão — mas esse ficheiro já tinha 204 registos do sistema anterior
# (schema diferente: "precos"/"sinais_evolucao" em vez de "checkpoints"),
# 119 deles ainda com completo=false. O código tentava processá-los como
# se fossem do schema novo e rebentava com KeyError: 'checkpoints' — foi
# a causa dos e-mails de falha nas primeiras execuções em produção.
# Ficheiro novo e dedicado, para nunca misturar com o histórico antigo
# (que fica congelado, tal como o resto do pipeline clássico).


# =============================================================================
# GITHUB — mesmo padrão já usado no resto do repo
# =============================================================================

def _com_retry_transiente(func, tentativas: int = 3, espera_inicial: float = 2.0):
    """
    Repete func() em caso de erro transitório (429 rate limit, 5xx do lado
    do servidor) — introduzido 09/07/2026 depois de um scan inteiro ter
    rebentado por um único 429 isolado no download_url. Espera crescente
    entre tentativas (2s, 4s, 8s...). Erros que não sejam destes códigos
    propagam-se logo, sem repetir (não faz sentido repetir um 404, por
    exemplo).
    """
    ultimo_erro = None
    for tentativa in range(tentativas):
        try:
            return func()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            ultimo_erro = e
            if tentativa < tentativas - 1:
                log.warning(f"HTTP {e.code} (tentativa {tentativa+1}/{tentativas}) — a esperar e a repetir")
                time.sleep(espera_inicial * (2 ** tentativa))
    raise ultimo_erro


def _github_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    def _fazer():
        url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        data = json.dumps(payload).encode() if payload else None
        req  = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "andreya-v2-s2b",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    return _com_retry_transiente(_fazer)


def _carregar_json_github(path: str, default: Any) -> tuple[Any, Optional[str]]:
    """
    CORREÇÃO 08/07/2026: a API "Contents" do GitHub só devolve o campo
    "content" (base64) para ficheiros até 1MB. Acima disso, devolve só
    metadados (incluindo sempre "sha", que continua a ser preciso para a
    escrita seguinte) e um "download_url" para o conteúdo real. Sem esta
    distinção, s2b_outcomes_v2.json (que ultrapassou 1MB) fazia o scan
    inteiro rebentar com JSONDecodeError em todas as execuções.
    """
    try:
        resp = _github_request("GET", path)
        sha = resp["sha"]
        conteudo_b64 = resp.get("content")
        if conteudo_b64:
            conteudo = base64.b64decode(conteudo_b64).decode()
        else:
            # Ficheiro > 1MB — ir buscar via download_url (conteúdo bruto,
            # sem auth). CORREÇÃO 09/07/2026: com retry — este pedido já
            # rebentou uma vez com 429 (rate limit), derrubando o scan
            # inteiro por causa de uma única falha transitória.
            download_url = resp.get("download_url")
            if not download_url:
                raise RuntimeError(f"GitHub não devolveu 'content' nem 'download_url' para {path}")
            def _fetch():
                with urllib.request.urlopen(download_url, timeout=30) as r:
                    return r.read().decode()
            conteudo = _com_retry_transiente(_fetch)
        return json.loads(conteudo), sha
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return default, None
        raise


def _guardar_json_github(path: str, dados: Any, sha: Optional[str], mensagem: str) -> None:
    """
    CORREÇÃO 09/07/2026: em 409 Conflict (sha desactualizado — provável
    sobreposição entre duas execuções, mais fácil de acontecer agora que o
    scan corre a cada 15 min e uma execução pode ainda estar a terminar
    quando a seguinte começa), busca o sha actual e tenta escrever outra
    vez, até 3 vezes, antes de desistir. Sem isto, um 409 isolado derrubava
    o scan inteiro a meio — já aconteceu (ver histórico de 09/07/2026).
    """
    conteudo_b64 = base64.b64encode(
        json.dumps(dados, indent=2, ensure_ascii=False).encode()
    ).decode()
    payload = {"message": mensagem, "content": conteudo_b64}
    if sha:
        payload["sha"] = sha

    for tentativa in range(3):
        try:
            _github_request("PUT", path, payload)
            return
        except urllib.error.HTTPError as e:
            if e.code != 409 or tentativa == 2:
                raise
            log.warning(f"{path}: 409 Conflict (tentativa {tentativa+1}/3) — a buscar sha actual e a repetir")
            _, sha_novo = _carregar_json_github(path, None)
            payload["sha"] = sha_novo
            time.sleep(1)


# =============================================================================
# MEXC
# =============================================================================

def _mexc_get(endpoint: str, params: Optional[dict] = None) -> Optional[Any]:
    url = f"{MEXC_BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "andreya-v2-s2b", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if resp.get("success", True) is False:
                log.warning(f"MEXC API erro em {endpoint}: {resp.get('message')}")
                return None
            return resp.get("data", resp)
    except Exception as e:
        log.error(f"MEXC GET {endpoint} falhou: {e}")
        return None


def fetch_todos_tickers() -> dict[str, dict]:
    dados = _mexc_get("/contract/ticker")
    if not dados:
        return {}
    if isinstance(dados, list):
        return {t["symbol"]: t for t in dados if "_USDT" in t.get("symbol", "")}
    return {}


def fetch_candles(symbol: str, intervalo: str, count: int) -> list[dict]:
    dados = _mexc_get(f"/contract/kline/{symbol}", {"interval": intervalo, "count": count})
    if not dados or "time" not in dados:
        return []
    n = min(len(dados["time"]), len(dados["close"]))
    return [
        {
            "timestamp": dados["time"][i],
            "open":      float(dados["open"][i]),
            "high":      float(dados["high"][i]),
            "low":       float(dados["low"][i]),
            "close":     float(dados["close"][i]),
            "volume":    float(dados["vol"][i]),
        }
        for i in range(n)
    ]


# =============================================================================
# CONSTRUÇÃO DE DADOS PARA OS SINAIS — versão simplificada (sem OI histórico
# próprio; S2 fica sempre com base 0, informativo apenas, tal como já era
# tratado como não-gatilho no S2b original)
# =============================================================================

def calcular_atr_pct(candles: list[dict], periodo: int = 14) -> float:
    if len(candles) < periodo + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-periodo:]) / periodo
    preco_actual = candles[-1]["close"]
    return (atr / preco_actual * 100) if preco_actual else 0.0


def calcular_volume_media_7d(candles: list[dict]) -> float:
    if len(candles) < 24:
        return sum(c["volume"] for c in candles) if candles else 0.0
    ultimos = candles[-168:]
    n_dias  = max(1, len(ultimos) // 24)
    total   = sum(c["volume"] for c in ultimos)
    return total / n_dias


def construir_dados_mexc(symbol: str, ticker: dict, candles_1h: list[dict]) -> tuple[DadosMEXC, DadosCoinglass, float]:
    preco_actual  = float(ticker.get("lastPrice", 0))
    volume_24h    = float(ticker.get("volume24", 0))
    vol_media_7d  = calcular_volume_media_7d(candles_1h)
    atr_pct       = calcular_atr_pct(candles_1h)
    funding_rate  = float(ticker.get("fundingRate", 0))

    mexc_d = DadosMEXC(
        ticker=symbol,
        preco_actual=preco_actual,
        preco_change_24h_pct=float(ticker.get("riseFallRate", 0)),
        volume_24h=volume_24h,
        volume_media_7d=vol_media_7d,
        high_24h=float(ticker.get("high24Price", preco_actual)),
        low_24h=float(ticker.get("lower24Price", preco_actual)),
        atr_1h=atr_pct,
        candles_1h=candles_1h,
    )
    # L/S ratio: proxy pelo funding, mesmo mecanismo já usado no resto do CFI
    # (endpoint real de L/S da MEXC devolve 404 — ver notas antigas do projecto)
    if funding_rate < -0.00005:
        ls_ratio = 0.5
    elif funding_rate > 0.00005:
        ls_ratio = 1.5
    else:
        ls_ratio = 1.0
    cg_d = DadosCoinglass(
        ticker=symbol,
        oi_change_24h_pct=0.0,  # sem baseline histórica própria neste processo — informativo, nunca gatilho
        funding_rate=funding_rate,
        ls_ratio=ls_ratio,
    )
    return mexc_d, cg_d, atr_pct


def _valores_sinais(mexc: DadosMEXC, coinglass: DadosCoinglass, direccao: str) -> dict:
    """
    Valores brutos dos 6 sinais clássicos — SEM ok/não-ok. A decisão de
    threshold fica para a análise estatística feita depois com os dados
    acumulados, não se grava agora. Pedido de Malaquias, 05/07/2026.
    """
    if direccao == "LONG":
        _, s1_valor = _s1_long(mexc)
    else:
        _, s1_valor = _s1_short(mexc)
    _, s2_oi, s2_preco, s2_vol           = _s2(mexc, coinglass, direccao)
    _, s3_funding                        = _s3(coinglass)
    _, s4_range                          = _s4(mexc)
    _, s5_ema9, s5_ema21, s5_estrutura   = _s5(mexc, direccao)
    _, s6_ls, s6_preco                   = _s6(mexc, coinglass, direccao)
    return {
        "s1_valor":      s1_valor,
        "s2_oi_pct":     s2_oi,
        "s2_preco_pct":  s2_preco,
        "s2_vol_dir_pct": s2_vol,
        "s3_funding":    s3_funding,
        "s4_range_pct":  s4_range,
        "s5_ema9":       s5_ema9,
        "s5_ema21":      s5_ema21,
        "s5_estrutura":  s5_estrutura,
        "s6_ls_ratio":   s6_ls,
        "s6_preco_pct":  s6_preco,
    }


def snapshot_completo(symbol: str, direccao: str, ticker: dict) -> Optional[dict]:
    """
    Preço, volume, os 6 "S" (valores) + ATR% + RSI(14) — lista exacta
    acordada com o Malaquias em 05/07/2026. Usado tanto no momento do
    gatilho como em cada checkpoint de 15 min durante a observação.
    """
    candles_1h = fetch_candles(symbol, "Min60", 30)
    if len(candles_1h) < 25:
        return None
    mexc_d, cg_d, atr_pct = construir_dados_mexc(symbol, ticker, candles_1h)
    valores = _valores_sinais(mexc_d, cg_d, direccao)
    closes  = [c["close"] for c in candles_1h]
    rsi14   = calcular_rsi(closes, 14)
    return {
        "preco":  float(ticker.get("lastPrice", 0)),
        "volume": float(ticker.get("volume24", 0)),
        **valores,
        "atr_pct": atr_pct,
        "rsi14":   rsi14,
    }


# =============================================================================
# TELEGRAM
# =============================================================================

def _enviar_alerta_s2b(disparos: list[dict], agora: datetime) -> None:
    hora = agora.strftime("%H:%M")
    linhas = [f"⚡ <b>S2b (15 min) — {hora} UTC</b>", ""]
    for d in disparos:
        seta = "🟢 LONG" if d["direccao"] == "LONG" else "🔴 SHORT"
        linhas.append(
            f"• <b>{d['symbol']}</b> {seta} — preço {d['var_preco']:+.1f}% / "
            f"volume {d['var_volume']:+.0f}% (vs média 1h)"
        )
    _enviar("\n".join(linhas))


# =============================================================================
# PROCESSO PRINCIPAL
# =============================================================================

def scan_s2b() -> None:
    agora = datetime.now(timezone.utc)
    log.info(f"=== S2B {agora.strftime('%Y-%m-%dT%H:%M')} ===")

    tickers = fetch_todos_tickers()
    if not tickers:
        log.error("Falha a obter tickers da MEXC — scan abortado")
        return

    historico, sha_hist = _carregar_json_github(HISTORICO_PATH, {})
    outcomes,  sha_out  = _carregar_json_github(OUTCOMES_PATH, [])

    alterado_hist  = False
    alterado_out   = False
    novos_disparos = []

    # ── 1) Elegibilidade + gatilho, para todos os tokens ainda não em observação ──
    for symbol, ticker in tickers.items():
        volume = float(ticker.get("volume24", 0))
        if volume < S2B_VOLUME_MIN_USD:
            continue
        preco = float(ticker.get("lastPrice", 0))
        if preco <= 0:
            continue

        registo = historico.setdefault(symbol, {"precos": [], "volumes": [], "em_observacao": False})
        if registo.get("em_observacao"):
            continue  # já disparou, tratado na secção 2

        precos_ant = registo["precos"]
        vols_ant   = registo["volumes"]

        if len(precos_ant) >= 1 and precos_ant[-1] and len(vols_ant) >= S2B_JANELA_TRAILING:
            var_preco  = (preco - precos_ant[-1]) / precos_ant[-1] * 100
            media_vol  = sum(vols_ant[-S2B_JANELA_TRAILING:]) / S2B_JANELA_TRAILING
            var_volume = ((volume - media_vol) / media_vol * 100) if media_vol else 0.0

            if abs(var_preco) >= S2B_PRECO_MIN_PCT and var_volume >= S2B_VOLUME_MIN_PCT:
                direccao = "LONG" if var_preco > 0 else "SHORT"
                snap = snapshot_completo(symbol, direccao, ticker)
                registo["em_observacao"] = True
                outcomes.append({
                    "symbol":            symbol,
                    "direccao":          direccao,
                    "preco_entrada":     preco,
                    "volume_entrada":    volume,
                    "var_preco_gatilho": var_preco,
                    "var_volume_gatilho": var_volume,
                    "sinais_lancamento": snap,   # None se as velas falharem — não bloqueia o alerta
                    # Cópia permanente do histórico de 15 em 15 min ANTES do
                    # gatilho — sem isto, o buffer ao vivo (s2b_historico.json)
                    # continua a andar para a frente depois das 24h e perde-se
                    # a forma como o preço/volume se comportou antes de
                    # disparar. Guardado aqui, fica preso a este sinal para
                    # sempre. Decisão 07/07/2026.
                    "buffer_pre_gatilho": {
                        "precos":  list(precos_ant),
                        "volumes": list(vols_ant),
                    },
                    "timestamp_entrada": agora.isoformat(),
                    "checkpoints":       {},
                    "completo":          False,
                })
                novos_disparos.append({
                    "symbol": symbol, "direccao": direccao,
                    "var_preco": var_preco, "var_volume": var_volume,
                })
                alterado_out = True
                log.info(f"[{symbol}] S2b DISPAROU {direccao} | preço {var_preco:+.1f}% | volume {var_volume:+.0f}%")

        # actualizar buffers sempre, tenha ou não disparado
        precos_ant.append(preco)
        vols_ant.append(volume)
        if len(precos_ant) > S2B_BUFFER_MAX:
            del precos_ant[: len(precos_ant) - S2B_BUFFER_MAX]
            del vols_ant[: len(vols_ant) - S2B_BUFFER_MAX]
        alterado_hist = True

    # ── 2) Checkpoints dos que já estão em observação ──
    abertos = [o for o in outcomes if not o.get("completo")]
    for o in abertos:
        symbol = o["symbol"]
        ticker = tickers.get(symbol)
        if not ticker:
            continue  # símbolo pode ter sido deslistado; tenta-se na próxima
        try:
            entrada = datetime.fromisoformat(o["timestamp_entrada"])
        except Exception:
            continue
        minutos = int((agora - entrada).total_seconds() // 60)
        minutos = min(minutos, S2B_JANELA_OBSERVACAO_MIN)
        # o scan já corre nativamente a cada 15 min — arredonda só para
        # absorver pequenas derivas do agendamento (segundos, não minutos)
        checkpoint = round(minutos / S2B_CHECKPOINT_MIN) * S2B_CHECKPOINT_MIN
        chave = str(checkpoint)
        checkpoints = o.setdefault("checkpoints", {})  # nunca rebentar por registo com schema inesperado

        if checkpoint > 0 and chave not in checkpoints:
            snap = snapshot_completo(symbol, o["direccao"], ticker)
            if snap is not None:
                checkpoints[chave] = snap
                alterado_out = True

        if minutos >= S2B_JANELA_OBSERVACAO_MIN and not o.get("completo"):
            o["completo"] = True
            alterado_out = True
            if symbol in historico:
                historico[symbol]["em_observacao"] = False  # liberta para poder voltar a disparar
                alterado_hist = True
            log.info(f"[{symbol}] S2b — observação de 24h completa ({len(checkpoints)} checkpoints)")

    # ── 3) Notificar + gravar ──
    if novos_disparos:
        _enviar_alerta_s2b(novos_disparos, agora)

    if alterado_hist:
        _guardar_json_github(HISTORICO_PATH, historico, sha_hist, f"S2b: histórico {agora.strftime('%Y-%m-%dT%H:%M')}")
    if alterado_out:
        _guardar_json_github(OUTCOMES_PATH, outcomes, sha_out, f"S2b: outcomes {agora.strftime('%Y-%m-%dT%H:%M')}")

    log.info(
        f"S2b concluído — {len(novos_disparos)} disparo(s) novo(s), "
        f"{len(abertos)} em observação, {len(tickers)} tickers avaliados"
    )


if __name__ == "__main__":
    scan_s2b()
