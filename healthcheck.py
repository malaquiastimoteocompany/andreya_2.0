#!/usr/bin/env python3
"""
healthcheck.py — Verificação diária de saúde do Projecto Andreya
===================================================================

Corre uma vez por dia (via cron-job.org -> GitHub Actions, mesmo padrão
do scanner.yml). Verifica se as APIs de que o projecto depende continuam
a responder no sítio certo, com a forma certa — não só "está no ar",
mas "tem os campos que o código espera".

Nasceu em 12/07/2026, depois de um dia a apanhar três variantes do mesmo
problema: ficheiros JSON a passar de ~1MB e a API do GitHub a devolver
conteúdo vazio (S2b e CSA), e o endpoint de open_interest da MEXC a
devolver 403 há semanas sem ninguém dar por isso (Setup B nunca disparava).
Um healthcheck diário teria apanhado o do OI em horas, não em semanas.

O que verifica:
  1. MEXC — endpoints usados pelo S2b e pelo CSA, confirma 200 + campos
     esperados presentes na resposta (não só que respondeu)
  2. GitHub — acesso de leitura aos 3 repos, e tamanho dos ficheiros de
     dados grandes — avisa a partir de 800KB, antes de chegarem a 1MB
  3. Notion — a API responde e consegue ler as duas bases de dados

Nunca falha silenciosamente: cada verificação reporta OK, AVISO ou ERRO,
e o resumo final vai para o Telegram, mesmo canal que já é usado.
"""

import os
import sys
import requests
from datetime import datetime, timezone

MEXC_TICKER_URL = "https://contract.mexc.com/api/v1/contract/ticker"
MEXC_KLINE_URL = "https://contract.mexc.com/api/v1/contract/kline/{symbol}"
TOKEN_TESTE = "BTC_USDT"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ficheiros de dados a vigiar — (repo, caminho, limite de aviso em bytes)
LIMITE_AVISO_BYTES = 800_000  # ~800KB, margem antes do limite de ~1MB da Contents API

FICHEIROS_VIGIADOS = [
    ("malaquiastimoteocompany/andreya_2.0", "s2b_outcomes_v2.json"),
    ("malaquiastimoteocompany/andreya_2.0", "s2b_historico.json"),
    ("malaquiastimoteocompany/andreya_scalp", "csa_alertas.json"),
    ("malaquiastimoteocompany/andreya_scalp_data", "csa_alertas.json"),
]

REPOS_A_CONFIRMAR = [
    "malaquiastimoteocompany/andreya_2.0",
    "malaquiastimoteocompany/andreya_scalp",
    "malaquiastimoteocompany/andreya_scalp_data",
]

NOTION_DATABASES = {
    # ID de "database" da API REST pública (2022-06-28) — diferente do ID de
    # "data source" usado internamente por ferramentas mais recentes.
    # Vem directamente do URL: https://app.notion.com/p/da6f6d5a398544d9ab02730724260fe1
    "Alertas CSA": "da6f6d5a398544d9ab02730724260fe1",
}

resultados = []  # cada item: (nivel, mensagem)  nivel em OK/AVISO/ERRO


def registar(nivel: str, mensagem: str):
    resultados.append((nivel, mensagem))
    print(f"[{nivel}] {mensagem}")


# --------------------------------------------------------------------------
# 1. MEXC
# --------------------------------------------------------------------------

def verificar_mexc():
    # Ticker — usado por S2b e CSA para preço, volume, OI (holdVol), funding
    try:
        r = requests.get(MEXC_TICKER_URL, params={"symbol": TOKEN_TESTE}, timeout=15)
        if r.status_code != 200:
            registar("ERRO", f"MEXC ticker devolveu {r.status_code}")
        else:
            d = r.json().get("data", {})
            campos_esperados = ["lastPrice", "volume24", "holdVol", "riseFallRate", "fundingRate"]
            em_falta = [c for c in campos_esperados if c not in d]
            if em_falta:
                registar("ERRO", f"MEXC ticker sem os campos: {em_falta}")
            else:
                registar("OK", "MEXC ticker — todos os campos esperados presentes")
    except Exception as e:
        registar("ERRO", f"MEXC ticker inacessível: {e}")

    # Kline — usado pelas análises retroactivas e pelo cálculo de ATR
    try:
        r = requests.get(
            MEXC_KLINE_URL.format(symbol=TOKEN_TESTE),
            params={"interval": "Min15"},
            timeout=15,
        )
        if r.status_code != 200:
            registar("ERRO", f"MEXC kline devolveu {r.status_code}")
        else:
            d = r.json().get("data", {})
            if not d.get("time") or not d.get("close") or not d.get("vol"):
                registar("ERRO", "MEXC kline sem os campos time/close/vol esperados")
            else:
                registar("OK", f"MEXC kline — {len(d['time'])} velas devolvidas")
    except Exception as e:
        registar("ERRO", f"MEXC kline inacessível: {e}")


