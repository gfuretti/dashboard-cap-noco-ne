"""
update_dashboard.py
====================
Atualiza o DAILY_DATA do dashboard_cap_noco_ne_v2.html com dados frescos do BigQuery.
Roda automaticamente via Task Scheduler todo dia de manhã.

Dependências: google-cloud-bigquery, db-dtypes
Auth: gcloud auth application-default login (já configurado)
"""

import json
import re
import subprocess
import sys
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from google.cloud import bigquery

# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────

DASHBOARD_PATH = Path(r"C:\Users\gfuretti\Documents\Claude\Projects\Planos de Capacidade\dashboard_cap_noco_ne_v2.html")
LOG_PATH       = DASHBOARD_PATH.parent / "update_dashboard.log"
BQ_PROJECT     = "meli-bi-data"

# Semanas ativas — atualizar conforme necessário
# Formato: (label, data_inicio, data_fim)
SEMANAS = [
    ("Sem 14", date(2026, 3, 29), date(2026, 4,  4)),
    ("Sem 15", date(2026, 4,  5), date(2026, 4, 11)),
    ("Sem 16", date(2026, 4, 12), date(2026, 4, 18)),
    ("Sem 17", date(2026, 4, 19), date(2026, 4, 25)),
    ("Sem 18", date(2026, 4, 26), date(2026, 5,  2)),
    ("Sem 19", date(2026, 5,  3), date(2026, 5,  9)),
    ("Sem 20", date(2026, 5, 10), date(2026, 5, 16)),
    ("Sem 21", date(2026, 5, 17), date(2026, 5, 23)),
]

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
  DATE(LM.SHP_LG_INIT_DT_TZ) AS DATA,
  (CASE
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'BRN%' THEN LM.SHP_LG_FACILITY_ID
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'BRD%' THEN LM.SHP_LG_FACILITY_ID
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'M%'   THEN LM.SHP_DESTINATION_FACILITY_ID
    WHEN LM.SHP_DESTINATION_FACILITY_ID IS NULL      THEN LM.SHP_LG_FACILITY_ID
    ELSE LM.SHP_DESTINATION_FACILITY_ID
  END) AS SAIDA_LM,
  (CASE
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('CAVALO') THEN 'N/A'
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'BRN%' THEN 'NEX'
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'BRD%' THEN 'DC'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('MOTO EXTRA','MOTOCICLETA','MOTO CROWD')
         AND LM.SHP_COMPANY_NAME IN ('Envios Extra','MELI EXTRA') THEN 'MOTO EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('MOTO CROWD') THEN 'MOTO EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN (
         'VEÍCULO DE PASSEIO 6HS - VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 6H',
         'VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 4H','VEICULO DE PASSEIO EXTRA 6H')
         AND LM.SHP_COMPANY_NAME IN ('Envios Extra','MELI EXTRA',
         'VEÍCULO DE PASSEIO 6HS - VEÍCULO DE PASSEIO','VEICULO DE PASSEIO EXTRA 4H',
         'VEICULO DE PASSEIO EXTRA 6H') THEN 'PASSEIO EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('UTILITÁRIOS','UTILITARIO EXTRA 6H')
         AND LM.SHP_COMPANY_NAME IN ('Envios Extra','MELI EXTRA') THEN 'UTILITÁRIO EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VUC MELI EXTRA - VUC','VUC','HR','HR SDD','VUC PP ( ATÉ 10 M3)')
         AND LM.SHP_COMPANY_NAME IN ('Envios Extra','MELI EXTRA') THEN 'VUC EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VAN')
         AND LM.SHP_COMPANY_NAME IN ('Envios Extra','MELI EXTRA') THEN 'VAN EXTRA'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('MOTOCICLETA','TRICICLOS','BIKE')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'MOTO SPOT'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('CARRO','VEÍCULO DE PASSEIO','PASSEIO SPOT NODO ORH4',
         'EPASSEIO PRÓPRIO','VEÍCULO DE PASSEIO SPOT_FIXO','PASSEIO XDSD FM',
         'VEICULO DE PASSEIO EXTRA 8H','VEICULO DE PASSEIO EXTRA 6H')
         AND LM.SHP_COMPANY_NAME NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'PASSEIO SPOT'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('FIORINO','UTILITÁRIOS','UTILITARIO FIXO','UTILITARIO EXTRA 6H')
         AND LM.SHP_COMPANY_NAME NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'UTILITÁRIO SPOT'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VEÍCULO URBANO COMPACTO','3/4/MÉDIO','CARRETA','HR','HR FM DD',
         'M1 - VUC','MÉDIO','TOCO','TRUCK','VUC BULKY','VUC','VUC FIXO','VUC PP ( ATÉ 10 M3)',
         'VUC G (ACIMA DE 18M3)','BULK - VAN_HR EQUIPE DUPLA POOL','BULK - VAN_HR EQUIPE ÚNICA POOL',
         'BULK - VUC EQUIPE DUPLA DEDICADO','BULK - VUC EQUIPE DUPLA POOL',
         'BULK - VUC EQUIPE ÚNICA DEDICADO','BULK - VUC EQUIPE ÚNICA POOL',
         'BULK - VUC PP','VUC LM 2026')
         AND LM.SHP_COMPANY_NAME NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'VUC SPOT'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VAN','VAN FIXO','VAN L ( DE 10 A 15M3)')
         AND LM.SHP_COMPANY_NAME NOT IN ('Kangu Logistics','KANGU LOGISTICS','Envios Extra','MELI EXTRA')
         THEN 'VAN SPOT'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VUC SDD','VUC PP SDD')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'VUC SDD'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('UTILITÁRIOS SDD')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'UTILITÁRIOS SDD'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VAN SDD','VAN L _SDD')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'VAN SDD'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('HR SDD')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'HR SDD'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('FIORINO','UTILITÁRIOS','UTILITARIO FIXO')
         AND LM.SHP_COMPANY_NAME IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'UTILITÁRIO KANGU'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VAN','VAN FIXO','VAN L ( DE 10 A 15M3)')
         AND LM.SHP_COMPANY_NAME IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'VAN KANGU'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('CARRO','VEÍCULO DE PASSEIO')
         AND LM.SHP_COMPANY_NAME IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'PASSEIO KANGU'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('VEÍCULO URBANO COMPACTO','3/4/MÉDIO','CARRETA','HR','HR FM DD',
         'MÉDIO','TOCO','TRUCK','VUC BULKY','VUC','VUC FIXO','VUC PP ( ATÉ 10 M3)',
         'VUC G (ACIMA DE 18M3)')
         AND LM.SHP_COMPANY_NAME IN ('Kangu Logistics','KANGU LOGISTICS') THEN 'VUC KANGU'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN (
         'E-UTILITÁRIO LAST MILE','MELIONE RENTAL UTILITÁRIO COM AJUDANTE',
         'RENTAL IHDS ELECTRIC 2P','RENTAL IHDS ELECTRIC 5P','RENTAL IHDS UTITLITY',
         'RENTAL UTILITÁRIO COM AJUDANTE','RENTAL UTILITÁRIO SEM AJUDANTE',
         'UTILITÁRIO ELÉTRICO FROTA FIXA','MELIONE UTILITÁRIO AGREGADO',
         'UTILITÁRIO LOCALIZA 2025','UTILITÁRIO ELÉTRICO BYD','UTILITÁRIO ARVAL 2025',
         'UTILITÁRIO FROTA FIXA NEPO','UTILITÁRIO VAMOS 2025','UTILITÁRIO TKS 2025',
         'UTILITÁRIO FROTA FIXA FADEL','UTILITÁRIO TKS 2025 - NEWBIE',
         'MELIONE RENTAL UTILITÁRIO COM AJUDANTE') THEN 'RENTAL UTILITÁRIO'
    WHEN LM.SHP_LG_VEHICLE_TYPE IN (
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
    WHEN LM.SHP_LG_VEHICLE_TYPE IN (
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
    WHEN LM.SHP_LG_VEHICLE_TYPE IN ('WALKER')
         AND LM.SHP_COMPANY_NAME NOT IN ('Envios Extra','MELI EXTRA') THEN 'WALKER'
    ELSE LM.SHP_LG_VEHICLE_TYPE
  END) AS VEICULO_AGRUPADO,
  (CASE
    WHEN LM.SHP_DESTINATION_FACILITY_ID LIKE 'E%' THEN 'AM1'
    WHEN LM.SHP_CYCLE_NAME_PLANNED IN ('AM0','AM2','AMB','AMDE','AM0V','AM') THEN 'AM1'
    WHEN LM.SHP_LG_FACILITY_ID IN ('SPA1','SRD1','SAM1','STO1','SBA2','SBA3',
         'SMR2','SMS1','SMS2','SSE1')
         AND LM.SHP_DESTINATION_FACILITY_ID IS NULL THEN 'AM1'
    WHEN LM.SHP_CYCLE_NAME_PLANNED IS NULL THEN
         CASE WHEN CAST(FORMAT_DATETIME('%H', SHP_LG_INIT_DTTM_TZ) AS NUMERIC) >= 13
              THEN 'PM1' ELSE 'AM1' END
    WHEN LM.SHP_CYCLE_NAME_PLANNED IN ('SD') THEN 'SD'
    WHEN LM.SHP_CYCLE_NAME_PLANNED IN ('CHP')
         AND LM.SHP_LG_FACILITY_ID IN ('SAL1') THEN 'PM1'
    ELSE LM.SHP_CYCLE_NAME_PLANNED
  END) AS CICLO,
  COUNT(DISTINCT LM.SHP_LG_ROUTE_ID) AS ROTAS,
  SUM(LM.UNIQUE_SHIPMENTS)            AS VOLUME

FROM `meli-bi-data.WHOWNER.DM_SHP_ROUTES_LAST_MILE` AS LM

WHERE DATE(LM.SHP_LG_INIT_DT_TZ) BETWEEN DATE_SUB(CURRENT_DATE, INTERVAL 60 DAY) AND CURRENT_DATE
  AND LM.SHP_SITE_ID = 'MLB'

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

def injetar_html(html: str, js_lines: list[str], semanas: list[tuple]) -> str:
    """Substitui DAILY_DATA e SEMANAS no HTML."""

    # 1. DAILY_DATA  (lambda evita que re interprete \u como escape)
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

    # 3. Query BigQuery
    log.info(f"Conectando ao BigQuery (projeto: {BQ_PROJECT})...")
    client = bigquery.Client(project=BQ_PROJECT)

    log.info("Executando query...")
    job = client.query(QUERY)
    rows = list(job.result())
    log.info(f"  Linhas retornadas do BQ: {len(rows):,}")

    # 4. Processar
    semanas_validas = set(labels)
    js_lines = processar_linhas(rows, semanas_validas)

    if not js_lines:
        log.error("Nenhum registro gerado — abortando para não limpar o dashboard.")
        sys.exit(1)

    # 5. Ler, injetar e salvar HTML
    log.info("Lendo HTML...")
    html = DASHBOARD_PATH.read_text(encoding="utf-8")

    log.info("Injetando dados...")
    html_novo = injetar_html(html, js_lines, sems)

    # Backup rápido (sobrescreve o backup anterior — .replace() funciona no Windows)
    backup = DASHBOARD_PATH.with_suffix(".html.bak")
    DASHBOARD_PATH.replace(backup)

    DASHBOARD_PATH.write_text(html_novo, encoding="utf-8")
    log.info(f"HTML salvo: {DASHBOARD_PATH}")
    log.info(f"Backup em : {backup}")

    # 6. Publicar no GitHub Pages
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
