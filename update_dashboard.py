"""
update_dashboard.py
====================
Atualiza o DAILY_DATA (BigQuery) e o PLAN_STATIC (Google Sheets) do dashboard.
Roda automaticamente via Task Scheduler / GitHub Actions.

Dependências: google-cloud-bigquery, db-dtypes, google-api-python-client, google-auth
Auth: service account JSON (SA_KEY_PATH) ou application-default como fallback
"""

import json
import os
import re
import subprocess
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import bigquery

# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────

# Suporte a GitHub Actions: usa variáveis de ambiente se disponíveis
_LOCAL_DASHBOARD = Path(r"C:\Users\gfuretti\Documents\Claude\Projects\Planos de Capacidade\dashboard_cap_noco_ne_v2.html")
DASHBOARD_PATH = Path(os.environ.get("DASHBOARD_PATH", str(_LOCAL_DASHBOARD)))
LOG_PATH       = DASHBOARD_PATH.parent / "update_dashboard.log"
BQ_PROJECT     = "meli-sbox"

# Service account: env var SA_KEY_PATH tem prioridade (usada no GitHub Actions)
_LOCAL_SA_KEY  = Path(r"C:\Users\gfuretti\Documents\Claude\Projects\Planos de Capacidade\sa_key.json")
SA_KEY_PATH    = Path(os.environ.get("SA_KEY_PATH", str(_LOCAL_SA_KEY)))

# ── Google Sheets — Plano de Capacidade ──────────────────
SHEETS_ID    = "19a80ydJ-NFKG4GRRJZibM-k6TQo4qBYBkrpY8mlKZFM"
SHEETS_RANGE = "Planos CAP!A:I"  # INDICADOR DIA|CICLO|SVC/XPTS|TIPO FROTA|MODAL|ROTAS|SPR|VOLUME|DATA

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

def get_credentials():
    """
    Retorna credenciais priorizando service account.
    Fallback: application-default (útil para desenvolvimento local).
    """
    key_path = Path(os.environ.get("SA_KEY_PATH", str(SA_KEY_PATH)))
    if key_path.exists():
        log.info(f"  Auth: service account ({key_path.name})")
        return service_account.Credentials.from_service_account_file(
            str(key_path), scopes=SCOPES
        )
    log.info("  Auth: application-default credentials (fallback)")
    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def _gerar_semanas(ano: int) -> list[tuple]:
    """
    Gera automaticamente todas as semanas do ano (domingo a sábado).
    Semana 1 começa no domingo da semana que contém 1º de janeiro.
    Exemplo: Jan 1 2026 = quinta → domingo dessa semana = 28/12/2025 → Sem 1.
    """
    jan1 = date(ano, 1, 1)
    # Volta até o domingo imediatamente anterior (ou o próprio Jan 1 se for domingo)
    days_back = (jan1.weekday() + 1) % 7  # weekday: Seg=0 … Dom=6
    base = jan1 - timedelta(days=days_back)
    semanas = []
    for w in range(1, 54):          # até 53 semanas
        start = base + timedelta(weeks=w - 1)
        end   = start + timedelta(days=6)
        # Para quando a semana já pertence completamente ao ano seguinte
        if start.year > ano and w > 52:
            break
        semanas.append((f"Sem {w}", start, end))
    return semanas

# Gerado dinamicamente — nenhuma edição manual necessária ao virar semana
SEMANAS = _gerar_semanas(date.today().year)

# Semanas a incluir no dashboard (as últimas 8 com dados)
MAX_SEMANAS = 8

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# GOOGLE SHEETS — PLANO DE CAPACIDADE
# ─────────────────────────────────────────────

def _parse_br_number(val: str) -> int:
    """Converte número brasileiro ('1.234,5' ou '1234') para int."""
    if not val or not str(val).strip():
        return 0
    v = str(val).strip().replace(".", "").replace(",", ".")
    try:
        return int(float(v))
    except ValueError:
        return 0


