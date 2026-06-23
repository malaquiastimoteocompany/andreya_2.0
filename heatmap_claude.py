# =============================================================================
# heatmap_claude.py — Método A: análise de heatmap de liquidações
# Manual CFI v2.0 — Secção 7.2
#
# ÚNICA peça do sistema que chama a API do Claude.
# Corre em Estado 3 (Momento 1) e no trigger (Momento 2).
# NÃO recalcula durante Estado 4.
#
# Fluxo:
#   1. Playwright abre o heatmap do Coinglass para o token
#   2. Screenshot da zona do gráfico → bytes PNG
#   3. Imagem enviada ao Claude API com prompt estruturado
#   4. Claude devolve JSON com cluster_pct (% de distância)
#   5. Validação: cluster entre 1% e 15% → Método A
#      Fora do range, heatmap uniforme ou falha → Método C (fallback)
# =============================================================================

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Optional

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL,
    COINGLASS_HEATMAP_URL,
    METODO_A_CLUSTER_MIN_PCT,
    METODO_A_CLUSTER_MAX_PCT,
)
from leverage import validar_cluster_metodo_a

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constantes de configuração Playwright
# -----------------------------------------------------------------------------

_HEATMAP_LOAD_TIMEOUT_MS  = 30_000   # 30s para a página carregar
_HEATMAP_RENDER_WAIT_MS   = 4_000    # 4s extra para o canvas renderizar
_HEATMAP_VIEWPORT         = {"width": 1280, "height": 800}

# Selectores CSS para o canvas do heatmap (testar em ordem; usar o primeiro que encontrar)
_HEATMAP_SELECTORS = [
    ".chart-container canvas",
    "canvas.liquidation-heatmap",
    "#liquidation-heatmap canvas",
    "canvas",                          # fallback — qualquer canvas na página
]

# User-agent para evitar bloqueios de bot no Coinglass
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# -----------------------------------------------------------------------------
# Prompt enviado ao Claude (Método A)
# -----------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are analyzing a Coinglass liquidation heatmap for {ticker} perpetual futures.

Current price: ${preco:.8f}
Trade direction: {direccao}

In this heatmap:
- Bright yellow/orange zones = large clusters of pending liquidations at that price level
- Y-axis = price levels (higher = more expensive)
- X-axis = time (right = most recent)

Your task: identify the NEAREST significant bright cluster in the relevant direction.
- LONG → look ABOVE the current price (short liquidations triggered on a rally)
- SHORT → look BELOW the current price (long liquidations triggered on a drop)

Return ONLY a JSON object — no other text, no markdown:
{{"cluster_pct": <float_or_null>}}

Where cluster_pct is the % distance from current price to the cluster centre (always positive, e.g. 0.05 = 5%).
Return null if there is no clear bright cluster between 1% and 15% from the current price in the relevant direction.\
"""


# -----------------------------------------------------------------------------
# Captura do heatmap via Playwright (assíncrono)
# -----------------------------------------------------------------------------

async def _capturar_heatmap_async(ticker: str) -> Optional[bytes]:
    """
    Abre o heatmap do Coinglass para o ticker dado e devolve um screenshot
    do canvas como bytes PNG.
    Devolve None em caso de falha (timeout, selector não encontrado, etc.).
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PwTimeout
    except ImportError:
        logger.error("playwright não instalado — pip install playwright && playwright install chromium")
        return None

    url = COINGLASS_HEATMAP_URL.format(ticker=ticker)
    logger.info(f"[{ticker}] Playwright: a abrir {url}")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport=_HEATMAP_VIEWPORT,
                user_agent=_USER_AGENT,
            )
            page = await context.new_page()

            # Carregar página
            await page.goto(url, wait_until="networkidle",
                            timeout=_HEATMAP_LOAD_TIMEOUT_MS)

            # Aguardar renderização do canvas
            await page.wait_for_timeout(_HEATMAP_RENDER_WAIT_MS)

            # Tentar cada selector até encontrar um canvas visível
            elemento = None
            for selector in _HEATMAP_SELECTORS:
                try:
                    el = page.locator(selector).first
                    await el.wait_for(state="visible", timeout=5_000)
                    elemento = el
                    logger.debug(f"[{ticker}] Canvas encontrado com selector: {selector!r}")
                    break
                except Exception:
                    continue

            if elemento is None:
                # Fallback: screenshot de toda a página (mais pesado mas funciona)
                logger.warning(f"[{ticker}] Nenhum canvas encontrado — screenshot da página completa")
                imagem = await page.screenshot(type="png", full_page=False)
            else:
                imagem = await elemento.screenshot(type="png")

            await browser.close()
            logger.info(f"[{ticker}] Screenshot capturado ({len(imagem)} bytes)")
            return imagem

    except Exception as e:
        logger.error(f"[{ticker}] Playwright falhou: {e}")
        return None


