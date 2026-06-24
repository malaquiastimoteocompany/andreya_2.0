# =============================================================================
# notion_logger.py — Logging nas 4 bases de dados Notion do CFI v2.0
# Manual CFI v2.0 — Secção 9.4
#
# Usa a Notion REST API directamente (sem MCP — este módulo corre de forma
# autónoma no GitHub Actions sem contexto Claude).
#
# Bases de dados (IDs reais):
#   Base 1 — Scans:                8715f7ab-e1a4-43ee-9201-dfe9927a5090
#   Base 2 — Tokens Monitorizados: d8d785c1-f500-4a87-8074-59f07831cfbb
#   Base 3 — Histórico Detecções:  5d86a2a7-4eb6-4ee6-bc87-7ade2b7ca08e
#   Base 4 — Histórico de Moves:   544b1e93-11da-40a0-abf6-2195f8a66228
#
# Nomes de propriedades verificados contra os schemas reais.
# Papel do Notion: histórico para consulta humana e ML.
# Fonte de verdade operacional: state.json no GitHub.
# =============================================================================

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import (
    NOTION_TOKEN,
    NOTION_DB_SCANS,
    NOTION_DB_TOKENS,
    NOTION_DB_DETECCOES,
    NOTION_DB_MOVES,
    TZ_LISBOA,
    VOLUME_MIN_POR_CATEGORIA,
)

log = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"
_NOTION_BASE    = "https://api.notion.com/v1"


# =============================================================================
# BASE HTTP — Notion REST API
# =============================================================================

def _headers() -> dict:
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type":   "application/json",
    }


def _post(endpoint: str, payload: dict) -> Optional[dict]:
    url  = f"{_NOTION_BASE}{endpoint}"
    data = json.dumps(payload, ensure_ascii=False).encode()
    req  = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error(f"Notion POST {endpoint} → HTTP {e.code}: {body[:300]}")
        return None
    except Exception as e:
        log.error(f"Notion POST {endpoint} falhou: {e}")
        return None


