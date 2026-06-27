# =============================================================================
# config.py — Crypto Futures Intelligence v2.0
# Fonte única de verdade para todos os thresholds e configurações
# Manual de referência: CFI v2.0 — 14 de Junho de 2026
# NÃO editar thresholds aqui directamente após os primeiros 3 meses —
# usar o processo de revisão manual (Fase 2) descrito na secção 11.
# =============================================================================

import os
from zoneinfo import ZoneInfo

# -----------------------------------------------------------------------------
# TIMEZONE
# -----------------------------------------------------------------------------
TZ_LISBOA = ZoneInfo("Europe/Lisbon")
TZ_UTC    = ZoneInfo("UTC")
# Todos os timestamps no JSON/GitHub em UTC.
# Conversão para Lisboa apenas no output (Telegram, PDF, Notion).

# -----------------------------------------------------------------------------
# EXCHANGE — MEXC (futuros perpétuos USDT-M)
# -----------------------------------------------------------------------------
EXCHANGE         = "MEXC"
TICKER_FORMATO   = "{base}_USDT"          # ex: PEPE_USDT, BTC_USDT
MEXC_BASE_URL    = "https://contract.mexc.com/api/v1"
MEXC_API_KEY     = os.environ["MEXC_API_KEY"]
MEXC_API_SECRET  = os.environ["MEXC_API_SECRET"]

# -----------------------------------------------------------------------------
# TELEGRAM
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003811042997")

# -----------------------------------------------------------------------------
# ANTHROPIC — Claude API (Método A: análise de heatmap)
# -----------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = "claude-sonnet-4-6"
# Chamada à API apenas em heatmap_claude.py — nunca noutros módulos.

# -----------------------------------------------------------------------------
# COINGLASS (Playwright)
# -----------------------------------------------------------------------------
COINGLASS_BASE_URL    = "https://www.coinglass.com"
COINGLASS_HEATMAP_URL = (
    "https://www.coinglass.com/pro/futures/LiquidationHeatMap?coin={ticker}"
)
# OI, funding, L/S e heatmap extraídos via Playwright DOM.

# -----------------------------------------------------------------------------
# GITHUB
# -----------------------------------------------------------------------------
GITHUB_REPO   = "malaquiastimoteocompany/andreya_2.0"
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
STATE_JSON_PATH = "state.json"    # raiz do repo — fonte de verdade operacional

# -----------------------------------------------------------------------------
# NOTION — projecto andreya_v2 (independente do CMF)
# -----------------------------------------------------------------------------
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

NOTION_PAGE_RAIZ          = "384a5230-de72-81fb-92b5-de4c0b064b2a"
NOTION_DB_SCANS           = "ebd18b31-bad2-4faa-a812-ff6580dd4930"  # 📡 Base 1
NOTION_DB_TOKENS          = "ef42937a-58a3-4957-8a90-30d78e8ff8db"  # 🪙 Base 2
NOTION_DB_DETECCOES       = "816d9df4-357e-48e8-aedb-e83a754ffb0f"  # 🔍 Base 3
NOTION_DB_MOVES           = "3a76df46-d585-407b-9c02-8d50e2eb3444"  # 📈 Base 4
# -----------------------------------------------------------------------------
# OUTPUT — PDFs (pasta separada do v1.6)
# -----------------------------------------------------------------------------
PDF_OUTPUT_DIR    = "outputs/andreya_v2"
PDF_NOME_FORMATO  = "scout_{hora}_{data}.pdf"  # ex: scout_06h_14Jun2026.pdf
PDF_TEMA_BG       = "#0d1117"
PDF_TEMA_ACCENT   = "#00ff88"

# -----------------------------------------------------------------------------
# SCHEDULE DE SCANS (manual 8.1) — horas Lisboa
# Conversão para UTC feita em runtime (ver utils.py)
# -----------------------------------------------------------------------------
SCAN_PESADO_HORAS_LISBOA    = [6, 10, 13, 18, 22]   # 5× por dia
SCAN_LEVE_INTERVALO_MIN     = 60                      # a cada hora (Estados 2-5)
SCAN_BREAKOUT_INTERVALO_MIN = 30                      # a cada 30 min (só Estado 3)
# 1ª verificação de breakout: 30 min após entrada em Estado 3

