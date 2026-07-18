# =============================================================================
# historico_cruzado.py — cruzamento de /analise_token com histórico já
# detectado pelo S2b (andreya_2.0) e pelo CSA (andreya_scalp_data)
#
# Usado só por analise_token() em scanner.py. Best-effort: qualquer falha
# aqui devolve um resumo vazio, nunca deve rebentar o comando /analise_token.
#
# Nota honesta sobre o que este módulo NÃO faz: s2b_outcomes_v2.json não
# guarda um "pico"/"resultado final" pré-calculado por sinal — isso só
# existe nos scripts de análise offline (ver s2b_manual_v1.md secção 4).
# O que este módulo dá é o que está mesmo no ficheiro: o gatilho e o
# checkpoint mais recente disponível para esse sinal. Também só olha para
# s2b_outcomes_v2.json (activo) — não percorre os arquivos diários
# (s2b_arquivo_2026-07-*.json) nem o backlog pré-15jul; isso fica como
# extensão futura se vier a fazer falta (ver nota no fim do ficheiro).
# =============================================================================

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from config import GITHUB_TOKEN

log = logging.getLogger(__name__)

_TIMEOUT = 20
_MAX_SINAIS_S2B = 5
_MAX_ALERTAS_CSA = 5

_REPO_S2B = "malaquiastimoteocompany/andreya_2.0"
_REPO_CSA = "malaquiastimoteocompany/andreya_scalp_data"


# -----------------------------------------------------------------------------
# Leitura genérica de JSON de qualquer repo (mesmo padrão de retry/>1MB que
# _carregar_json_github em s2b_v2.py, mas parametrizado por repo — aquele
# helper só lê do GITHUB_REPO fixo, não serve para o repo do CSA)
# -----------------------------------------------------------------------------

def _ler_json_repo(repo: str, path: str) -> Optional[Any]:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "andreya-analise-token",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            resp = json.loads(r.read())

        conteudo_b64 = resp.get("content")
        if conteudo_b64:
            conteudo = base64.b64decode(conteudo_b64).decode()
        else:
            download_url = resp.get("download_url")
            if not download_url:
                log.warning(f"{repo}/{path}: sem 'content' nem 'download_url'")
                return None
            with urllib.request.urlopen(download_url, timeout=_TIMEOUT) as r:
                conteudo = r.read().decode()

        return json.loads(conteudo)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            log.info(f"{repo}/{path}: não encontrado (404)")
        else:
            log.warning(f"{repo}/{path}: HTTP {e.code}")
        return None
    except Exception as e:
        log.warning(f"{repo}/{path}: falha a ler ({e})")
        return None


# -----------------------------------------------------------------------------
# S2b — s2b_historico.json (estado ao vivo) + s2b_outcomes_v2.json (sinais)
# -----------------------------------------------------------------------------

def obter_historico_s2b(symbol: str) -> dict:
    """
    symbol: já normalizado com sufixo _USDT (ex: "SHIB_USDT").
    Devolve {"em_observacao": bool, "sinais": [ {...}, ... ]} — sinais
    ordenados do mais recente para o mais antigo, no máximo _MAX_SINAIS_S2B.
    Em qualquer falha devolve {"em_observacao": False, "sinais": []}.
    """
    vazio: dict = {"em_observacao": False, "sinais": []}

    historico = _ler_json_repo(_REPO_S2B, "s2b_historico.json")
    em_observacao = False
    if isinstance(historico, dict):
        registo = historico.get(symbol, {})
        em_observacao = bool(registo.get("em_observacao", False))

    outcomes = _ler_json_repo(_REPO_S2B, "s2b_outcomes_v2.json")
    if not isinstance(outcomes, list):
        return {"em_observacao": em_observacao, "sinais": []}

    correspondentes = [o for o in outcomes if isinstance(o, dict) and o.get("symbol") == symbol]
    correspondentes.sort(key=lambda o: o.get("timestamp_entrada", ""), reverse=True)

    sinais = []
    for o in correspondentes[:_MAX_SINAIS_S2B]:
        checkpoints = o.get("checkpoints") or {}
        preco_entrada = o.get("preco_entrada")
        ultimo_checkpoint_pct = None
        n_checkpoints = len(checkpoints)
        if checkpoints and preco_entrada:
            try:
                ultima_chave = max(checkpoints.keys(), key=lambda k: int(k))
                preco_ultimo = checkpoints[ultima_chave].get("preco")
                if preco_ultimo:
                    ultimo_checkpoint_pct = (preco_ultimo / preco_entrada - 1) * 100
            except (ValueError, TypeError, ZeroDivisionError):
                pass

        sinais.append({
            "timestamp_entrada": o.get("timestamp_entrada"),
            "direccao": o.get("direccao"),
            "tipo_gatilho": o.get("tipo_gatilho"),
            "var_preco_gatilho_pct": o.get("var_preco_gatilho"),
            "n_checkpoints": n_checkpoints,
            "ultimo_checkpoint_pct": ultimo_checkpoint_pct,
        })

    return {"em_observacao": em_observacao, "sinais": sinais}


# -----------------------------------------------------------------------------
# CSA — csa_alertas.json (activo) + csa_alertas_historico.json (arquivo)
# -----------------------------------------------------------------------------

def obter_historico_csa(symbol: str) -> dict:
    """
    symbol: com ou sem sufixo _USDT — o CSA guarda o token base (ex: "BTC"),
    por isso o sufixo é removido antes de comparar.
    Devolve {"alertas": [ {...}, ... ]}, mais recente primeiro, no máximo
    _MAX_ALERTAS_CSA. Em qualquer falha devolve {"alertas": []}.
    """
    token_base = symbol.replace("_USDT", "").upper()

    todos: list[dict] = []
    for ficheiro in ("csa_alertas.json", "csa_alertas_historico.json"):
        dados = _ler_json_repo(_REPO_CSA, ficheiro)
        if isinstance(dados, list):
            todos.extend(d for d in dados if isinstance(d, dict))

    correspondentes = [d for d in todos if str(d.get("token", "")).upper() == token_base]
    correspondentes.sort(key=lambda d: d.get("data_alerta", ""), reverse=True)

    # Deduplicar por (data_alerta, setup) — csa_alertas.json e o histórico
    # podem sobrepor-se num alerta que acabou de ser arquivado
    vistos = set()
    alertas = []
    for d in correspondentes:
        chave = (d.get("data_alerta"), d.get("setup"), d.get("direccao"))
        if chave in vistos:
            continue
        vistos.add(chave)
        alertas.append({
            "data_alerta": d.get("data_alerta"),
            "direccao": d.get("direccao"),
            "setup": d.get("setup"),
            "score": d.get("score"),
            "resultado": d.get("resultado"),
            "pnl_pct": d.get("pnl_pct"),
        })
        if len(alertas) >= _MAX_ALERTAS_CSA:
            break

    return {"alertas": alertas}


# -----------------------------------------------------------------------------
# Extensão futura (não implementada): se um dia fizer falta procurar sinais
# S2b anteriores ao ficheiro activo, os arquivos diários
# (s2b_arquivo_2026-07-15.json, -16, -17, -18, ...) e
# s2b_arquivo_backlog_pre_15jul.json seguem o mesmo schema de
# s2b_outcomes_v2.json — bastaria listar o directório via
# GET /repos/{repo}/contents/ e aplicar o mesmo filtro por "symbol".
# Não fiz isto agora para manter o /analise_token rápido (um comando
# Telegram não deve demorar dezenas de segundos a responder).
# -----------------------------------------------------------------------------
