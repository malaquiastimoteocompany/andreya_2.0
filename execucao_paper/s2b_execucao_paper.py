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
# CORREÇÃO 15/07/2026: antes um só OUTCOMES_PATH partilhado entre os três
# mecanismos de detecção — dois deles (clássico 15min + rápida 5min) a
# escrever no mesmo ficheiro com tanta frequência causou impasse de
# escrita (nenhum conseguia "ganhar a vez", ver README_regimes.json).
# Agora cada mecanismo de detecção tem o seu próprio ficheiro; este
# módulo lê e escreve nos dois, sabendo sempre de qual veio cada posição
# (campo origem_ficheiro na SQLite).
OUTCOMES_PATH_V2 = os.environ.get("OUTCOMES_PATH_V2", "s2b_outcomes_v2.json")
OUTCOMES_PATH_RAPIDA = os.environ.get("OUTCOMES_PATH_RAPIDA", "s2b_outcomes_rapida.json")
FECHOS_PATH = os.environ.get("FECHOS_PATH", "s2b_fechos_paper.json")  # já não usado directamente, mantido só de referência


def _caminho_fechos_hoje(agora: datetime) -> str:
    """
    CORREÇÃO 17/07/2026: s2b_fechos_paper.json cresceu para 39.9MB em só 2
    dias (470 fechos) e passou a falhar a escrever, mesmo com a API de
    dados do Git — mesmo padrão exacto do outcomes_v2.json de 15/07/2026.
    Passa a ser um ficheiro por dia (mesmo princípio já usado no arquivo
    de sinais completos do s2b_v2.py, e no CSA) — nunca mais cresce sem
    controlo. Migração única 17/07/2026: os 470 fechos existentes foram
    divididos por dia em s2b_fechos_paper_AAAA-MM-DD.json.
    """
    return f"s2b_fechos_paper_{agora.strftime('%Y-%m-%d')}.json"
# CORREÇÃO 15/07/2026 (nº3): resultados de fecho passam a viver aqui,
# separados dos outcomes files — ver docstring de escrever_fechadas_no_github.
# Para juntar sinal+resultado numa análise: indexar por (symbol,
# timestamp_entrada) nos dois lados.
DB_PATH = os.environ.get("DB_PATH", "/data/s2b_execucao_paper.db")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
EXPORT_INTERVAL_SECONDS = int(os.environ.get("EXPORT_INTERVAL_SECONDS", "180"))  # 3 min
_ultima_exportacao = 0.0  # inicializado a 0 -- primeira exportação acontece já no 1º ciclo com fechos pendentes
# CORREÇÃO 15/07/2026 (nº2): antes escrevia ao GitHub em todos os ciclos
# (60s) em que alguma posição fechasse — colidia com o scanner.yml (15 min)
# a escrever no mesmo s2b_outcomes_v2.json. Isto só afecta a CADÊNCIA da
# escrita ao GitHub — a lógica de trailing em si (quando activa, quando
# fecha) continua a correr a cada ciclo de 60s, sem alteração nenhuma; só
# o "reportar ao GitHub" passa a ser em lote, a cada 3 min.
JANELA_SYNC_HORAS = float(os.environ.get("JANELA_SYNC_HORAS", "24"))
FILTRAR_SINTETICOS = os.environ.get("FILTRAR_SINTETICOS", "1") == "1"

# CORREÇÃO 20/07/2026: fade — testado contra 267 trades reais (15-20/07,
# mercado calmo), aplicado uniformemente a todos (não só retrospectivamente
# aos que sabíamos que iam falhar): sem regra dava -$1.76 no total; fechar
# às 3h se ainda não tiver activado E abrir a posição invertida a partir
# daí deu +$0.52 — vira o sistema de prejuízo para lucro. Mecanismo: em
# mercado calmo/de range, um falso arranque tende a reverter, mesmo
# princípio que traders humanos usam para "fade" breakouts falhados.
# Ainda por validar fora da amostra (mesma amostra onde foi descoberto) —
# ver README_regimes.json para o momento zero desta funcionalidade.
FADE_ATIVO = os.environ.get("FADE_ATIVO", "1") == "1"
FADE_HORAS_LIMITE = float(os.environ.get("FADE_HORAS_LIMITE", "3.0"))