# -----------------------------------------------------------------------------
# UNIVERSO — filtros de entrada (manual 2.2 / 2.4)
# -----------------------------------------------------------------------------
UNIVERSO_VOLUME_MIN_USD        = 500_000         # $500K
UNIVERSO_DIAS_LISTADO_MIN      = 14
UNIVERSO_MARKET_CAP_MIN_USD    = 5_000_000       # $5M
UNIVERSO_GRACE_PERIOD_DIAS     = 3
UNIVERSO_TAMANHO_ESPERADO_MIN  = 80
UNIVERSO_TAMANHO_ESPERADO_MAX  = 120

# Revisão do universo: apenas no scan pesado das 06h Lisboa
UNIVERSO_REVISAO_HORA_LISBOA   = 6

# -----------------------------------------------------------------------------
# CATEGORIAS E THRESHOLDS POR CATEGORIA
# -----------------------------------------------------------------------------
CATEGORIAS = [
    "Memes", "AI", "DeFi",
    "Layer 1", "Layer 2",
    "Gaming/NFT", "Infrastructure",
]

# Volume mínimo por categoria — filtra execução (manual 2.3)
VOLUME_MIN_POR_CATEGORIA: dict[str, int] = {
    "Memes":          500_000,
    "AI":             500_000,
    "DeFi":           500_000,
    "Layer 1":      1_000_000,
    "Layer 2":      1_000_000,
    "Gaming/NFT":   1_000_000,
    "Infrastructure":1_000_000,
}

# Leverage máxima por categoria (manual 7.4)
LEVERAGE_MAX_POR_CATEGORIA: dict[str, int] = {
    "Memes":          10,
    "AI":             10,
    "DeFi":            5,
    "Layer 1":         5,
    "Layer 2":         5,
    "Gaming/NFT":      5,
    "Infrastructure":  3,
}
LEVERAGE_MIN_GLOBAL = 2   # mínimo absoluto, todas as categorias

# Miss detection — amplitude range 24h para registar miss (manual 8.1)
MISS_THRESHOLD_PCT: dict[str, float] = {
    "Memes":           0.10,   # >10%
    "AI":              0.10,
    "DeFi":            0.10,
    "Layer 1":         0.07,   # >7%
    "Layer 2":         0.07,
    "Gaming/NFT":      0.07,
    "Infrastructure":  0.07,
}

# Funding extremo LONG por categoria — flag não bloqueante (manual 3.3)
FUNDING_EXTREMO_LONG: dict[str, float] = {
    "Memes":          +0.0010,   # +0.10%
    "AI":             +0.0004,   # +0.04%
    "Gaming/NFT":     +0.0004,   # +0.04%
    "DeFi":           +0.00025,  # +0.025%
    "Layer 1":        +0.00025,
    "Layer 2":        +0.00025,
    "Infrastructure": +0.00025,
}

# Funding extremo SHORT por categoria — flag não bloqueante (manual 3.3)
FUNDING_EXTREMO_SHORT: dict[str, float] = {
    "Memes":          -0.0005,   # -0.05%
    "AI":             -0.0002,   # -0.02%
    "Gaming/NFT":     -0.0002,   # -0.02%
    "DeFi":           -0.00015,  # -0.015%
    "Layer 1":        -0.00015,
    "Layer 2":        -0.00015,
    "Infrastructure": -0.00015,
}

# -----------------------------------------------------------------------------
# SINAIS — S1 a S6 (manual 3.1 / 3.2)
# S3 e S4 são iguais para LONG e SHORT.
# S1, S2, S5, S6 têm versões distintas.
# -----------------------------------------------------------------------------

# S1 LONG — Volume seco
S1_LONG_VOLUME_MAX_PCT_MEDIA7D = 0.60    # < 60% da média 7 dias

# S1 SHORT — Volume controlado crescente
S1_SHORT_VOLUME_MIN_PCT_MEDIA7D = 0.80   # entre 80% e 150% da média 7 dias
S1_SHORT_VOLUME_MAX_PCT_MEDIA7D = 1.50

# S2 LONG e SHORT — OI a entrar (condições de OI e preço iguais; direcção do
# volume distingue LONG de SHORT)
S2_OI_CHANGE_MIN_PCT   = +0.02   # +2%
S2_OI_CHANGE_MAX_PCT   = +0.15   # +15%
S2_PRECO_CHANGE_MIN_PCT = -0.03  # -3%
S2_PRECO_CHANGE_MAX_PCT = +0.03  # +3%
S2_LONG_VOLUME_SUBIDA_MIN_PCT  = 0.60   # >60% do volume total em candles de subida
S2_SHORT_VOLUME_DESCIDA_MIN_PCT = 0.60  # >60% do volume total em candles de descida

# S3 — Funding neutro (igual LONG e SHORT)
S3_FUNDING_MIN = -0.0001   # -0.01%
S3_FUNDING_MAX = +0.0001   # +0.01%