def ler_plano_sheets() -> list[dict]:
    """
    Lê o Plano de Capacidade direto do Google Sheets usando as credenciais
    application-default já configuradas para BigQuery.
    Retorna lista de dicts {saida_lm, tipo_frota, modal, ciclo, rotas, volume}.
    """
    log.info("Lendo Plano de Capacidade do Google Sheets...")
    creds   = get_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEETS_ID, range=SHEETS_RANGE)
        .execute()
    )
    rows = result.get("values", [])
    if not rows:
        raise ValueError("Planilha retornou vazia!")

    # Localiza cabeçalho (primeira linha com "SVC" ou "SAIDA")
    header_idx = 0
    for i, row in enumerate(rows):
        joined = " ".join(row).upper()
        if "SVC" in joined or "SAIDA" in joined:
            header_idx = i
            break

    header = [c.strip().upper() for c in rows[header_idx]]
    log.info(f"  Cabeçalho encontrado na linha {header_idx + 1}: {header}")

    # Índices das colunas relevantes
    def col(name):
        for i, h in enumerate(header):
            if name in h:
                return i
        return None

    i_ciclo   = col("CICLO")
    i_saida   = col("SVC")   or col("SAIDA")
    i_frota   = col("TIPO")
    i_modal   = col("MODAL")
    i_rotas   = col("ROTAS")
    i_volume  = col("VOLUME")

    if any(x is None for x in [i_saida, i_frota, i_modal, i_rotas, i_volume]):
        raise ValueError(f"Colunas obrigatórias não encontradas. Header: {header}")

    plano = []
    for row in rows[header_idx + 1:]:
        # Para de processar ao atingir tabela secundária ou linha em branco
        if not row or len(row) <= max(i_saida, i_modal):
            continue
        saida = str(row[i_saida]).strip() if i_saida < len(row) else ""
        modal = str(row[i_modal]).strip() if i_modal < len(row) else ""
        if not saida or not modal or saida.upper() in ("SVC/XPTS", "MODAL AGRUPADO"):
            continue

        rotas  = _parse_br_number(row[i_rotas]  if i_rotas  < len(row) else "")
        volume = _parse_br_number(row[i_volume] if i_volume < len(row) else "")

        # Ignora linhas sem valor
        if rotas == 0 and volume == 0:
            continue

        frota = str(row[i_frota]).strip().upper() if i_frota < len(row) else ""
        ciclo = str(row[i_ciclo]).strip().upper() if i_ciclo is not None and i_ciclo < len(row) else "AM1"

        plano.append({
            "saida_lm":  saida,
            "tipo_frota": frota,
            "modal":      modal,
            "ciclo":      ciclo,
            "rotas":      rotas,
            "volume":     volume,
        })

    log.info(f"  Linhas do plano carregadas: {len(plano)}")
    return plano


def build_plan_static_js(plano: list[dict]) -> str:
    """Gera o bloco JS da constante PLAN_STATIC."""
    jd = lambda v: json.dumps(v, ensure_ascii=False)
    linhas = [
        f"  {{saida_lm:{jd(p['saida_lm'])},tipo_frota:{jd(p['tipo_frota'])},"
        f"modal:{jd(p['modal'])},ciclo:{jd(p['ciclo'])},"
        f"rotas:{p['rotas']},volume:{p['volume']}}}"
        for p in plano
    ]
    return "const PLAN_STATIC = [\n" + ",\n".join(linhas) + "\n];"


# ─────────────────────────────────────────────
# FUNÇÕES DE MAPEAMENTO
# ─────────────────────────────────────────────

def get_semana(d: date) -> str | None:
    """Retorna o label da semana interna (ex: 'Sem 19') para uma data."""
    for label, start, end in SEMANAS:
        if start <= d <= end:
            return label
    return None