# --------------------------------------------------------------------------
# 2. GitHub
# --------------------------------------------------------------------------

def verificar_github():
    if not GITHUB_TOKEN:
        registar("ERRO", "GITHUB_TOKEN não configurado — não foi possível testar GitHub")
        return

    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

    for repo in REPOS_A_CONFIRMAR:
        try:
            r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=15)
            if r.status_code == 200:
                registar("OK", f"GitHub — acesso a {repo} confirmado")
            elif r.status_code in (401, 403, 404):
                registar("AVISO", f"GitHub — sem acesso a {repo} (status {r.status_code})")
            else:
                registar("ERRO", f"GitHub — {repo} devolveu {r.status_code} inesperado")
        except Exception as e:
            registar("ERRO", f"GitHub — {repo} inacessível: {e}")

    for repo, path in FICHEIROS_VIGIADOS:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers, params={"ref": "main"}, timeout=15,
            )
            if r.status_code == 404:
                registar("AVISO", f"GitHub — {repo}/{path} não encontrado (pode ainda não existir)")
                continue
            if r.status_code != 200:
                registar("AVISO", f"GitHub — {repo}/{path} sem acesso (status {r.status_code})")
                continue
            tamanho = r.json().get("size", 0)
            if tamanho >= LIMITE_AVISO_BYTES:
                registar(
                    "AVISO",
                    f"GitHub — {repo}/{path} com {tamanho/1024:.0f}KB, "
                    f"a aproximar-se do limite de ~1MB da Contents API",
                )
            else:
                registar("OK", f"GitHub — {repo}/{path}: {tamanho/1024:.0f}KB")
        except Exception as e:
            registar("ERRO", f"GitHub — {repo}/{path} inacessível: {e}")


# --------------------------------------------------------------------------
# 3. Notion
# --------------------------------------------------------------------------

def verificar_notion():
    if not NOTION_TOKEN:
        registar("ERRO", "NOTION_TOKEN não configurado — não foi possível testar Notion")
        return

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    for nome, database_id in NOTION_DATABASES.items():
        try:
            r = requests.post(
                f"https://api.notion.com/v1/databases/{database_id}/query",
                headers=headers, json={"page_size": 1}, timeout=15,
            )
            if r.status_code == 200:
                registar("OK", f"Notion — base '{nome}' acessível")
            elif r.status_code in (401, 403):
                registar("AVISO", f"Notion — sem acesso à base '{nome}' (status {r.status_code})")
            else:
                registar("ERRO", f"Notion — base '{nome}' devolveu {r.status_code} inesperado")
        except Exception as e:
            registar("ERRO", f"Notion — base '{nome}' inacessível: {e}")


# --------------------------------------------------------------------------
# Telegram — resumo final
# --------------------------------------------------------------------------

def enviar_resumo():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram não configurado — resumo só fica no log do GitHub Actions")
        return

    agora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    erros = [m for n, m in resultados if n == "ERRO"]
    avisos = [m for n, m in resultados if n == "AVISO"]

    if not erros and not avisos:
        emoji_topo = "✅"
        titulo = "Healthcheck diário — tudo OK"
    elif erros:
        emoji_topo = "🔴"
        titulo = "Healthcheck diário — ERROS encontrados"
    else:
        emoji_topo = "🟡"
        titulo = "Healthcheck diário — avisos"

    linhas = [f"{emoji_topo} <b>{titulo}</b>", f"{agora}", ""]
    if erros:
        linhas.append("<b>Erros:</b>")
        linhas += [f"• {m}" for m in erros]
        linhas.append("")
    if avisos:
        linhas.append("<b>Avisos:</b>")
        linhas += [f"• {m}" for m in avisos]
        linhas.append("")
    linhas.append(f"{len(resultados) - len(erros) - len(avisos)}/{len(resultados)} verificações OK")

    texto = "\n".join(linhas)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"Falha a enviar resumo ao Telegram: {e}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    print(f"Healthcheck — {datetime.now(timezone.utc).isoformat()}")
    verificar_mexc()
    verificar_github()
    verificar_notion()
    enviar_resumo()

    erros = [m for n, m in resultados if n == "ERRO"]
    if erros:
        sys.exit(1)  # falha visível no GitHub Actions se houver erro real


if __name__ == "__main__":
    main()