# S4 — Range apertado (igual LONG e SHORT)
S4_RANGE_MAX_PCT = 0.05    # (High24h - Low24h) / Low24h < 5%

# S5 LONG — Estrutura positiva no 1h
# EMA9(1h) > EMA21(1h) + últimos 4 candles 1h com Low crescente
S5_LONG_EMA9_ACIMA_EMA21  = True
S5_LONG_N_CANDLES_HL      = 4    # Low[N] > Low[N-1], para N = 4 candles fechados

# S5 SHORT — Estrutura negativa no 1h
# EMA9(1h) < EMA21(1h) + últimos 4 candles 1h com High decrescente
S5_SHORT_EMA9_ABAIXO_EMA21 = True
S5_SHORT_N_CANDLES_LH      = 4   # High[N] < High[N-1], para N = 4 candles fechados

# Parâmetros EMA para S5 (MEXC API — candles 1h)
S5_EMA_RAPIDA  = 9
S5_EMA_LENTA   = 21
S5_TIMEFRAME   = "Min60"   # formato MEXC para candles 1h

# S6 LONG — Convicção compradora sem move
S6_LONG_LS_RATIO_MIN       = 1.30    # L/S > 1.3
S6_LONG_PRECO_CHANGE_MAX_PCT = +0.03 # preço 24h < +3%

# S6 SHORT — Convicção vendedora sem move
S6_SHORT_LS_RATIO_MAX        = 0.70   # L/S < 0.7
S6_SHORT_PRECO_CHANGE_MIN_PCT = -0.03 # preço 24h > -3%

# -----------------------------------------------------------------------------
# ATR — parâmetros (manual 7.3)
# -----------------------------------------------------------------------------
ATR_PERIODO    = 14
ATR_TIMEFRAME  = "Min60"   # 1h — formato MEXC

# -----------------------------------------------------------------------------
# SCORING (manual 4.1 / 4.2)
# -----------------------------------------------------------------------------
SCORE_MAXIMO            = 6
SCORE_ESTADO1_MAX       = 3   # 0–3 → Estado 1
SCORE_ESTADO2_MIN       = 4   # 4–5 → Estado 2
SCORE_ESTADO2_MAX       = 5
SCORE_ESTADO3           = 6   # 6/6 → Estado 3
SCORE_DIRECAO_DELTA_MIN = 2   # diferença mínima LONG vs SHORT para definir direcção

# Estado 2: precisa de 2 scans pesados consecutivos (manual 4.3)
ESTADO2_SCANS_CONSECUTIVOS_MIN = 2

# Degradação Estado 3: score desce para 4-5 → Estado 2 (manual 5)
ESTADO3_DEGRADACAO_SCORE_MIN = 4
ESTADO3_DEGRADACAO_SCORE_MAX = 5
# Score ≤3 em Estado 3 → Momento 3A → Estado 1 (regra original)

# -----------------------------------------------------------------------------
# TRIGGERS DE BREAKOUT (manual 6.1)
# -----------------------------------------------------------------------------
# Condição 1 — Volume 30m vs média 6h (12 candles de 30m)
TRIGGER_VOLUME_30M_VS_6H_RATIO = 2.00    # > 200%
TRIGGER_VOLUME_REFERENCIA_CANDLES = 12   # 12 candles de 30m = 6h

# Condição 2 — OI change 30m
TRIGGER_OI_30M_MIN_PCT = 0.03            # > +3%

# Condição 3 — Preço vs High/Low 24h
# LONG:  preço actual > High 24h
# SHORT: preço actual < Low 24h
# (lógica em triggers.py — sem constante numérica adicional)

# -----------------------------------------------------------------------------
# BTC — filtro de regime (manual 10.1)
# -----------------------------------------------------------------------------
BTC_TICKER         = "BTC_USDT"
BTC_EMA_PERIODO    = 21
BTC_EMA_TIMEFRAME  = "Min60"           # 1h

# Volatilidade extrema — pausa breakouts
BTC_VOLATILIDADE_EXTREMA_PCT = 0.03    # >3% num candle de 1h

# Normalização após volatilidade extrema
BTC_NORMALIZADO_N_CANDLES    = 2       # 2 candles de 1h consecutivos
BTC_NORMALIZADO_VAR_MAX_PCT  = 0.015   # cada um com variação < 1.5%

