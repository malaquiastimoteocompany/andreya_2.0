#!/usr/bin/env python3
"""
S2b — Módulo de Execução (Paper Mode)
=======================================

Serviço persistente (Railway, não GitHub Actions) que:
1. Vigia o s2b_outcomes_v2.json no GitHub e apanha sinais novos (sem 'paper_trailing')
   dentro da janela de sincronização (default 24h desde o timestamp_entrada).
2. Para cada sinal apanhado, abre uma posição paper e gere um trailing stop
   (activação 1×ATR / distância 2×ATR / SL inicial 5×ATR — manual S2b Secção 6.2).
3. Monitoriza preço a cada POLL_SECONDS (default 60s) via MEXC contract ticker.
4. Ao fechar (SL atingido ou timeout 24h), escreve o resultado de volta ao
   registo correspondente no GitHub, campo 'paper_trailing'.

Não coloca ordens reais. Não toca em 'checkpoints' nem 'buffer_pre_gatilho'
(esses são geridos pelo s2b_v2.py). Só lê os campos de entrada e escreve um
campo novo, isolado.

Variáveis de ambiente:
  GITHUB_TOKEN       — obrigatório, fine-grained PAT com Contents: Read and write no repo
  GITHUB_REPO        — default 'malaquiastimoteocompany/andreya_2.0'
  GITHUB_BRANCH      — default 'main'
  OUTCOMES_PATH      — default 's2b_outcomes_v2.json'
  DB_PATH            — default '/data/s2b_execucao_paper.db'  (volume Railway)
  POLL_SECONDS        — default 60
  JANELA_SYNC_HORAS  — default 24  (só apanha sinais com entrada dentro desta janela)
  FILTRAR_SINTETICOS — default '1' (ignora símbolos que contenham 'STOCK', ex: *STOCK_USDT)
  MEXC_TICKER_URL    — default 'https://contract.mexc.com/api/v1/contract/ticker'
"""

import os
import sys
import time
import json
import base64
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "malaquiastimoteocompany/andreya_2.0")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
OUTCOMES_PATH = os.environ.get("OUTCOMES_PATH", "s2b_outcomes_v2.json")
DB_PATH = os.environ.get("DB_PATH", "/data/s2b_execucao_paper.db")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
JANELA_SYNC_HORAS = float(os.environ.get("JANELA_SYNC_HORAS", "24"))
FILTRAR_SINTETICOS = os.environ.get("FILTRAR_SINTETICOS", "1") == "1"
MEXC_TICKER_URL = os.environ.get("MEXC_TICKER_URL", "https://contract.mexc.com/api/v1/contract/ticker")

# Mecanismo de trailing — manual S2b Secção 6.2, validado contra 34 sinais reais.
# ATR_DISTANCIA_TRAILING actualizado de 2.0 -> 1.5 em 11/07/2026, depois de
# testar 1.0x-6.0x contra os 50 sinais completos disponíveis nessa altura:
# padrão limpo e quase monótono, 1.5x deu o melhor PnL médio (+2.83% vs +2.49%
# a 2x) e o melhor total ($1.41 vs $1.24 a $1/trade). Mesma amostra usada para
# validar o resto do mecanismo — a confirmar com dados novos do paper mode.
ATR_ACTIVACAO = 1.0
ATR_DISTANCIA_TRAILING = 1.5
ATR_SL_INICIAL = 5.0
TIMEOUT_HORAS = 24.0

GITHUB_API = "https://api.github.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("s2b_execucao_paper")


def _headers():
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN não definido no ambiente.")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


# --------------------------------------------------------------------------
# GitHub — leitura e escrita via Git Data API
# (evita a limitação de ~1MB da Contents API para leitura; usa blob+tree+commit
#  para escrita, com retry em conflitos de ref, tal como já feito no scan a 15min)
# --------------------------------------------------------------------------

def gh_get_file_sha(path: str) -> str:
    """Devolve o sha (blob) actual do ficheiro no branch configurado."""
    r = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}",
        headers=_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["sha"]


def gh_get_blob(sha: str) -> bytes:
    r = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/git/blobs/{sha}",
        headers=_headers(),
        timeout=60,
    )
    r.raise_for_status()
    d = r.json()
    return base64.b64decode(d["content"])


def ler_outcomes() -> list:
    sha = gh_get_file_sha(OUTCOMES_PATH)
    raw = gh_get_blob(sha)
    return json.loads(raw)