def get_tipo_frota(modal: str) -> str:
    """Classifica o modal em tipo_frota conforme regra de negócio."""
    m = modal.upper()
    if "RENTAL" in m:          return "FROTA FIXA"
    if "KANGU" in m:           return "KANGU SPOT"
    if "SDD" in m:             return "SDD"
    if "NEX" in m or m == "DC": return "NODOS"
    if "EXTRA" in m:           return "ENVIOS EXTRA"
    return "SPOT"


# ─────────────────────────────────────────────
# QUERY BIGQUERY
# ─────────────────────────────────────────────

QUERY = """
SELECT
  DATE(LM.DATA) AS DATA,
  (CASE
    WHEN LM.SAIDA_LM LIKE 'BRN%' THEN LM.SVC_ORIGEM
    WHEN LM.SAIDA_LM LIKE 'BRD%' THEN LM.SVC_ORIGEM
    WHEN LM.SAIDA_LM LIKE 'M%'   THEN LM.SVC_ORIGEM
    WHEN LM.SAIDA_LM IS NULL      THEN LM.SVC_ORIGEM
    ELSE LM.SAIDA_LM
  END) AS SAIDA_LM,
  (CASE
    WHEN LM.VEICULO IN ('CAVALO') THEN 'N/A'
    WHEN LM.SAIDA_LM LIKE 'BRN%' THEN 'NEX'
    WHEN LM.SAIDA_LM LIKE 'BRD%' THEN 'DC'
    WHEN LM.VEICULO IN ('MOTO EXTRA','MOTOCICLETA','MOTO CROWD')
         AND LM.MLP IN ('Envios Extra','MELI EXTRA') THEN 'MOTO EXTRA'
    WHEN LM.VEICULO IN ('MOTO CROWD') THEN 'MOTO EXTRA'
    WHEN LM.VEICULO IN (
         'VEÍCULO DE PASSEIO 6HS - VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 6H',
         'VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 4H','VEICULO DE PASSEIO EXTRA 6H')
         AND LM.MLP IN ('Envios Extra','MELI EXTRA',
         'VEÍCULO DE PASSEIO 6HS - VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 4H',
         'VEICULO DE PASSEIO EXTRA 6H') THEN 'PASSEIO EXTRA'
    WHEN LM.VEICULO IN ('UTILITÁRIOS','UTILITARIO EXTRA 6H')
         AND LM.MLP IN ('Envios Extra','MELI EXTRA') THEN 'UTILITÁRIO EXTRA'
    WHEN LM.VEICULO IN ('VUC MELI EXTRA - VUC','VUC','HR','HR SDD','VUC PP ( ATÉ 10 M3)')
         AND LM.MLP IN ('Envios Extra','MELI EXTRA') THEN 'VUC EXTRA'
    WHEN LM.VEICULO IN ('VAN')
         AND LM.MLP IN ('Envios Extra','MELI EXTRA') THEN 'VAN EXTRA'
    WHEN LM.VEICULO IN ('MOTOCICLETA','TRICICLOS','BIKE')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'MOTO SPOT'
    WHEN LM.VEICULO IN ('CARRO','VEÍCULO DE PASSEIO','PASSEIO SPOT NODO ORH4',
         'EPASSEIO PRÓPRIO','VEÍCULO DE PASSEIO SPOT_FIXO','PASSEIO XDSD FM',
         'VEICULO DE PASSEIO EXTRA 8H','VEICULO DE PASSEIO EXTRA 6H')
         AND LM.MLP NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'PASSEIO SPOT'
    WHEN LM.VEICULO IN ('FIORINO','UTILITÁRIOS','UTILITARIO FIXO','UTILITARIO EXTRA 6H')
         AND LM.MLP NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'UTILITÁRIO SPOT'
    WHEN LM.VEICULO IN ('VEÍCULO URBANO COMPACTO','3/4/MÉDIO','CARRETA','HR','HR FM DD',
         'M1 - VUC','MÉDIO','TOCO','TRUCK','VUC BULKY','VUC','VUC FIXO','VUC PP ( ATÉ 10 M3)',
         'VUC G (ACIMA DE 18M3)','BULK - VAN_HR EQUIPE DUPLA POOL','BULK - VAN_HR EQUIPE ÚNICA POOL',
         'BULK - VUC EQUIPE DUPLA DEDICADO','BULK - VUC EQUIPE DUPLA POOL',
         'BULK - VUC EQUIPE ÚNICA DEDICADO','BULK - VUC EQUIPE ÚNICA POOL',
         'BULK - VUC PP','VUC LM 2026')
         AND LM.MLP NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'VUC SPOT'
    WHEN LM.VEICULO IN ('VAN','VAN FIXO','VAN L ( DE 10 A 15M3)')
         AND LM.MLP NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'VAN SPOT'
    WHEN LM.VEICULO IN ('VUC SDD','VUC PP SDD')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'VUC SDD'
    WHEN LM.VEICULO IN ('UTILITÁRIOS SDD')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'UTILITÁRIOS SDD'
    WHEN LM.VEICULO IN ('VAN SDD','VAN L _SDD')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'VAN SDD'
    WHEN LM.VEICULO IN ('HR SDD')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'HR SDD'
    WHEN LM.VEICULO IN ('FIORINO','UTILITÁRIOS','UTILITARIO FIXO')
         AND LM.MLP IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'UTILITÁRIO KANGU'
    WHEN LM.VEICULO IN ('VAN','VAN FIXO','VAN L ( DE 10 A 15M3)')
         AND LM.MLP IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'VAN KANGU'
    WHEN LM.VEICULO IN ('CARRO','VEÍCULO DE PASSEIO')
         AND LM.MLP IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'PASSEIO KANGU'
    WHEN LM.VEICULO IN ('VEÍCULO URBANO COMPACTO','3/4/MÉDIO','CARRETA','HR','HR FM DD',
         'MÉDIO','TOCO','TRUCK','VUC BULKY','VUC','VUC FIXO','VUC PP ( ATÉ 10 M3)',
         'VUC G (ACIMA DE 18M3)')
         AND LM.MLP IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'VUC KANGU'
    WHEN LM.VEICULO IN (
         'E-UTILITÁRIO LAST MILE','MELIONE RENTAL UTILITÁRIO COM AJUDANTE',
         'RENTAL IHDS ELECTRIC 2P','RENTAL IHDS ELECTRIC 5P','RENTAL IHDS UTITLITY',
         'RENTAL UTILITÁRIO COM AJUDANTE','RENTAL UTILITÁRIO SEM AJUDANTE',
         'UTILITÁRIO ELÉTRICO FROTA FIXA','MELIONE UTILITÁRIO AGREGADO',
         'UTILITÁRIO LOCALIZA 2025','UTILITÁRIO ELÉTRICO BYD','UTILITÁRIO ARVAL 2025',
         'UTILITÁRIO FROTA FIXA NEPO','UTILITÁRIO VAMOS 2025','UTILITÁRIO TKS 2025',
         'UTILITÁRIO FROTA FIXA FADEL','UTILITÁRIO TKS 2025 - NEWBIE',
         'MELIONE RENTAL UTILITÁRIO COM AJUDANTE') THEN 'RENTAL UTILITÁRIO'
    WHEN LM.VEICULO IN (
         'ARROW','E-VAN MÉDIA - EQUIPE ÚNICA','MELIONE ESPECIAL','MELIONE RENTAL VAN',
         'MELIONE VAN','MELIONE VAN FROTA FIXA','MELIONE YELLOW POOL','MELIONE VAN AGREGADO',
         'MELIONE VAN MÉDIA ELÉTRICA','RENTAL IHDS LARGE VAN',
         'RENTALS LARGE VAN – EQUIPE DUPLA','RENTALS LARGE VAN – EQUIPE ÚNICA',
         'VAN ELÉTRICA PRÓPRIA','VAN FROTA FIXA - EQUIPE DUPLA','VAN FROTA FIXA - EQUIPE ÚNICA',
         'VAN MÉDIA ELÉTRICA','YELLOW POOL LARGE VAN – EQUIPE DUPLA',
         'YELLOW POOL LARGE VAN – EQUIPE ÚNICA','EVAN PRÓPRIA','ARROW 1 HELPER',
         'ARROW MELIONE','ARROW KN','FROTA FIXA LARGE MASTER','FROTA FIXA LARGE VAN FORD AUTO',
         'VAN FROTA FIXA DEDICADO','FROTA FIXA LARGE VAN FORD','VAN TKS 2025',
         'MELIONE VAN FROTA FIXA DEDICADO','VAN ELÉTRICA JV','LARGE VAN ELETRICA J750',
         'VAN VAMOS 2025','MELIONE VAN VAMOS 2025','LARGE VAN ELÉTRICA - EQUIPE ÚNICA',
         'MELIONE VAN TKS 2025','MELIONE LARGE VAN ELETRICA J750','VAN TKS 2025 H2',
         'VAN VAMOS 2025 H2','MELIONE VAN TKS 2025 H2','MELIONE VAN VAMOS 2025 H2',
         'LARGE VAN ELÉTRICA','LARGE VAN ELÉTRICA - EQUIPE ÚNICA - LOCALIZA') THEN 'RENTAL VAN'
    WHEN LM.VEICULO IN (
         'M1 RENTAL MEDIO 31 DD*FM','M1 RENTAL MEDIO 37 DD*FM','M1 RENTAL VUC DD*FM',
         'MELIONE HR','MELIONE VUC','MELIONE - MÉDIO','MELIONE VUC AGREGADO',
         'VUC DEDICADO COM AJUDANTE','VUC DEDICADO COM AJUDANTE SEM TELEMETRIA',
         'VUC ELÉTRICO','MEDIO FM DD','MELIONE VUC DEDICADO','M1 - MÉDIO',
         'VUC RENTAL TKS','VUC TKS 2025','MELIONE VUC RENTAL TKS','VUC VAMOS 2025',
         'MELIONE VUC TKS 2025','VUC RENTAL','VUC ELÉTRICO DELIVERY','MELIONE VUC ELÉTRICO',
         'RENTAL VUC FM','VUC VAMOS 2025 H2','VUC DEDICADO FBM 7K',
         'MELIONE VUC VAMOS 2025 H2','VUC DEDICADO FBM 4K','VUC VAMOS 2025 - NEWBIE',
         'VUC TKS 2025 - NEWBIE','MELIONE VUC VAMOS 2025','VUC G - ELÉTRICO DELIVERY 2E',
         'VUC G - ELÉTRICO DELIVERY 3E','VUC DEDICADO COM AJUDANTE - NEWBIE',
         'MELIONE VUC LM 2026','CARRETA FM DD','MELIONE VUC') THEN 'RENTAL VUC'
    WHEN LM.VEICULO IN ('WALKER')
         AND LM.MLP NOT IN ('Envios Extra','MELI EXTRA') THEN 'WALKER'
    ELSE LM.VEICULO
  END) AS VEICULO_AGRUPADO,
  (CASE
    WHEN LM.SAIDA_LM LIKE 'E%' THEN 'AM1'
    WHEN LM.CICLO IN ('AM0','AM2','AMB','AMDE','AM0V','AM') THEN 'AM1'
    WHEN LM.SVC_ORIGEM IN ('SPA1','SRD1','SAM1','STO1','SBA2','SBA3',
         'SMR2','SMS1','SMS2','SSE1')
         AND LM.SAIDA_LM IS NULL THEN 'AM1'
    WHEN LM.CICLO IS NULL THEN
         CASE WHEN CAST(FORMAT_DATETIME('%H', DATA) AS NUMERIC) >= 13
              THEN 'PM1' ELSE 'AM1' END
    WHEN LM.CICLO IN ('SD') THEN 'SD'
    WHEN LM.CICLO IN ('CHP')
         AND LM.SVC_ORIGEM IN ('SAL1') THEN 'PM1'
    ELSE LM.CICLO
  END) AS CICLO,
  SUM(LM.ROTAS_DESP)        AS ROTAS,
  SUM(LM.PCTS_DESPACHADOS)  AS VOLUME

FROM `meli-sbox.PLANOPSLM.REAL_PLAN_OPS_TOTAL` AS LM

WHERE DATE(LM.DATA) BETWEEN DATE_SUB(CURRENT_DATE, INTERVAL 60 DAY) AND CURRENT_DATE

GROUP BY ALL
"""