# Comportamento do filtro BTC < EMA21:
# - Scoring continua a correr para TODOS os tokens (incluindo LONG)
# - contador_estado2 continua a incrementar
# - O campo "estado" no JSON NÃO avança para tokens LONG
# - Momento 0, 1, 2 para LONG NÃO são enviados
# - Base 3: campo bloqueado_filtro_btc = true para dados ML
# (implementado em scoring.py e triggers.py)

# -----------------------------------------------------------------------------
# FILTROS MACRO ADICIONAIS (manual 10.2)
# -----------------------------------------------------------------------------
# Funding extremo cross-sector — calculado POR CATEGORIA
FUNDING_CROSS_SECTOR_THRESHOLD = 0.50   # >50% dos tokens da categoria → flag no PDF

# Liquidações extremas
LIQUIDACOES_EXTREMAS_USD = 500_000_000  # >$500M/24h → flag no PDF e update horário

# -----------------------------------------------------------------------------
# LEVERAGE — fórmula e Método A/C (manual 7.2 / 7.3)
# -----------------------------------------------------------------------------
# Método C (base)
METODO_C_ATR_MULTIPLICADOR = 3.0    # Target = 3 × ATR(1h)

# Método A (heatmap — Estado 3 e trigger)
METODO_A_CLUSTER_MIN_PCT = 0.01     # 1% do preço — zona brilhante válida
METODO_A_CLUSTER_MAX_PCT = 0.15     # 15% do preço — acima disto → Método C
# Falha Playwright ou heatmap uniforme → Método C automaticamente

# SL e Leverage
SL_ATR_MULTIPLICADOR      = 1.5     # SL = 1.5 × ATR(1h)
LEVERAGE_NUMERADOR         = 0.10   # Leverage = 10% ÷ distância_target_%
# Arredondamento: sempre para baixo (floor), inteiro mais próximo
# Exemplo: 4.1× → 4×; mínimo global: 2×

# TP escalonado (manual 7.5)
TP1_FRACCAO_TARGET       = 0.5     # TP1 = target / 2 → fecha 50%
TP2_FRACCAO_TARGET       = 1.0     # TP2 = target completo → fecha restante 50%
TP_UNICO_SE_TP1_LT_ATR   = 1.0     # se TP1 < 1×ATR(1h) → TP único (sem split)

# -----------------------------------------------------------------------------
# SIZING SUGERIDO (manual 7.6)
# -----------------------------------------------------------------------------
SIZING_VINHA_ESTADO3    = 0.20   # 20% da banca
SIZING_VINHA_ESTADO2    = 0.15   # 15% da banca
SIZING_SALTO_DIRECTO    = 0.20   # 20% (com nota de cautela no Momento 1 e 2)

# -----------------------------------------------------------------------------
# CONDIÇÕES DE CONCLUSÃO — tempos mínimos (manual 6.4)
# -----------------------------------------------------------------------------
CONCLUSAO_COND1_TEMPO_MIN_H = 0    # Target atingido — sem tempo mínimo
CONCLUSAO_COND2_TEMPO_MIN_H = 2    # Reversão após ganho — mínimo 2h
CONCLUSAO_COND3_TEMPO_MIN_H = 1    # Breakout falso (volta ao nível pré-breakout) — 1h
CONCLUSAO_COND4_TEMPO_MIN_H = 24   # Tempo esgotado — 24h (por natureza, só activa às 24h)

# Condição 2 — limiar de reversão
CONCLUSAO_COND2_REVERSAO_FRACCAO = 0.50   # reverteu >= 50% do ganho máximo

# Campos hora-a-hora da Base 4 para cálculo do ganho máximo
CONCLUSAO_CHECKPOINTS_H = [1, 2, 4, 8, 24]
# Limitação conhecida: picos intra-hora não capturados — imprecisão ocasional aceite

# -----------------------------------------------------------------------------
# ALERTAS TELEGRAM — janela de entrada (manual 9.1)
# -----------------------------------------------------------------------------
MOMENTO2_JANELA_ENTRADA_H = 2   # 2h após alerta — verificar preço se ultrapassado

# Regra de prioridade para condições especiais combinadas no Momento 2:
# 1. BTC volátil  2. Target actualizado  3. Grace period
# (implementado em notificacoes.py)

# -----------------------------------------------------------------------------
# PROTOCOLO DE FALHA DE API (manual 8.1)
# -----------------------------------------------------------------------------
# MEXC falha → scan aborta, sem PDF, sem JSON, API Status = "MEXC falhou"
# Coinglass falha → S2/S3/S6 indisponíveis, score máximo 3/6,
#                   API Status = "Coinglass falhou",
#                   tokens Estado 2+ mantêm estado actual
COINGLASS_FALHA_SCORE_MAX = 3