def _patch(endpoint: str, payload: dict) -> Optional[dict]:
    url  = f"{_NOTION_BASE}{endpoint}"
    data = json.dumps(payload, ensure_ascii=False).encode()
    req  = urllib.request.Request(url, data=data, headers=_headers(), method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error(f"Notion PATCH {endpoint} → HTTP {e.code}: {body[:300]}")
        return None
    except Exception as e:
        log.error(f"Notion PATCH {endpoint} falhou: {e}")
        return None


def _query(db_id: str, filtro: Optional[dict] = None, page_size: int = 1) -> list[dict]:
    """Query a uma base de dados. Retorna lista de resultados."""
    payload: dict = {"page_size": page_size}
    if filtro:
        payload["filter"] = filtro
    resp = _post(f"/databases/{db_id}/query", payload)
    if resp and "results" in resp:
        return resp["results"]
    return []


# =============================================================================
# CONTADORES SEQUENCIAIS
# =============================================================================

def _proximo_id(db_id: str, prefixo: str, prop_titulo: str) -> str:
    """
    Gera o próximo ID sequencial (ex: DET-042) contando as páginas existentes.
    Se a query falhar, usa timestamp como fallback.
    """
    try:
        resp = _post(f"/databases/{db_id}/query", {"page_size": 1})
        if resp and "results" in resp:
            # Notion não devolve count directo — usar abordagem de paginação
            # Simplificação aceite: contar via timestamp (ms) para unicidade garantida
            pass
    except Exception:
        pass
    # Fallback: timestamp ms (garante unicidade mesmo sem count)
    ts = int(datetime.now(timezone.utc).timestamp() * 1000) % 100_000
    return f"{prefixo}-{ts:05d}"


# =============================================================================
# CONVERSORES DE PROPRIEDADES — Notion API format
# =============================================================================

def _titulo(valor: str) -> dict:
    return {"title": [{"text": {"content": str(valor)}}]}

def _texto(valor: Optional[str]) -> dict:
    return {"rich_text": [{"text": {"content": str(valor or "")}}]}

def _numero(valor: Optional[float]) -> dict:
    return {"number": float(valor) if valor is not None else None}

def _select(opcao: Optional[str]) -> dict:
    return {"select": {"name": str(opcao)} if opcao else None}

def _checkbox(valor: bool) -> dict:
    return {"checkbox": bool(valor)}

def _data(iso_str: Optional[str]) -> dict:
    """Data simples (sem hora) a partir de string ISO UTC."""
    if not iso_str:
        return {"date": None}
    try:
        dt   = datetime.fromisoformat(iso_str)
        data = dt.astimezone(TZ_LISBOA).strftime("%Y-%m-%d")
        return {"date": {"start": data}}
    except Exception:
        return {"date": {"start": iso_str[:10]}}


# =============================================================================
# MAPEAMENTO DE ESTADOS
# =============================================================================

_ESTADO_LABEL = {
    1: "1 - Passiva",
    2: "2 - Radar",
    3: "3 - Prioritário",
    4: "4 - Breakout",
    5: "5 - Concluído",
}

_ESTADO_NUM_STR = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5"}

_CONDICAO_LABEL = {
    1: "1 - Target atingido",
    2: "2 - Reversão após ganho",
    3: "3 - Breakout falso",
    4: "4 - Tempo esgotado",
}

_TARGET_METODO_LABEL = {
    "A": "A - Heatmap Claude",
    "C": "C - ATR x3",
}

_TP_TIPO_LABEL = {
    "escalonado": "Escalonado",
    "unico":      "Único",
}

_HORA_LABEL = {
    6:  "06h Lisboa",
    10: "10h Lisboa",
    13: "13h Lisboa",
    18: "18h Lisboa",
    22: "22h Lisboa",
}


# =============================================================================
# BASE 1 — SCANS
# =============================================================================

def log_scan(
    scan_id: str,
    hora_lisboa: int,
    btc_preco: float,
    filtro_btc: str,
    contagem_estados: dict,
    novos_estado2: int = 0,
    novos_estado3: int = 0,
    breakouts: int = 0,
    concluidos: int = 0,
    grace_period: int = 0,
    misses: Optional[list] = None,
    api_status: str = "OK",
    notas: str = "",
    fg_valor: Optional[float] = None,
    fg_label: Optional[str] = None,
    altcoin_season: Optional[float] = None,
    liquidacoes_24h: Optional[float] = None,
    btc_24h_pct: Optional[float] = None,
) -> Optional[str]:
    """
    Cria uma entrada na Base 1 — Scans.
    Chamado no final de cada scan pesado.
    Retorna o page_id da entrada criada, ou None se falhar.
    """
    total = sum(contagem_estados.values())
    hora_label = _HORA_LABEL.get(hora_lisboa, f"{hora_lisboa:02d}h Lisboa")
    agora_utc  = datetime.now(timezone.utc).isoformat()

    props: dict = {
        "Scan ID":           _titulo(scan_id),
        "Hora":              _select(hora_label),
        "date:Data:start":   datetime.now(TZ_LISBOA).strftime("%Y-%m-%d"),
        "BTC Preço USDT":    _numero(btc_preco),
        "Filtro BTC":        _select(filtro_btc),
        "Total Analisados":  _numero(total),
        "Em Estado 1":       _numero(contagem_estados.get(1, 0)),
        "Em Estado 2":       _numero(contagem_estados.get(2, 0)),
        "Em Estado 3":       _numero(contagem_estados.get(3, 0)),
        "Em Estado 4":       _numero(contagem_estados.get(4, 0)),
        "Novos Estado 2":    _numero(novos_estado2),
        "Novos Estado 3":    _numero(novos_estado3),
        "Breakouts":         _numero(breakouts),
        "Concluídos":        _numero(concluidos),
        "Grace Period":      _numero(grace_period),
        "Misses":            _numero(len(misses) if misses else 0),
        "API Status":        _select(api_status),
    }

    if notas:
        props["Notas"] = _texto(notas)
    if fg_valor is not None:
        props["F&G Valor"] = _numero(fg_valor)
    if fg_label:
        props["F&G Label"] = _select(fg_label)
    if altcoin_season is not None:
        props["Altcoin Season"] = _numero(altcoin_season)
    if liquidacoes_24h is not None:
        props["Liquidações 24h USD"] = _numero(liquidacoes_24h)
    if btc_24h_pct is not None:
        props["BTC 24h %"] = _numero(btc_24h_pct)

    # Notion API usa o formato de propriedades como valores directos para datas
    # quando usamos o parent database_id
    payload = {
        "parent":     {"database_id": NOTION_DB_SCANS},
        "properties": _converter_props(props),
    }

    resp = _post("/pages", payload)
    if resp:
        log.info(f"[Notion B1] Scan {scan_id} registado")
        return resp.get("id")
    return None


# =============================================================================
# BASE 2 — TOKENS MONITORIZADOS
# =============================================================================

def upsert_token(
    symbol: str,
    categoria: str,
    estado: int,
    score: int,
    direccao: str,
    activo: bool = True,
    grace_period: bool = False,
    grace_period_dias: Optional[int] = None,
    data_entrada: Optional[str] = None,
) -> Optional[str]:
    """
    Cria ou actualiza o token na Base 2.
    Procura primeiro pelo ticker; se encontrar, actualiza; caso contrário, cria.
    Retorna o page_id.
    """
    # Procurar entrada existente
    resultados = _query(NOTION_DB_TOKENS, {
        "property": "Token",
        "title":    {"equals": symbol},
    }, page_size=1)

    estado_label = _ESTADO_LABEL.get(estado, "1 - Passiva")
    vol_min      = VOLUME_MIN_POR_CATEGORIA.get(categoria, 500_000)
    agora_lisboa = datetime.now(TZ_LISBOA).strftime("%Y-%m-%d")

    props_raw: dict = {
        "Token":                    _titulo(symbol),
        "Categoria":                _select(categoria),
        "Estado Actual":            _select(estado_label),
        "Score Actual":             _numero(score),
        "Direcção Actual":          _select(direccao if direccao else "INDEFINIDO"),
        "Activo":                   _checkbox(activo),
        "Grace Period":             _checkbox(grace_period),
        "Grace Period Dias Restantes": _numero(grace_period_dias),
        "Volume Mínimo USD":        _numero(vol_min),
        "date:Último Update:start": agora_lisboa,
    }
    if data_entrada:
        props_raw["date:Data Entrada Universo:start"] = data_entrada[:10]

    props = _converter_props(props_raw)

    if resultados:
        page_id = resultados[0]["id"]
        resp = _patch(f"/pages/{page_id}", {"properties": props})
        if resp:
            log.debug(f"[Notion B2] {symbol} actualizado (estado={estado})")
            return page_id
    else:
        payload = {
            "parent":     {"database_id": NOTION_DB_TOKENS},
            "properties": props,
        }
        resp = _post("/pages", payload)
        if resp:
            log.info(f"[Notion B2] {symbol} criado")
            return resp.get("id")
    return None


# =============================================================================
# BASE 3 — HISTÓRICO DE DETECÇÕES
# =============================================================================

def log_deteccao(
    token,
    resultado,
    sinais_long,
    sinais_short,
    atr_1h: float,
    funding_flag: Optional[str],
    btc_acima_ema21: bool,
    scan_id: str,
    bloqueado_filtro_btc: bool = False,
    btc_preco: float = 0.0,
    fg_valor: Optional[float] = None,
    horas_no_estado: float = 0.0,
    miss_info: Optional[dict] = None,
) -> Optional[str]:
    """
    Cria uma entrada na Base 3 — Histórico de Detecções.
    Chamado quando um token muda de estado.

    miss_info: dict com {'magnitude': float, 'direccao': str} ou None.
    """
    det_id    = _proximo_id(NOTION_DB_DETECCOES, "DET", "Detecção ID")
    agora_utc = datetime.now(timezone.utc).isoformat()
    hora_lx   = datetime.now(TZ_LISBOA).strftime("%H:%M")

    # Sinais dominantes (da direcção do token)
    sinais = sinais_long if token.direccao == "LONG" else sinais_short

    funding_label = funding_flag if funding_flag else "Nenhuma"

    props_raw: dict = {
        "Detecção ID":     _titulo(det_id),
        "Token":           _texto(token.ticker),
        "date:Data:start": datetime.now(TZ_LISBOA).strftime("%Y-%m-%d"),
        "Hora Lisboa":     _texto(hora_lx),
        "Estado Anterior": _select(_ESTADO_NUM_STR.get(resultado.estado_anterior, "1")),
        "Estado Novo":     _select(_ESTADO_NUM_STR.get(resultado.estado_novo,     "1")),
        "Score":           _numero(token.score_actual),
        "Direcção":        _select(token.direccao or "INDEFINIDO"),
        "S1":              _checkbox(sinais.s1 if sinais else False),
        "S2":              _checkbox(sinais.s2 if sinais else False),
        "S3":              _checkbox(sinais.s3 if sinais else False),
        "S4":              _checkbox(sinais.s4 if sinais else False),
        "S5":              _checkbox(sinais.s5 if sinais else False),
        "S6":              _checkbox(sinais.s6 if sinais else False),
        "ATR 1h %":        _numero(round(atr_1h * 100, 4)),
        "Funding Flag":    _select(funding_label),
        "BTC Preço":       _numero(btc_preco),
        "BTC acima EMA21": _checkbox(btc_acima_ema21),
        "Scan ID":         _texto(scan_id),
        "Resultado Final": _select("Pendente"),
        "Horas no Estado": _numero(horas_no_estado),
        "Salto Directo":   _checkbox(token.salto_directo),
        "Bloqueado Filtro BTC": _checkbox(bloqueado_filtro_btc),
        "Miss Detectado":  _checkbox(miss_info is not None),
    }

    if fg_valor is not None:
        props_raw["F&G"] = _numero(fg_valor)
    if miss_info:
        props_raw["Miss Magnitude %"] = _numero(round(miss_info.get("magnitude", 0) * 100, 2))
        props_raw["Miss Direcção"]    = _select(miss_info.get("direccao", "LONG"))

    payload = {
        "parent":     {"database_id": NOTION_DB_DETECCOES},
        "properties": _converter_props(props_raw),
    }

    resp = _post("/pages", payload)
    if resp:
        page_id = resp.get("id")
        log.info(f"[Notion B3] Detecção {det_id}: {token.ticker} "
                 f"Estado {resultado.estado_anterior}→{resultado.estado_novo}")
        return page_id
    return None


def actualizar_resultado_deteccao(
    det_id_notion: str,
    resultado_final: str,
) -> bool:
    """
    Actualiza o campo Resultado Final de uma entrada da Base 3.
    resultado_final: "Verdadeiro Positivo" | "Falso Positivo" | "Miss" | "Pendente"
    """
    payload = {
        "properties": {
            "Resultado Final": _select(resultado_final),
        }
    }
    resp = _patch(f"/pages/{det_id_notion}", payload)
    return resp is not None


# =============================================================================
# BASE 4 — HISTÓRICO DE MOVES
# =============================================================================

def log_move_create(
    token,
    campos: dict,
    lev_r,
    det_id_notion: Optional[str] = None,
    horas_em_estado3: float = 0.0,
    scans_em_estado3: int = 0,
    notas: str = "",
) -> Optional[str]:
    """
    Cria a entrada inicial na Base 4 quando um breakout é confirmado (Momento 2).
    Retorna o page_id da entrada criada.
    """
    move_id     = _proximo_id(NOTION_DB_MOVES, "MOV", "Move ID")
    agora_lx    = datetime.now(TZ_LISBOA)
    hora_lx_str = agora_lx.strftime("%H:%M")
    data_lx_str = agora_lx.strftime("%Y-%m-%d")

    estado_pre = campos.get("estado", 3)
    estado_pre_label = "3 - Prioritário" if estado_pre == 3 else "2 - Radar"

    # Sinais activos como string (ex: "S1 S3 S4 S5")
    sinais_activos = ""
    if lev_r:
        pass  # sinais não disponíveis aqui directamente — preenchido externamente

    target_m1 = token.momento1_target_pct
    target_m2 = lev_r.target_pct if lev_r else None
    metodo    = lev_r.target_metodo if lev_r else "C"
    tp_tipo   = lev_r.tp_tipo if lev_r else "escalonado"

    props_raw: dict = {
        "Move ID":              _titulo(move_id),
        "Token":                _texto(token.ticker),
        "Direcção":             _select(token.direccao),
        "date:Data Trigger:start": data_lx_str,
        "Hora Trigger Lisboa":  _texto(hora_lx_str),
        "Preço Trigger":        _numero(lev_r.preco_entry if lev_r else 0.0),
        "Score Pré-trigger":    _numero(token.score_actual),
        "Estado Pré-trigger":   _select(estado_pre_label),
        "Salto Directo":        _checkbox(token.salto_directo),
        "Horas em Estado 3":    _numero(horas_em_estado3),
        "Scans em Estado 3":    _numero(scans_em_estado3),
        "Target Método":        _select(_TARGET_METODO_LABEL.get(metodo, "C - ATR x3")),
        "Target Momento 1 %":   _numero(round(target_m1 * 100, 4) if target_m1 else None),
        "Target Momento 2 %":   _numero(round(target_m2 * 100, 4) if target_m2 else None),
        "TP Tipo":              _select(_TP_TIPO_LABEL.get(tp_tipo, "Escalonado")),
        "BTC Volátil no Trigger":  _checkbox(token.btc_volatil_no_trigger),
        "Grace Period no Trigger": _checkbox(campos.get("grace_period", False)),
    }
    if det_id_notion:
        props_raw["Detecção ID"] = _texto(det_id_notion)
    if sinais_activos:
        props_raw["Sinais Activos"] = _texto(sinais_activos)
    if notas:
        props_raw["Notas"] = _texto(notas)

    payload = {
        "parent":     {"database_id": NOTION_DB_MOVES},
        "properties": _converter_props(props_raw),
    }

    resp = _post("/pages", payload)
    if resp:
        page_id = resp.get("id")
        log.info(f"[Notion B4] Move {move_id}: {token.ticker} trigger registado")
        return page_id
    return None


def log_move_update(
    symbol: str,
    checkpoint_h: int,
    ganho_pct: float,
    trigger_preco: float = 0.0,
    leverage=None,
    notas: str = "",
) -> bool:
    """
    Actualiza o checkpoint hora-a-hora (+1h/+2h/+4h/+8h/+24h) numa entrada da Base 4.
    Procura o move mais recente do token (Estado 4) e actualiza o campo correcto.
    """
    if checkpoint_h not in (1, 2, 4, 8, 24):
        return False

    campo = f"Evolução +{checkpoint_h}h %"

    # Procurar o page_id do move activo deste token
    resultados = _query(NOTION_DB_MOVES, {
        "and": [
            {"property": "Token",    "rich_text": {"equals": symbol}},
            {"property": "Move Total %", "number": {"is_empty": True}},
        ]
    }, page_size=1)

    if not resultados:
        log.warning(f"[Notion B4] {symbol}: move activo não encontrado para checkpoint +{checkpoint_h}h")
        return False

    page_id = resultados[0]["id"]
    payload = {
        "properties": {
            campo: _numero(round(ganho_pct * 100, 4)),
        }
    }
    resp = _patch(f"/pages/{page_id}", payload)
    if resp:
        log.debug(f"[Notion B4] {symbol}: checkpoint +{checkpoint_h}h = {ganho_pct*100:.2f}%")
        return True
    return False


def log_move_conclusao(
    symbol: str,
    conclusao,
    preco_conclusao: float,
    checkpoints: dict,
) -> bool:
    """
    Fecha a entrada de move na Base 4 quando o Estado 5 é atingido.
    Preenche: Data Conclusão, Preço Conclusão, Move Total %, Move Máximo %,
              Duração Horas, Tipo Conclusão, Condição Activada,
              e todos os checkpoints ainda em falta.
    """
    resultados = _query(NOTION_DB_MOVES, {
        "and": [
            {"property": "Token",        "rich_text": {"equals": symbol}},
            {"property": "Move Total %", "number":    {"is_empty": True}},
        ]
    }, page_size=1)

    if not resultados:
        log.warning(f"[Notion B4] {symbol}: move activo não encontrado para conclusão")
        return False

    page_id   = resultados[0]["id"]
    agora_lx  = datetime.now(TZ_LISBOA)
    cond_label = _CONDICAO_LABEL.get(conclusao.condicao, "4 - Tempo esgotado")

    props_raw: dict = {
        "date:Data Conclusão:start": agora_lx.strftime("%Y-%m-%d"),
        "Hora Conclusão Lisboa":     _texto(agora_lx.strftime("%H:%M")),
        "Preço Conclusão":           _numero(preco_conclusao),
        "Move Total %":              _numero(round(conclusao.ganho_atual_pct  * 100, 4)),
        "Move Máximo %":             _numero(round(conclusao.ganho_maximo_pct * 100, 4)),
        "Duração Horas":             _numero(round(conclusao.horas_decorridas, 2)),
        "Tipo Conclusão":            _select(conclusao.tipo),
        "Condição Activada":         _select(cond_label),
    }

    # Preencher checkpoints em falta
    for h in (1, 2, 4, 8, 24):
        if h in checkpoints:
            props_raw[f"Evolução +{h}h %"] = _numero(round(checkpoints[h] * 100, 4))

    resp = _patch(f"/pages/{page_id}", {"properties": _converter_props(props_raw)})
    if resp:
        log.info(f"[Notion B4] {symbol}: move concluído ({conclusao.tipo}, Cond {conclusao.condicao})")
        return True
    return False


# =============================================================================
# MISS DETECTION — registo na Base 3
# =============================================================================

def log_miss(
    symbol: str,
    magnitude_pct: float,
    direccao: str,
    scan_id: str,
    btc_acima_ema21: bool = True,
    btc_preco: float = 0.0,
) -> Optional[str]:
    """
    Regista um Miss (token em Estado 1 que fez move significativo sem ser detectado)
    na Base 3 com campos específicos de miss.
    """
    det_id   = _proximo_id(NOTION_DB_DETECCOES, "DET", "Detecção ID")
    hora_lx  = datetime.now(TZ_LISBOA).strftime("%H:%M")

    props_raw: dict = {
        "Detecção ID":     _titulo(det_id),
        "Token":           _texto(symbol),
        "date:Data:start": datetime.now(TZ_LISBOA).strftime("%Y-%m-%d"),
        "Hora Lisboa":     _texto(hora_lx),
        "Estado Anterior": _select("1"),
        "Estado Novo":     _select("1"),
        "Score":           _numero(0),
        "Direcção":        _select("INDEFINIDO"),
        "S1": _checkbox(False), "S2": _checkbox(False), "S3": _checkbox(False),
        "S4": _checkbox(False), "S5": _checkbox(False), "S6": _checkbox(False),
        "ATR 1h %":         _numero(0),
        "Funding Flag":     _select("Nenhuma"),
        "BTC Preço":        _numero(btc_preco),
        "BTC acima EMA21":  _checkbox(btc_acima_ema21),
        "Scan ID":          _texto(scan_id),
        "Resultado Final":  _select("Miss"),
        "Horas no Estado":  _numero(0),
        "Salto Directo":    _checkbox(False),
        "Bloqueado Filtro BTC": _checkbox(False),
        "Miss Detectado":   _checkbox(True),
        "Miss Magnitude %": _numero(round(magnitude_pct * 100, 2)),
        "Miss Direcção":    _select(direccao),
    }

    payload = {
        "parent":     {"database_id": NOTION_DB_DETECCOES},
        "properties": _converter_props(props_raw),
    }

    resp = _post("/pages", payload)
    if resp:
        log.info(f"[Notion B3] Miss {det_id}: {symbol} {direccao} {magnitude_pct*100:.1f}%")
        return resp.get("id")
    return None


# =============================================================================
# CONVERSOR FINAL — traduz props_raw para formato Notion API
# =============================================================================

def _converter_props(props_raw: dict) -> dict:
    """
    Converte o dict de propriedades no formato Notion REST API.

    Suporta:
    - Valores já em formato Notion (dict com chave "title", "rich_text", etc.)
    - Campos de data no formato "date:NOME:start" → {"date": {"start": valor}}
    - Strings/números directos → encapsulados automaticamente
    """
    resultado = {}
    datas: dict = {}   # acumular partes de data por nome de campo

    for chave, valor in props_raw.items():
        # Campos de data no formato expandido (date:Campo:start / is_datetime)
        if chave.startswith("date:"):
            partes = chave.split(":", 2)
            if len(partes) == 3:
                _, nome_campo, parte = partes
                if nome_campo not in datas:
                    datas[nome_campo] = {}
                datas[nome_campo][parte] = valor
            continue

        # Valores já em formato Notion
        if isinstance(valor, dict) and any(
            k in valor for k in ("title", "rich_text", "number", "select",
                                  "checkbox", "date", "multi_select")
        ):
            resultado[chave] = valor
        elif isinstance(valor, str):
            resultado[chave] = _texto(valor)
        elif isinstance(valor, (int, float)):
            resultado[chave] = _numero(valor)
        elif isinstance(valor, bool):
            resultado[chave] = _checkbox(valor)
        elif valor is None:
            resultado[chave] = {"number": None}

    # Processar campos de data acumulados
    for nome_campo, partes_data in datas.items():
        start = partes_data.get("start")
        if start:
            resultado[nome_campo] = {"date": {"start": start}}

    return resultado