# ─────────────────────────────────────────────
# LÓGICA DE SEMANAS ATIVAS
# ─────────────────────────────────────────────

def semanas_ativas() -> list[tuple]:
    """
    Retorna as semanas que têm dados disponíveis (data de início <= hoje)
    e limita a MAX_SEMANAS semanas mais recentes.
    """
    hoje = date.today()
    disponiveis = [(l, s, e) for l, s, e in SEMANAS if s <= hoje]
    return disponiveis[-MAX_SEMANAS:]


def build_semanas_js(semanas: list[tuple]) -> str:
    """Gera o bloco JS da constante SEMANAS."""
    linhas = []
    for label, start, end in semanas:
        linhas.append(
            f"  {{label:'{label}',startDate:'{start.isoformat()}',endDate:'{end.isoformat()}'}}"
        )
    return "const SEMANAS = [\n" + ",\n".join(linhas) + "\n];"


# ─────────────────────────────────────────────
# PROCESSAMENTO DOS DADOS
# ─────────────────────────────────────────────

def processar_linhas(rows, semanas_validas: set[str]) -> list[str]:
    """Converte linhas do BQ em strings JS para o DAILY_DATA."""
    js_lines = []
    skipped_sem = skipped_modal = 0

    for row in rows:
        date_str = row["DATA"].isoformat()
        d = row["DATA"]
        modal = row["VEICULO_AGRUPADO"] or ""
        saida = row["SAIDA_LM"] or ""
        ciclo = row["CICLO"] or ""
        rotas = int(row["ROTAS"] or 0)
        volume = int(row["VOLUME"] or 0)

        # Descartar modais sem mapeamento útil
        if modal in ("N/A", "", "WALKER") or not modal:
            skipped_modal += 1
            continue

        semana = get_semana(d)
        if semana not in semanas_validas:
            skipped_sem += 1
            continue

        tipo_frota = get_tipo_frota(modal)

        jd = lambda v: json.dumps(v, ensure_ascii=False)
        js_lines.append(
            f'  {{date:{jd(date_str)},semana:{jd(semana)},'
            f'saida_lm:{jd(saida)},modal:{jd(modal)},'
            f'tipo_frota:{jd(tipo_frota)},ciclo:{jd(ciclo)},'
            f'rotas:{rotas},volume:{volume}}}'
        )

    log.info(f"  Registros JS gerados : {len(js_lines):,}")
    log.info(f"  Ignorados (sem semana): {skipped_sem:,}")
    log.info(f"  Ignorados (modal N/A) : {skipped_modal:,}")
    return js_lines