def capturar_heatmap(ticker: str) -> Optional[bytes]:
    """
    Wrapper síncrono para _capturar_heatmap_async.
    Compatível com código não-async (scan agents).
    """
    try:
        return asyncio.run(_capturar_heatmap_async(ticker))
    except Exception as e:
        logger.error(f"[{ticker}] asyncio.run falhou: {e}")
        return None


# -----------------------------------------------------------------------------
# Análise do heatmap via Claude API
# -----------------------------------------------------------------------------

def analisar_heatmap_claude(
    imagem_bytes: bytes,
    ticker: str,
    preco_atual: float,
    direccao: str,
) -> Optional[float]:
    """
    Envia o screenshot do heatmap para o Claude e extrai o cluster_pct.
    Devolve a % de distância como decimal (ex: 0.05 = 5%) ou None.

    O Claude responde em JSON: {"cluster_pct": 0.05} ou {"cluster_pct": null}.
    """
    imagem_b64 = base64.standard_b64encode(imagem_bytes).decode("utf-8")
    prompt = _PROMPT_TEMPLATE.format(
        ticker=ticker,
        preco=preco_atual,
        direccao=direccao,
    )

    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=100,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": imagem_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                }
            ],
        )

        texto = resposta.content[0].text.strip()
        logger.debug(f"[{ticker}] Claude respondeu: {texto!r}")

        # Extrair JSON — o Claude pode envolver em ```json ... ``` por hábito
        if "```" in texto:
            texto = texto.split("```")[-2].strip()
            if texto.startswith("json"):
                texto = texto[4:].strip()

        dados = json.loads(texto)
        cluster_pct = dados.get("cluster_pct")

        if cluster_pct is None:
            logger.info(f"[{ticker}] Claude: sem cluster claro → Método C")
            return None

        cluster_pct = float(cluster_pct)
        logger.info(f"[{ticker}] Claude: cluster_pct = {cluster_pct:.4f} ({cluster_pct*100:.2f}%)")
        return cluster_pct

    except json.JSONDecodeError as e:
        logger.error(f"[{ticker}] Claude devolveu JSON inválido: {e} — texto: {texto!r}")
        return None
    except anthropic.APIError as e:
        logger.error(f"[{ticker}] Anthropic API error: {e}")
        return None
    except Exception as e:
        logger.error(f"[{ticker}] analisar_heatmap_claude falhou: {e}")
        return None


# -----------------------------------------------------------------------------
# Função principal — chamada pelo scanner
# -----------------------------------------------------------------------------

def obter_target_metodo_a(
    ticker: str,
    preco_atual: float,
    direccao: str,
    atr_1h_pct: float,
) -> tuple[Optional[float], str]:
    """
    Tenta obter o target via Método A (heatmap + Claude).
    Se falhar em qualquer passo, cai para Método C.

    Retorna: (target_pct, metodo_usado)
      target_pct:   distância do target em decimal (ex: 0.05 = 5%), sempre positivo
      metodo_usado: "A" se cluster válido encontrado, "C" caso contrário

    Critério de cluster válido (manual 7.2):
      - zona brilhante entre 1% e 15% do preço actual
      - fora deste range, heatmap uniforme ou falha Playwright → Método C

    Este é o ÚNICO ponto onde o Claude API é invocado em todo o sistema.
    Manual secção 7.2 — Método A.
    """
    from config import METODO_C_ATR_MULTIPLICADOR

    metodo_c_target = METODO_C_ATR_MULTIPLICADOR * atr_1h_pct

    # ── 1. Capturar heatmap ───────────────────────────────────────────────────
    logger.info(f"[{ticker}] Método A: a capturar heatmap...")
    imagem = capturar_heatmap(ticker)

    if imagem is None:
        logger.warning(f"[{ticker}] Playwright falhou → Método C (target={metodo_c_target:.4f})")
        return metodo_c_target, "C"

    # ── 2. Analisar com Claude ────────────────────────────────────────────────
    logger.info(f"[{ticker}] Método A: a enviar imagem ao Claude...")
    cluster_pct = analisar_heatmap_claude(imagem, ticker, preco_atual, direccao)

    if cluster_pct is None:
        logger.info(f"[{ticker}] Claude: sem cluster → Método C (target={metodo_c_target:.4f})")
        return metodo_c_target, "C"

    # ── 3. Validar range do cluster ───────────────────────────────────────────
    if not validar_cluster_metodo_a(cluster_pct):
        logger.warning(
            f"[{ticker}] Cluster {cluster_pct*100:.2f}% fora do range "
            f"[{METODO_A_CLUSTER_MIN_PCT*100:.0f}%-{METODO_A_CLUSTER_MAX_PCT*100:.0f}%] "
            f"→ Método C"
        )
        return metodo_c_target, "C"

    # ── 4. Método A válido ────────────────────────────────────────────────────
    logger.info(
        f"[{ticker}] Método A: cluster válido a {cluster_pct*100:.2f}% "
        f"(Método C seria {metodo_c_target*100:.2f}%)"
    )
    return cluster_pct, "A"