# Filtro de var_preco_gatilho — testado 13/07/2026 contra a Amostra Real (32
# trades reais, monitorização a 1min): sinais com |var_preco_gatilho| > 20%
# tiveram só 14.3% win rate (-8.02% médio) vs 60% win rate (+2.26% médio)
# dentro do tecto. Mecanismo: um movimento já grande na própria vela do
# gatilho significa que a maior parte do movimento já aconteceu antes de
# entrarmos — pouco espaço sobra para o trailing trabalhar. NÃO filtra a
# detecção (s2b_v2.py continua a registar tudo, sempre) — só decide se o
# paper mode abre posição a sério. Sinais fora do tecto continuam visíveis
# no outcomes file, só não geram 'paper_trailing'.
VAR_PRECO_GATILHO_MAX_PCT = float(os.environ.get("VAR_PRECO_GATILHO_MAX_PCT", "20"))
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


def ler_outcomes(path: str) -> list:
    """
    CORREÇÃO 17/07/2026: com ficheiros por dia, o de hoje pode
    legitimamente ainda não existir (primeiro fecho do dia) — 404 nesse
    caso é normal, não um erro; devolve lista vazia em vez de rebentar.
    """
    try:
        sha = gh_get_file_sha(path)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    raw = gh_get_blob(sha)
    return json.loads(raw)