def escrever_outcomes(dados: list, mensagem: str, tentativas: int = 5) -> None:
    """Commit do ficheiro completo via Git Data API, com retry em 409/422
    (conflito de ref — outro processo, ex: o scan de detecção, comitou entretanto)."""
    novo_conteudo = json.dumps(dados, ensure_ascii=False, indent=None).encode("utf-8")

    for tentativa in range(1, tentativas + 1):
        try:
            ref = requests.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs/heads/{GITHUB_BRANCH}",
                headers=_headers(), timeout=30,
            )
            ref.raise_for_status()
            commit_sha = ref.json()["object"]["sha"]

            commit = requests.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/commits/{commit_sha}",
                headers=_headers(), timeout=30,
            )
            commit.raise_for_status()
            base_tree_sha = commit.json()["tree"]["sha"]

            blob = requests.post(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/blobs",
                headers=_headers(), timeout=60,
                json={"content": base64.b64encode(novo_conteudo).decode(), "encoding": "base64"},
            )
            blob.raise_for_status()
            novo_blob_sha = blob.json()["sha"]

            tree = requests.post(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/trees",
                headers=_headers(), timeout=30,
                json={
                    "base_tree": base_tree_sha,
                    "tree": [{
                        "path": OUTCOMES_PATH,
                        "mode": "100644",
                        "type": "blob",
                        "sha": novo_blob_sha,
                    }],
                },
            )
            tree.raise_for_status()
            novo_tree_sha = tree.json()["sha"]

            novo_commit = requests.post(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/commits",
                headers=_headers(), timeout=30,
                json={
                    "message": mensagem,
                    "tree": novo_tree_sha,
                    "parents": [commit_sha],
                },
            )
            novo_commit.raise_for_status()
            novo_commit_sha = novo_commit.json()["sha"]

            update_ref = requests.patch(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/git/refs/heads/{GITHUB_BRANCH}",
                headers=_headers(), timeout=30,
                json={"sha": novo_commit_sha, "force": False},
            )
            if update_ref.status_code in (409, 422):
                raise requests.HTTPError(f"conflito de ref ({update_ref.status_code})")
            update_ref.raise_for_status()

            log.info("Commit escrito em %s: %s", OUTCOMES_PATH, mensagem)
            return

        except requests.HTTPError as e:
            espera = min(2 ** tentativa, 30)
            log.warning("Falha ao escrever no GitHub (tentativa %d/%d): %s — a esperar %ds",
                        tentativa, tentativas, e, espera)
            time.sleep(espera)

    raise RuntimeError(f"Não foi possível escrever '{OUTCOMES_PATH}' após {tentativas} tentativas.")


# --------------------------------------------------------------------------
# MEXC — preços actuais (um pedido por ciclo para todos os símbolos)
# --------------------------------------------------------------------------

def obter_precos_actuais() -> dict:
    """Devolve {symbol: lastPrice} para todos os contratos MEXC."""
    r = requests.get(MEXC_TICKER_URL, timeout=30)
    r.raise_for_status()
    payload = r.json()
    dados = payload.get("data", payload) if isinstance(payload, dict) else payload
    precos = {}
    for item in dados:
        symbol = item.get("symbol")
        preco = item.get("lastPrice")
        if symbol and preco is not None:
            precos[symbol] = float(preco)
    return precos