# ─────────────────────────────────────────────
# INJEÇÃO NO HTML
# ─────────────────────────────────────────────

def injetar_html(html: str, js_lines: list[str], semanas: list[tuple],
                 plano: list[dict]) -> str:
    """Substitui DAILY_DATA, SEMANAS e PLAN_STATIC no HTML."""

    # 1. DAILY_DATA
    novo_daily = "const DAILY_DATA = [\n" + ",\n".join(js_lines) + "\n];"
    html, n1 = re.subn(
        r'const DAILY_DATA = \[[\s\S]*?\];',
        lambda m: novo_daily,
        html
    )
    if n1 == 0:
        raise ValueError("Padrão 'const DAILY_DATA' não encontrado no HTML!")

    # 2. SEMANAS
    novo_semanas = build_semanas_js(semanas)
    html, n2 = re.subn(
        r'const SEMANAS = \[[\s\S]*?\];',
        lambda m: novo_semanas,
        html
    )
    if n2 == 0:
        log.warning("Padrão 'const SEMANAS' não encontrado — mantendo original.")

    # 3. PLAN_STATIC
    novo_plan = build_plan_static_js(plano)
    html, n3 = re.subn(
        r'const PLAN_STATIC = \[[\s\S]*?\];',
        lambda m: novo_plan,
        html
    )
    if n3 == 0:
        log.warning("Padrão 'const PLAN_STATIC' não encontrado — mantendo original.")
    else:
        log.info(f"  PLAN_STATIC atualizado ({len(plano)} linhas)")

    return html


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"Iniciando atualização — {datetime.now():%d/%m/%Y %H:%M:%S}")
    log.info("=" * 60)

    # 1. Semanas ativas
    sems = semanas_ativas()
    labels = [s[0] for s in sems]
    log.info(f"Semanas ativas: {labels}")

    # 2. Verificar arquivo HTML
    if not DASHBOARD_PATH.exists():
        log.error(f"HTML não encontrado: {DASHBOARD_PATH}")
        sys.exit(1)

    # 3. Ler Plano de Capacidade do Google Sheets
    try:
        plano = ler_plano_sheets()
    except Exception as e:
        log.error(f"Erro ao ler Google Sheets: {e}")
        log.warning("Continuando sem atualizar PLAN_STATIC...")
        plano = []

    # 4. Query BigQuery
    log.info(f"Conectando ao BigQuery (projeto: {BQ_PROJECT})...")
    client = bigquery.Client(project=BQ_PROJECT, credentials=get_credentials())

    log.info("Executando query...")
    job = client.query(QUERY)
    rows = list(job.result())
    log.info(f"  Linhas retornadas do BQ: {len(rows):,}")

    # 5. Processar diário
    semanas_validas = set(labels)
    js_lines = processar_linhas(rows, semanas_validas)

    if not js_lines:
        log.error("Nenhum registro gerado — abortando para não limpar o dashboard.")
        sys.exit(1)

    # 6. Ler, injetar e salvar HTML
    log.info("Lendo HTML...")
    html = DASHBOARD_PATH.read_text(encoding="utf-8")

    log.info("Injetando dados...")
    html_novo = injetar_html(html, js_lines, sems, plano)

    # Backup rápido (sobrescreve o backup anterior — .replace() funciona no Windows)
    backup = DASHBOARD_PATH.with_suffix(".html.bak")
    DASHBOARD_PATH.replace(backup)

    DASHBOARD_PATH.write_text(html_novo, encoding="utf-8")
    log.info(f"HTML salvo: {DASHBOARD_PATH}")
    log.info(f"Backup em : {backup}")

    # 6. Publicar no GitHub Pages (apenas fora do GitHub Actions — lá o workflow faz o git)
    if os.environ.get("GITHUB_ACTIONS"):
        log.info("GitHub Actions detectado — git push será feito pelo workflow.")
    else:
        log.info("Publicando no GitHub Pages...")
        repo_dir = str(DASHBOARD_PATH.parent)
        hoje_str = datetime.now().strftime("%d/%m/%Y %H:%M")

        def git(args: list[str]) -> str:
            result = subprocess.run(
                ["git"] + args,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)} falhou:\n{result.stderr.strip()}")
            return result.stdout.strip()

        try:
            git(["add", "dashboard_cap_noco_ne_v2.html"])
            git(["commit", "-m", f"chore: atualização automática DAILY_DATA - {hoje_str}"])
            git(["push", "origin", "main"])
            log.info("  Dashboard publicado com sucesso no GitHub Pages!")
        except RuntimeError as e:
            log.warning(f"  GitHub push não realizado (pode não haver mudança): {e}")

    log.info("Atualização concluída com sucesso!")


if __name__ == "__main__":
    main()