def escrever_outcomes(path: str, dados: list, mensagem: str, tentativas: int = 5) -> None:
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
                        "path": path,
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

            log.info("Commit escrito em %s: %s", path, mensagem)
            return

        except requests.HTTPError as e:
            espera = min(2 ** tentativa, 30)
            log.warning("Falha ao escrever no GitHub (tentativa %d/%d): %s — a esperar %ds",
                        tentativa, tentativas, e, espera)
            time.sleep(espera)

    raise RuntimeError(f"Não foi possível escrever '{path}' após {tentativas} tentativas.")


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
    # CORREÇÃO 15/07/2026: origem_ficheiro diz de qual dos dois outcomes
    # files (v2=clássico+sobe-desce, rapida=detecção rápida) este sinal
    # veio — necessário desde que deixaram de partilhar o mesmo ficheiro,
    # para saber onde escrever o resultado de volta ao fechar. ALTER TABLE
    # em vez de incluir na CREATE TABLE porque bases já existentes (deploys
    # anteriores a hoje) não voltam a correr o CREATE TABLE IF NOT EXISTS.
    try:
        con.execute("ALTER TABLE posicoes ADD COLUMN origem_ficheiro TEXT NOT NULL DEFAULT 'v2'")
    except sqlite3.OperationalError:
        pass  # coluna já existe, migração já correu antes
    # CORREÇÃO 15/07/2026 (nº2): antes escrevia ao GitHub em TODOS os ciclos
    # (60s) em que alguma posição fechasse — com o volume de sinais de hoje,
    # isso colidia com o scanner.yml (15 min) a escrever no mesmo
    # s2b_outcomes_v2.json. 'exportado' marca posições já fechadas mas ainda
    # por escrever no GitHub — passam a acumular-se e a ser escritas em lote,
    # de X em X minutos (ver EXPORT_INTERVAL_SECONDS), não a cada fecho.
    try:
        con.execute("ALTER TABLE posicoes ADD COLUMN exportado INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # CORREÇÃO 20/07/2026: fade_origem_id — NULL para sinais originais,
    # id da posição original para posições invertidas (fade). Serve dois
    # propósitos: rastreabilidade (juntar de volta à posição que a
    # originou) e guarda contra fade-de-fade (só sinais originais, com
    # fade_origem_id NULL, são elegíveis para serem invertidos).
    try:
        con.execute("ALTER TABLE posicoes ADD COLUMN fade_origem_id INTEGER")
    except sqlite3.OperationalError:
        pass
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

def sincronizar_novos_sinais(con: sqlite3.Connection, outcomes: list, origem: str):
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

        # CORREÇÃO 15/07/2026: um único registo com sinais_lancamento=None
        # (falha transitória na origem, ex: MEXC não respondeu a tempo do
        # snapshot) rebentava este ciclo inteiro, sempre no mesmo sítio,
        # todos os ciclos — nenhuma posição nova sincronizava enquanto o
        # registo mau continuasse na lista (aconteceu com WISHBONE_USDT).
        # Salta só este registo, não pára a sincronização dos outros.
        sl_info = entrada.get("sinais_lancamento")
        if not sl_info or sl_info.get("atr_pct") is None:
            log.warning("Registo sem sinais_lancamento válido, a saltar [%s]: %s", origem, symbol)
            continue

        var_preco_gatilho = entrada.get("var_preco_gatilho")
        if var_preco_gatilho is not None and abs(var_preco_gatilho) > VAR_PRECO_GATILHO_MAX_PCT:
            log.info("Fora do tecto de var_preco_gatilho (%.1f%% > %.1f%%) — sem posição paper: %s",
                      abs(var_preco_gatilho), VAR_PRECO_GATILHO_MAX_PCT, entrada["symbol"])
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
                    sl_inicial, sl_atual, extremo_favoravel, activado, estado, origem_ficheiro)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'aberta', ?)""",
                (symbol, entrada["timestamp_entrada"], direccao, preco_entrada, atr_pct,
                 atr_preco, sl_inicial, sl_inicial, preco_entrada, origem),
            )
            con.commit()
            novos += 1
            log.info("Nova posição paper [%s]: %s %s @ %.8f (ATR%%=%.3f, SL inicial=%.8f)",
                      origem, symbol, direccao, preco_entrada, atr_pct, sl_inicial)
        except sqlite3.IntegrityError:
            pass  # já existe localmente (UNIQUE symbol+timestamp_entrada)

    if novos:
        log.info("Sincronização [%s]: %d posição(ões) nova(s) apanhada(s).", origem, novos)


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

        # Fade — ver constante FADE_ATIVO/FADE_HORAS_LIMITE para o porquê.
        # fade_origem_id IS NULL garante que só sinais originais são
        # elegíveis (a posição invertida em si nunca é fadeada outra vez).
        if (FADE_ATIVO and not p["activado"] and not p["fade_origem_id"]
                and horas_decorridas >= FADE_HORAS_LIMITE):
            pnl_pct = calcular_pnl_pct(p["direccao"], p["preco_entrada"], preco_atual)
            con.execute(
                """UPDATE posicoes SET estado='fechada', motivo_fecho='fade_invertido',
                   timestamp_fecho=?, preco_fecho=?, pnl_pct=? WHERE id=?""",
                (agora.isoformat(), preco_atual, pnl_pct, p["id"]),
            )
            log.info("Posição fechada (fade): %s %s preço=%.8f pnl=%.2f%% — a abrir invertida",
                      p["symbol"], p["direccao"], preco_atual, pnl_pct)
            fechadas.append(p["id"])

            direccao_invertida = "SHORT" if p["direccao"] == "LONG" else "LONG"
            sl_inicial_novo = calcular_sl_inicial(direccao_invertida, preco_atual, p["atr_preco"])
            try:
                con.execute(
                    """INSERT INTO posicoes
                       (symbol, timestamp_entrada, direccao, preco_entrada, atr_pct, atr_preco,
                        sl_inicial, sl_atual, extremo_favoravel, activado, estado, origem_ficheiro,
                        fade_origem_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'aberta', ?, ?)""",
                    (p["symbol"], agora.isoformat(), direccao_invertida, preco_atual, p["atr_pct"],
                     p["atr_preco"], sl_inicial_novo, sl_inicial_novo, preco_atual,
                     p["origem_ficheiro"], p["id"]),
                )
                log.info("Posição fade aberta: %s %s @ %.8f (invertida de %s)",
                          p["symbol"], direccao_invertida, preco_atual, p["direccao"])
            except sqlite3.IntegrityError:
                pass  # já existe localmente (raro, mesmo símbolo+segundo)
            con.commit()
            continue

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
    """
    CORREÇÃO 15/07/2026 (nº3): antes escrevia de volta DENTRO dos outcomes
    files (v2/rapida), fundindo com o registo do sinal original — mas isso
    mantinha uma colisão com o scanner.yml (escreve no mesmo v2.json a
    cada 15 min) e com a detecção rápida (rapida.json a cada 5 min). O
    lote de 3 em 3 min (correcção nº2) reduziu a frequência mas não
    eliminou — a janela de tentativas do scanner sozinha já dura quase um
    minuto, e continuava a haver sobreposição.
    Agora escreve num ficheiro PRÓPRIO (FECHOS_PATH), que mais ninguém
    toca — elimina a colisão por completo, não só reduz. Cada registo
    fica indexado por (symbol, timestamp_entrada), a mesma chave usada em
    todo o resto do sistema, para se poder juntar de volta ao sinal
    original em qualquer análise futura.
    """
    if not ids_fechadas:
        return

    caminho_hoje = _caminho_fechos_hoje(datetime.now(timezone.utc))
    fechos = ler_outcomes(caminho_hoje)
    alteracoes = 0

    for pid in ids_fechadas:
        cur = con.execute("SELECT * FROM posicoes WHERE id=?", (pid,))
        colunas = [d[0] for d in cur.description]
        p = dict(zip(colunas, cur.fetchone()))

        hist_cur = con.execute(
            "SELECT timestamp, preco, sl, activado FROM sl_historico WHERE posicao_id=? ORDER BY timestamp",
            (pid,),
        )
        sl_historico = [
            {"timestamp": r[0], "preco": r[1], "sl": r[2], "activado": bool(r[3])}
            for r in hist_cur.fetchall()
        ]

        fechos.append({
            "symbol": p["symbol"],
            "timestamp_entrada": p["timestamp_entrada"],
            "origem_ficheiro": p.get("origem_ficheiro") or "v2",
            "fade_origem_id": p.get("fade_origem_id"),  # None = sinal original; caso contrário, id da posição que originou este fade
            "paper_trailing": {
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
            },
        })
        alteracoes += 1

    if alteracoes:
        escrever_outcomes(
            caminho_hoje,
            fechos,
            f"fechos_paper: {alteracoes} posição(ões) fechada(s) — {datetime.now(timezone.utc).isoformat()}",
        )
        con.executemany(
            "UPDATE posicoes SET exportado=1 WHERE id=?",
            [(pid,) for pid in ids_fechadas],
        )
        con.commit()


# --------------------------------------------------------------------------
# Loop principal
# --------------------------------------------------------------------------

def ciclo(con: sqlite3.Connection):
    global _ultima_exportacao

    outcomes_v2 = ler_outcomes(OUTCOMES_PATH_V2)
    sincronizar_novos_sinais(con, outcomes_v2, "v2")
    outcomes_rapida = ler_outcomes(OUTCOMES_PATH_RAPIDA)
    sincronizar_novos_sinais(con, outcomes_rapida, "rapida")

    precos = obter_precos_actuais()
    atualizar_posicoes_abertas(con, precos)  # actualiza trailing/fecha a cada ciclo, sempre

    # Exportar ao GitHub em lote, não a cada fecho — ver EXPORT_INTERVAL_SECONDS.
    agora_ts = time.time()
    if agora_ts - _ultima_exportacao >= EXPORT_INTERVAL_SECONDS:
        cur = con.execute("SELECT id FROM posicoes WHERE estado='fechada' AND exportado=0")
        pendentes = [row[0] for row in cur.fetchall()]
        if pendentes:
            escrever_fechadas_no_github(con, pendentes)
        _ultima_exportacao = agora_ts


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