# --------------------------------------------------------------------------
# SQLite — estado local das posições paper (durante a vida da posição)
# --------------------------------------------------------------------------

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS posicoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp_entrada TEXT NOT NULL,
            direccao TEXT NOT NULL,
            preco_entrada REAL NOT NULL,
            atr_pct REAL NOT NULL,
            atr_preco REAL NOT NULL,
            sl_inicial REAL NOT NULL,
            sl_atual REAL NOT NULL,
            extremo_favoravel REAL NOT NULL,
            activado INTEGER NOT NULL DEFAULT 0,
            activado_em TEXT,
            estado TEXT NOT NULL DEFAULT 'aberta',
            motivo_fecho TEXT,
            timestamp_fecho TEXT,
            preco_fecho REAL,
            pnl_pct REAL,
            UNIQUE(symbol, timestamp_entrada)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sl_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            posicao_id INTEGER NOT NULL REFERENCES posicoes(id),
            timestamp TEXT NOT NULL,
            preco REAL NOT NULL,
            sl REAL NOT NULL,
            activado INTEGER NOT NULL
        )
    """)
    con.commit()
    return con


# --------------------------------------------------------------------------
# Lógica de trailing — manual S2b Secção 6.2
# --------------------------------------------------------------------------

def eh_token_sintetico(symbol: str) -> bool:
    return "STOCK" in symbol.upper()


def calcular_sl_inicial(direccao: str, preco_entrada: float, atr_preco: float) -> float:
    if direccao == "LONG":
        return preco_entrada - ATR_SL_INICIAL * atr_preco
    return preco_entrada + ATR_SL_INICIAL * atr_preco


def atualizar_trailing(direccao: str, preco_entrada: float, atr_preco: float,
                        extremo_favoravel: float, sl_atual: float, activado: bool,
                        preco_atual: float):
    """Devolve (novo_extremo, novo_sl, novo_activado, deve_fechar, motivo)."""
    if direccao == "LONG":
        novo_extremo = max(extremo_favoravel, preco_atual)
        if not activado and preco_atual >= preco_entrada + ATR_ACTIVACAO * atr_preco:
            activado = True
        if activado:
            candidato_sl = novo_extremo - ATR_DISTANCIA_TRAILING * atr_preco
            novo_sl = max(sl_atual, candidato_sl)
        else:
            novo_sl = sl_atual
        deve_fechar = preco_atual <= novo_sl
    else:  # SHORT
        novo_extremo = min(extremo_favoravel, preco_atual)
        if not activado and preco_atual <= preco_entrada - ATR_ACTIVACAO * atr_preco:
            activado = True
        if activado:
            candidato_sl = novo_extremo + ATR_DISTANCIA_TRAILING * atr_preco
            novo_sl = min(sl_atual, candidato_sl)
        else:
            novo_sl = sl_atual
        deve_fechar = preco_atual >= novo_sl

    motivo = "sl_trailing" if deve_fechar else None
    return novo_extremo, novo_sl, activado, deve_fechar, motivo


def calcular_pnl_pct(direccao: str, preco_entrada: float, preco_fecho: float) -> float:
    if direccao == "LONG":
        return (preco_fecho - preco_entrada) / preco_entrada * 100
    return (preco_entrada - preco_fecho) / preco_entrada * 100


# --------------------------------------------------------------------------
# Sincronização de sinais novos (GitHub -> posições locais)
# --------------------------------------------------------------------------

def sincronizar_novos_sinais(con: sqlite3.Connection, outcomes: list):
    agora = datetime.now(timezone.utc)
    novos = 0

    for entrada in outcomes:
        if "paper_trailing" in entrada:
            continue

        symbol = entrada["symbol"]
        if FILTRAR_SINTETICOS and eh_token_sintetico(symbol):
            continue

        ts_entrada = datetime.fromisoformat(entrada["timestamp_entrada"])
        horas_decorridas = (agora - ts_entrada).total_seconds() / 3600
        if horas_decorridas < 0 or horas_decorridas > JANELA_SYNC_HORAS:
            continue

        direccao = entrada["direccao"]
        preco_entrada = float(entrada["preco_entrada"])
        atr_pct = float(entrada["sinais_lancamento"]["atr_pct"])
        atr_preco = preco_entrada * atr_pct / 100.0
        sl_inicial = calcular_sl_inicial(direccao, preco_entrada, atr_preco)

        try:
            con.execute(
                """INSERT INTO posicoes
                   (symbol, timestamp_entrada, direccao, preco_entrada, atr_pct, atr_preco,
                    sl_inicial, sl_atual, extremo_favoravel, activado, estado)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'aberta')""",
                (symbol, entrada["timestamp_entrada"], direccao, preco_entrada, atr_pct,
                 atr_preco, sl_inicial, sl_inicial, preco_entrada),
            )
            con.commit()
            novos += 1
            log.info("Nova posição paper: %s %s @ %.8f (ATR%%=%.3f, SL inicial=%.8f)",
                      symbol, direccao, preco_entrada, atr_pct, sl_inicial)
        except sqlite3.IntegrityError:
            pass  # já existe localmente (UNIQUE symbol+timestamp_entrada)

    if novos:
        log.info("Sincronização: %d posição(ões) nova(s) apanhada(s).", novos)


# --------------------------------------------------------------------------
# Actualização das posições abertas
# --------------------------------------------------------------------------

def atualizar_posicoes_abertas(con: sqlite3.Connection, precos: dict):
    agora = datetime.now(timezone.utc)
    cur = con.execute("SELECT * FROM posicoes WHERE estado = 'aberta'")
    colunas = [d[0] for d in cur.description]
    posicoes = [dict(zip(colunas, row)) for row in cur.fetchall()]

    fechadas = []

    for p in posicoes:
        preco_atual = precos.get(p["symbol"])
        if preco_atual is None:
            log.warning("Sem preço MEXC para %s neste ciclo — a saltar.", p["symbol"])
            continue

        ts_entrada = datetime.fromisoformat(p["timestamp_entrada"])
        horas_decorridas = (agora - ts_entrada).total_seconds() / 3600

        novo_extremo, novo_sl, activado, deve_fechar, motivo = atualizar_trailing(
            p["direccao"], p["preco_entrada"], p["atr_preco"],
            p["extremo_favoravel"], p["sl_atual"], bool(p["activado"]), preco_atual,
        )

        timeout = horas_decorridas >= TIMEOUT_HORAS
        if timeout and not deve_fechar:
            deve_fechar = True
            motivo = "timeout_24h"

        activado_em = p["activado_em"]
        if activado and not p["activado"]:
            activado_em = agora.isoformat()
            log.info("Trailing activado: %s @ %.8f", p["symbol"], preco_atual)

        con.execute(
            """UPDATE posicoes SET sl_atual=?, extremo_favoravel=?, activado=?, activado_em=?
               WHERE id=?""",
            (novo_sl, novo_extremo, int(activado), activado_em, p["id"]),
        )
        con.execute(
            "INSERT INTO sl_historico (posicao_id, timestamp, preco, sl, activado) VALUES (?, ?, ?, ?, ?)",
            (p["id"], agora.isoformat(), preco_atual, novo_sl, int(activado)),
        )

        if deve_fechar:
            pnl_pct = calcular_pnl_pct(p["direccao"], p["preco_entrada"], preco_atual)
            con.execute(
                """UPDATE posicoes SET estado='fechada', motivo_fecho=?, timestamp_fecho=?,
                   preco_fecho=?, pnl_pct=? WHERE id=?""",
                (motivo, agora.isoformat(), preco_atual, pnl_pct, p["id"]),
            )
            log.info("Posição fechada: %s %s motivo=%s preço=%.8f pnl=%.2f%%",
                      p["symbol"], p["direccao"], motivo, preco_atual, pnl_pct)
            fechadas.append(p["id"])

        con.commit()

    return fechadas


# --------------------------------------------------------------------------
# Escrita de posições fechadas de volta ao GitHub
# --------------------------------------------------------------------------

def escrever_fechadas_no_github(con: sqlite3.Connection, ids_fechadas: list):
    if not ids_fechadas:
        return

    outcomes = ler_outcomes()
    indice = {(e["symbol"], e["timestamp_entrada"]): i for i, e in enumerate(outcomes)}

    alteracoes = 0
    for pid in ids_fechadas:
        cur = con.execute("SELECT * FROM posicoes WHERE id=?", (pid,))
        colunas = [d[0] for d in cur.description]
        p = dict(zip(colunas, cur.fetchone()))

        chave = (p["symbol"], p["timestamp_entrada"])
        if chave not in indice:
            log.warning("Posição fechada localmente mas sinal já não existe no GitHub: %s", chave)
            continue

        hist_cur = con.execute(
            "SELECT timestamp, preco, sl, activado FROM sl_historico WHERE posicao_id=? ORDER BY timestamp",
            (pid,),
        )
        sl_historico = [
            {"timestamp": r[0], "preco": r[1], "sl": r[2], "activado": bool(r[3])}
            for r in hist_cur.fetchall()
        ]

        outcomes[indice[chave]]["paper_trailing"] = {
            "direccao": p["direccao"],
            "preco_entrada": p["preco_entrada"],
            "atr_pct": p["atr_pct"],
            "atr_preco": p["atr_preco"],
            "sl_inicial": p["sl_inicial"],
            "activado": bool(p["activado"]),
            "activado_em": p["activado_em"],
            "sl_historico": sl_historico,
            "fecho": {
                "motivo": p["motivo_fecho"],
                "timestamp": p["timestamp_fecho"],
                "preco": p["preco_fecho"],
                "pnl_pct": p["pnl_pct"],
            },
        }
        alteracoes += 1

    if alteracoes:
        escrever_outcomes(
            outcomes,
            f"paper_trailing: {alteracoes} posição(ões) fechada(s) — {datetime.now(timezone.utc).isoformat()}",
        )


# --------------------------------------------------------------------------
# Loop principal
# --------------------------------------------------------------------------

def ciclo(con: sqlite3.Connection):
    outcomes = ler_outcomes()
    sincronizar_novos_sinais(con, outcomes)

    precos = obter_precos_actuais()
    fechadas = atualizar_posicoes_abertas(con, precos)

    escrever_fechadas_no_github(con, fechadas)


def main():
    log.info("S2b execução paper — a arrancar. Repo=%s Branch=%s Poll=%ds",
              GITHUB_REPO, GITHUB_BRANCH, POLL_SECONDS)
    con = init_db()

    while True:
        inicio = time.time()
        try:
            ciclo(con)
        except Exception:
            log.exception("Erro no ciclo — a continuar no próximo.")

        duracao = time.time() - inicio
        espera = max(0, POLL_SECONDS - duracao)
        time.sleep(espera)


if __name__ == "__main__":
    main()
