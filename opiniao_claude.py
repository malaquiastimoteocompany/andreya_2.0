# =============================================================================
# opiniao_claude.py — opinião sintetizada para /analise_token SYMBOL
#
# Segunda peça do sistema a chamar a API do Claude (a primeira é
# heatmap_claude.py, Método A). Ao contrário daquela, esta não é chamada
# pelos scans automáticos — só pelo comando ad-hoc /analise_token do
# Telegram (scanner.py::analise_token()).
#
# Pega no snapshot quant já calculado (preço, funding, OI, RSI, ATR, S1-S6)
# e no histórico cruzado do S2b/CSA (historico_cruzado.py) e pede ao Claude
# uma leitura curta que tenha ambos em conta — não só o momento actual.
#
# Falha aberta: qualquer erro (API, parsing) devolve None. Quem chama
# (analise_token) já tem de qualquer forma o veredicto por regras como
# fallback — nunca deve ficar sem resposta por causa disto.
# =============================================================================

from __future__ import annotations

import logging
from typing import Optional

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

_MAX_TOKENS = 400


_PROMPT_TEMPLATE = """\
Estás a analisar {symbol} (futuros perpétuos MEXC) para um trader experiente \
que já opera dois sistemas automáticos neste token universe: o S2b (detector \
de breakout por preço+volume) e o CSA (scalp de curto prazo). Responde em \
português europeu.

DADOS ACTUAIS
Preço: {preco:.6g} ({var_24h:+.1f}% 24h)
Funding: {funding:+.4f}%
OI actual: ${oi:,.0f}
Volume 24h: ${volume_24h:,.0f}
RSI(14): {rsi14}
ATR(1h): {atr_pct:.2f}%
Estrutura EMA9/21: {estrutura_ema}
Score S1-S6 LONG: {score_long}/6 ({resumo_long})
Score S1-S6 SHORT: {score_short}/6 ({resumo_short})
No universo activo do CFI: {no_universo}

HISTÓRICO S2b (detector de breakout, mesmo token)
Em observação neste momento: {s2b_em_observacao}
{s2b_sinais}

HISTÓRICO CSA (scalp alerts, mesmo token)
{csa_alertas}

A tua tarefa: escreve uma opinião curta (80-120 palavras, texto corrido, \
sem markdown, pode usar 1 emoji no início: 🟢 para viés LONG, 🔴 para viés \
SHORT, 🟡 para cautela/misto, ⚪ para sem sinal accionável). Usa o \
histórico do S2b/CSA para calibrar a opinião — por exemplo, se este token já \
disparou o S2b antes e o movimento se desenvolveu bem ou mal, ou se tem \
padrão recorrente de scalps CSA, diz isso explicitamente em vez de ignorar. \
Se não há histórico nenhum, diz isso também — é informação relevante, não \
uma lacuna a esconder. Não dês recomendação de entrada/alavancagem — é \
leitura, não conselho de investimento. Não repitas os números todos, só os \
que pesam na tua leitura.

Responde APENAS com o texto da opinião, sem preâmbulo, sem aspas à volta.\
"""


def _formatar_sinais_s2b(sinais: list[dict]) -> str:
    if not sinais:
        return "Nenhum sinal S2b registado para este token."
    linhas = []
    for s in sinais:
        pct_ultimo = s.get("ultimo_checkpoint_pct")
        pct_str = f"{pct_ultimo:+.1f}% desde o gatilho (último checkpoint)" if pct_ultimo is not None else "sem checkpoints suficientes ainda"
        linhas.append(
            f"- {s.get('timestamp_entrada', '?')} | {s.get('direccao', '?')} | "
            f"gatilho: {s.get('tipo_gatilho', '?')} ({s.get('var_preco_gatilho_pct', 0):+.1f}%) | {pct_str}"
        )
    return "\n".join(linhas)


def _formatar_alertas_csa(alertas: list[dict]) -> str:
    if not alertas:
        return "Nenhum alerta CSA registado para este token."
    linhas = []
    for a in alertas:
        resultado = a.get("resultado")
        pnl = a.get("pnl_pct")
        resultado_str = f"{resultado} ({pnl:+.1f}%)" if resultado and pnl is not None else (resultado or "sem resultado registado")
        linhas.append(
            f"- {a.get('data_alerta', '?')} | {a.get('direccao', '?')} | "
            f"setup {a.get('setup', '?')} | score {a.get('score', '?')}/10 | {resultado_str}"
        )
    return "\n".join(linhas)


def gerar_opiniao(
    symbol: str,
    preco: float,
    var_24h_pct: float,
    funding: float,
    oi: float,
    volume_24h: float,
    rsi14: Optional[float],
    atr_pct: float,
    estrutura_ema: str,
    score_long: float,
    resumo_long: str,
    score_short: float,
    resumo_short: str,
    no_universo: bool,
    historico_s2b: dict,
    historico_csa: dict,
) -> Optional[str]:
    """
    Devolve o texto da opinião (pronto para inserir na mensagem Telegram)
    ou None se a chamada à API falhar por qualquer razão.
    """
    prompt = _PROMPT_TEMPLATE.format(
        symbol=symbol,
        preco=preco,
        var_24h=var_24h_pct,
        funding=funding * 100,
        oi=oi,
        volume_24h=volume_24h,
        rsi14=f"{rsi14:.1f}" if rsi14 is not None else "—",
        atr_pct=atr_pct * 100,
        estrutura_ema=estrutura_ema,
        score_long=score_long,
        resumo_long=resumo_long,
        score_short=score_short,
        resumo_short=resumo_short,
        no_universo="sim" if no_universo else "não",
        s2b_em_observacao="sim" if historico_s2b.get("em_observacao") else "não",
        s2b_sinais=_formatar_sinais_s2b(historico_s2b.get("sinais", [])),
        csa_alertas=_formatar_alertas_csa(historico_csa.get("alertas", [])),
    )

    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        resposta = cliente.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = resposta.content[0].text.strip()
        if not texto:
            logger.warning(f"[{symbol}] opiniao_claude: resposta vazia")
            return None
        logger.info(f"[{symbol}] opiniao_claude: opinião gerada ({len(texto)} chars)")
        return texto

    except anthropic.APIError as e:
        logger.error(f"[{symbol}] opiniao_claude: Anthropic API error: {e}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] opiniao_claude: falhou ({e})")
        return None
