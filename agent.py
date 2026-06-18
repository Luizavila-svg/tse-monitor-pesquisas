#!/usr/bin/env python3
"""
Monitor de Pesquisas Eleitorais do TSE
Baixa diariamente os dados abertos do TSE e gera relatório HTML.
"""

import csv
import html as html_mod
import io
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import requests

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

# ── Configuração ─────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
PDFS_DIR   = REPORTS_DIR / "pdfs"
ARCHIVE_DIR = REPORTS_DIR / "archive"
STATE_FILE = DATA_DIR / "state.json"
INDEX_FILE = REPORTS_DIR / "index.html"

for d in [DATA_DIR, REPORTS_DIR, PDFS_DIR, ARCHIVE_DIR]:
    d.mkdir(parents=True, exist_ok=True)

TSE_CSV_URL = "https://cdn.tse.jus.br/estatistica/sead/odsele/pesquisa_eleitoral/pesquisa_eleitoral_2026.zip"
TSE_QST_URL = "https://cdn.tse.jus.br/estatistica/sead/odsele/pesquisa_eleitoral/questionario_pesquisa_2026.zip"
CSV_ZIP     = DATA_DIR / "pesquisa_eleitoral_2026.zip"
QST_ZIP     = DATA_DIR / "questionario_2026.zip"

DAYS_BACK = 2  # pesquisas registradas nas últimas N*24h
CI_MODE   = os.environ.get("CI", "false").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tse")

# ── Utilitários de download ───────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

def download_if_changed(url: str, dest: Path, etag_key: str, state: dict) -> bool:
    """Baixa arquivo apenas se mudou no servidor (via ETag). Retorna True se baixou."""
    headers = {}
    old_etag = state.get(etag_key)
    if old_etag and dest.exists():
        headers["If-None-Match"] = old_etag

    log.info("Verificando %s …", url)
    try:
        resp = requests.get(url, headers=headers, stream=True, timeout=600)
    except requests.RequestException as e:
        log.error("Falha ao baixar %s: %s", url, e)
        return False

    if resp.status_code == 304:
        log.info("  → sem mudanças (304). Usando cache.")
        return False

    resp.raise_for_status()
    log.info("  → baixando %.1f MB …", int(resp.headers.get("content-length", 0)) / 1e6)

    tmp = dest.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(65536):
            f.write(chunk)
    tmp.replace(dest)

    state[etag_key] = resp.headers.get("ETag", "")
    log.info("  → salvo em %s", dest)
    return True

# ── Leitura do CSV ────────────────────────────────────────────────────────────

def read_recent_surveys(zip_path: Path, days_back: int = DAYS_BACK) -> list[dict]:
    """Lê todos os CSVs do ZIP e retorna pesquisas registradas nas últimas N*24h."""
    cutoff = datetime.now() - timedelta(days=days_back)
    results = []

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            with zf.open(name) as raw:
                content = raw.read().decode("latin-1")
            reader = csv.DictReader(io.StringIO(content), delimiter=";")
            for row in reader:
                try:
                    reg_date = datetime.strptime(row["DT_REGISTRO"], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if reg_date >= cutoff:
                    row["_reg_date"] = reg_date
                    results.append(row)

    # Deduplica por protocolo (aparece em _BRASIL e no CSV do estado)
    seen = set()
    unique = []
    for r in results:
        k = r["NR_PROTOCOLO_REGISTRO"]
        if k not in seen:
            seen.add(k)
            unique.append(r)

    log.info("%d pesquisa(s) registrada(s) nas últimas %dh", len(unique), days_back * 24)
    return unique

# ── Extração de questionários ─────────────────────────────────────────────────

def extract_questions(qst_zip: Path, protocolo: str):
    """Extrai texto do PDF do questionário para o protocolo dado."""
    if not PDF_SUPPORT or not qst_zip.exists():
        return None

    with zipfile.ZipFile(qst_zip) as zf:
        matches = [n for n in zf.namelist() if n.startswith(protocolo)]
        if not matches:
            return None

        pdf_name = matches[0]
        dest = PDFS_DIR / Path(pdf_name).name
        if not dest.exists():
            data = zf.read(pdf_name)
            dest.write_bytes(data)

        try:
            with pdfplumber.open(dest) as pdf:
                pages_text = [p.extract_text() or "" for p in pdf.pages]
            full = "\n".join(pages_text).strip()
            return full if full else None
        except Exception as e:
            log.warning("  Erro ao extrair PDF %s: %s", pdf_name, e)
            return None

def pdf_relative_path(protocolo: str, qst_zip: Path):
    """Retorna caminho relativo do PDF para link no HTML, ou None se não existir."""
    if not qst_zip.exists():
        return None
    try:
        with zipfile.ZipFile(qst_zip) as zf:
            matches = [n for n in zf.namelist() if n.startswith(protocolo)]
            if matches:
                return f"pdfs/{Path(matches[0]).name}"
    except Exception:
        pass
    return None

# ── Formatação do Plano Amostral ─────────────────────────────────────────────

# Palavras-chave que indicam início de uma categoria no texto inline
_CATEGORY_RE = re.compile(
    r'(?<=[.;])\s+'                       # após ponto ou ponto-e-vírgula
    r'(?='
    r'(?:GÊNERO|GENERO|FAIXA\s+ET[AÁ]RIA|GRAU\s+DE\s+INSTRU[CÇ][AÃ]O'
    r'|ESCOLARIDADE|RENDA|NÍVEL\s+ECON[OÔ]MICO|LOCAL(?:IZAÇÃO)?'
    r'|REGI[OÃ]O|[A-ZÁÉÍÓÚÂÊÎÔÛÃÕ]{4,}(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕ]+)*)'
    r'\s*:)',
    re.IGNORECASE | re.UNICODE
)

_ALL_CAPS_HEADER = re.compile(
    r'^([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ\s/\-]{1,50})\s*:\s*$',
    re.UNICODE
)
_INLINE_CAPS_HEADER = re.compile(
    r'^([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ][A-ZÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ\s/\-]{1,50})\s*:\s*(.+)$',
    re.UNICODE
)
_ITEM_LINE = re.compile(r'^(.{2,60}?):\s*(.+)$')
_SOURCE_LINE = re.compile(r'^\(Fonte:|^https?://', re.IGNORECASE)


def _is_all_caps(s: str) -> bool:
    letters = re.sub(r'[^A-Za-záéíóúâêîôûãõàèìòùçÁÉÍÓÚÂÊÎÔÛÃÕÀÈÌÒÙÇ]', '', s)
    return bool(letters) and letters == letters.upper()


def format_plano_amostral(text: str) -> str:
    """Converte o texto bruto do plano amostral em HTML estruturado por categorias."""
    if not text or text.strip() in ('', '—'):
        return '<em>—</em>'

    # Normaliza quebras de linha
    text = text.replace('\r\n', '\n').replace('\r', '\n').strip()

    # Para textos onde as categorias estão inline (numa só linha), separa-as
    text = _CATEGORY_RE.sub('\n', text)

    lines = [l.strip() for l in text.split('\n') if l.strip()]

    parts = []  # lista de (tipo, conteúdo)
    for line in lines:
        if _SOURCE_LINE.match(line):
            continue  # descarta linhas de fonte/URL

        # Cabeçalho ALL CAPS só com "CATEGORIA:" na linha
        m = _ALL_CAPS_HEADER.match(line)
        if m and _is_all_caps(m.group(1)):
            parts.append(('cat', m.group(1).strip().title()))
            continue

        # Cabeçalho ALL CAPS com conteúdo inline: "CATEGORIA: texto..."
        m = _INLINE_CAPS_HEADER.match(line)
        if m and _is_all_caps(m.group(1)):
            cat   = m.group(1).strip().title()
            rest  = m.group(2).strip()
            parts.append(('cat', cat))
            # Divide o conteúdo em sub-itens separados por vírgula ou ponto-e-vírgula
            sub_items = re.split(r'[,;]\s+(?=[A-Za-záéíóúâêîôûãõ0-9])', rest)
            for si in sub_items:
                si = si.strip().rstrip('.')
                if si:
                    parts.append(('item', si))
            continue

        # Linha de item "Label: valor"
        m = _ITEM_LINE.match(line)
        if m:
            parts.append(('item', line))
            continue

        parts.append(('text', line))

    # Monta HTML
    html_parts = []
    for kind, content in parts:
        safe = html_mod.escape(content)
        if kind == 'cat':
            html_parts.append(f'<div class="pa-cat">{safe}</div>')
        elif kind == 'item':
            m = _ITEM_LINE.match(content)
            if m:
                label = html_mod.escape(m.group(1).strip())
                value = html_mod.escape(m.group(2).strip().rstrip(';.,'))
                html_parts.append(
                    f'<div class="pa-item">'
                    f'<span class="pa-label">{label}</span>'
                    f'<span class="pa-value">{value}</span>'
                    f'</div>'
                )
            else:
                html_parts.append(f'<div class="pa-item-plain">{safe}</div>')
        else:
            html_parts.append(f'<div class="pa-text">{safe}</div>')

    return '\n'.join(html_parts) if html_parts else f'<div class="pa-text">{html_mod.escape(text)}</div>'


# ── Geração do HTML ───────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #f4f5f7;
    color: #1a1a2e;
    line-height: 1.6;
}
header {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
    color: white;
    padding: 2.5rem 2rem 2rem;
    text-align: center;
}
header h1 { font-size: 1.8rem; font-weight: 700; letter-spacing: -0.5px; }
header .subtitle { opacity: .75; margin-top: .4rem; font-size: .95rem; }
header .badge {
    display: inline-block;
    background: #e94560;
    color: white;
    border-radius: 20px;
    padding: .25rem .9rem;
    font-size: .9rem;
    font-weight: 600;
    margin-top: .8rem;
}
.filters {
    display: flex;
    gap: .75rem;
    flex-wrap: wrap;
    padding: 1.2rem 2rem;
    background: white;
    border-bottom: 1px solid #e0e0e0;
    position: sticky;
    top: 0;
    z-index: 10;
}
.filters label { font-size: .85rem; font-weight: 600; color: #555; align-self: center; }
.filters select, .filters input {
    padding: .4rem .7rem;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: .9rem;
    background: white;
    cursor: pointer;
}
.filters button {
    padding: .4rem 1rem;
    background: #0f3460;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: .9rem;
}
.main { max-width: 1100px; margin: 0 auto; padding: 1.5rem 1.5rem 3rem; }
.card {
    background: white;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,.07);
    margin-bottom: 1.5rem;
    overflow: hidden;
    border: 1px solid #e8e8e8;
}
.card-header {
    background: #0f3460;
    color: white;
    padding: .9rem 1.25rem;
    display: flex;
    align-items: center;
    gap: .75rem;
    flex-wrap: wrap;
}
.protocolo {
    font-family: monospace;
    font-size: 1rem;
    font-weight: 700;
    background: rgba(255,255,255,.15);
    padding: .2rem .6rem;
    border-radius: 4px;
}
.cargo {
    font-weight: 600;
    font-size: 1rem;
    flex: 1;
}
.uf {
    background: #e94560;
    padding: .2rem .7rem;
    border-radius: 20px;
    font-size: .85rem;
    font-weight: 700;
}
.dt-registro {
    font-size: .8rem;
    opacity: .75;
    margin-left: auto;
}
.empresa {
    padding: .75rem 1.25rem;
    font-weight: 600;
    font-size: 1rem;
    color: #333;
    border-bottom: 1px solid #f0f0f0;
    background: #fafafa;
}
.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0;
}
@media (max-width: 700px) { .grid { grid-template-columns: 1fr; } }
.field {
    padding: .9rem 1.25rem;
    border-bottom: 1px solid #f0f0f0;
    border-right: 1px solid #f0f0f0;
}
.field:nth-child(even) { border-right: none; }
.field label {
    display: block;
    font-size: .72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: #888;
    margin-bottom: .25rem;
}
.field p { font-size: .9rem; color: #333; }
.full-width { grid-column: 1 / -1; border-right: none; }
.questions-section {
    padding: 1rem 1.25rem;
    border-top: 2px solid #e94560;
    background: #fffbf8;
}
.questions-section h4 {
    font-size: .85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .5px;
    color: #e94560;
    margin-bottom: .75rem;
}
.questions-text {
    font-size: .88rem;
    line-height: 1.7;
    color: #333;
    white-space: pre-wrap;
    max-height: 400px;
    overflow-y: auto;
    background: white;
    border: 1px solid #eee;
    border-radius: 6px;
    padding: .75rem 1rem;
}
.plano-amostral {
    border: 1px solid #e8e8e8;
    border-radius: 6px;
    overflow: hidden;
    margin-top: .3rem;
    background: white;
}
.pa-cat {
    font-size: .78rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .6px;
    color: #0f3460;
    background: #f0f4ff;
    border-left: 3px solid #e94560;
    padding: .35rem .75rem;
    margin: .6rem 0 .15rem;
    border-radius: 0 4px 4px 0;
}
.pa-item {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: .5rem;
    padding: .25rem .75rem;
    font-size: .87rem;
    border-bottom: 1px solid #f5f5f5;
}
.pa-label { color: #444; flex: 1; }
.pa-value { font-weight: 600; color: #0f3460; white-space: nowrap; }
.pa-item-plain {
    padding: .25rem .75rem;
    font-size: .87rem;
    color: #444;
    border-bottom: 1px solid #f5f5f5;
}
.pa-text {
    padding: .3rem .75rem;
    font-size: .87rem;
    color: #555;
    line-height: 1.55;
}
.pdf-link {
    display: inline-block;
    margin-top: .5rem;
    padding: .4rem .9rem;
    background: #0f3460;
    color: white;
    border-radius: 6px;
    font-size: .85rem;
    text-decoration: none;
    font-weight: 600;
}
.pdf-link:hover { background: #16213e; }
.no-questions {
    font-size: .85rem;
    color: #999;
    font-style: italic;
    padding: 1rem 1.25rem;
    border-top: 1px solid #f0f0f0;
}
.empty-state {
    text-align: center;
    padding: 4rem 2rem;
    color: #888;
    font-size: 1.1rem;
}
footer {
    text-align: center;
    padding: 2rem;
    font-size: .85rem;
    color: #888;
    border-top: 1px solid #eee;
    background: white;
    margin-top: 2rem;
}
footer a { color: #0f3460; }
.archive-bar {
    background: white;
    border-bottom: 1px solid #eee;
    padding: .6rem 2rem;
    font-size: .85rem;
    color: #555;
}
.archive-bar a { color: #0f3460; margin-right: .75rem; }
"""

FILTER_JS = """
function filterCards() {
    var uf = document.getElementById('f-uf').value.toLowerCase();
    var cargo = document.getElementById('f-cargo').value.toLowerCase();
    var txt = document.getElementById('f-txt').value.toLowerCase();
    document.querySelectorAll('.card').forEach(function(card) {
        var cardUf = card.dataset.uf || '';
        var cardCargo = card.dataset.cargo || '';
        var cardTxt = card.innerText.toLowerCase();
        var show = (!uf || cardUf === uf) &&
                   (!cargo || cardCargo.includes(cargo)) &&
                   (!txt || cardTxt.includes(txt));
        card.style.display = show ? '' : 'none';
    });
}
document.getElementById('f-uf').addEventListener('change', filterCards);
document.getElementById('f-cargo').addEventListener('change', filterCards);
document.getElementById('f-txt').addEventListener('input', filterCards);
document.getElementById('btn-clear').addEventListener('click', function() {
    document.getElementById('f-uf').value = '';
    document.getElementById('f-cargo').value = '';
    document.getElementById('f-txt').value = '';
    filterCards();
});
"""

def fmt_date(raw: str) -> str:
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return raw[:10] if raw else "—"

def build_card(s: dict, qst_zip: Path) -> str:
    protocolo = s["NR_PROTOCOLO_REGISTRO"]
    empresa   = s.get("NM_EMPRESA_FANTASIA") or s.get("NM_EMPRESA", "")
    cargo     = s.get("DS_CARGO", "")
    uf        = s.get("SG_UF", "")

    metodologia  = s.get("DS_METODOLOGIA_PESQUISA", "—") or "—"
    plano_html   = format_plano_amostral(s.get("DS_PLANO_AMOSTRAL", "") or "")
    controle     = s.get("DS_SISTEMA_CONTROLE", "—") or "—"
    municipio    = s.get("DS_DADO_MUNICIPIO", "—") or "—"
    qt           = s.get("QT_ENTREVISTADO", "—") or "—"
    estatistico  = s.get("NM_ESTATISTICO_RESP", "—") or "—"
    dt_inicio    = fmt_date(s.get("DT_INICIO_PESQUISA", ""))
    dt_fim       = fmt_date(s.get("DT_FIM_PESQUISA", ""))
    dt_divulg    = fmt_date(s.get("DT_DIVULGACAO", ""))
    dt_registro  = s.get("DT_REGISTRO", "")[:16]

    questions = extract_questions(qst_zip, protocolo)
    pdf_path  = pdf_relative_path(protocolo, qst_zip)

    if questions:
        q_html = f"""
        <div class="questions-section">
            <h4>Perguntas Registradas</h4>
            <div class="questions-text">{questions[:5000]}{'...' if len(questions) > 5000 else ''}</div>
            {f'<a class="pdf-link" href="{pdf_path}" target="_blank">Baixar PDF completo</a>' if pdf_path else ''}
        </div>"""
    elif pdf_path:
        q_html = f"""
        <div class="questions-section">
            <h4>Questionário</h4>
            <a class="pdf-link" href="{pdf_path}" target="_blank">Baixar PDF do questionário</a>
        </div>"""
    else:
        q_html = '<p class="no-questions">Questionário não disponível no cache local.</p>'

    return f"""
<div class="card" data-uf="{uf.lower()}" data-cargo="{cargo.lower()}">
  <div class="card-header">
    <span class="protocolo">{protocolo}</span>
    <span class="cargo">{cargo}</span>
    <span class="uf">{uf}</span>
    <span class="dt-registro">Registrado: {dt_registro}</span>
  </div>
  <div class="empresa">{empresa}</div>
  <div class="grid">
    <div class="field full-width">
      <label>Metodologia</label>
      <p>{metodologia}</p>
    </div>
    <div class="field full-width">
      <label>Universo / Plano Amostral</label>
      <div class="plano-amostral">{plano_html}</div>
    </div>
    <div class="field full-width">
      <label>Controle de Qualidade</label>
      <p>{controle}</p>
    </div>
    <div class="field full-width">
      <label>Abrangência Geográfica</label>
      <p>{municipio}</p>
    </div>
    <div class="field">
      <label>Entrevistados (universo)</label>
      <p>{qt}</p>
    </div>
    <div class="field">
      <label>Estatístico Responsável</label>
      <p>{estatistico}</p>
    </div>
    <div class="field">
      <label>Período de Coleta</label>
      <p>{dt_inicio} a {dt_fim}</p>
    </div>
    <div class="field">
      <label>Data Prevista de Divulgação</label>
      <p>{dt_divulg}</p>
    </div>
  </div>
  {q_html}
</div>"""

def build_filters(surveys: list[dict]) -> str:
    ufs    = sorted(set(s["SG_UF"] for s in surveys))
    cargos = sorted(set(s["DS_CARGO"] for s in surveys))
    uf_opts    = "".join(f'<option value="{u.lower()}">{u}</option>' for u in ufs)
    cargo_opts = "".join(f'<option value="{c.lower()}">{c}</option>' for c in cargos)
    return f"""
<div class="filters">
  <label>Filtros:</label>
  <select id="f-uf"><option value="">Todos os estados</option>{uf_opts}</select>
  <select id="f-cargo"><option value="">Todos os cargos</option>{cargo_opts}</select>
  <input id="f-txt" type="search" placeholder="Buscar texto…" style="flex:1;min-width:140px;">
  <button id="btn-clear">Limpar</button>
</div>"""

def build_archive_bar() -> str:
    files = sorted(ARCHIVE_DIR.glob("*.html"), reverse=True)[:10]
    if not files:
        return ""
    links = " | ".join(f'<a href="archive/{f.name}">{f.stem}</a>' for f in files)
    return f'<div class="archive-bar">Relatórios anteriores: {links}</div>'

def generate_report(surveys: list[dict], qst_zip: Path) -> str:
    now = datetime.now()
    count = len(surveys)
    cards = "".join(build_card(s, qst_zip) for s in
                    sorted(surveys, key=lambda x: x["_reg_date"], reverse=True))

    if not cards:
        cards = '<div class="empty-state">Nenhuma pesquisa registrada nas últimas 24 horas.</div>'

    filters = build_filters(surveys) if surveys else ""
    archive = build_archive_bar()

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Monitor TSE · Pesquisas Eleitorais</title>
  <style>{CSS}</style>
</head>
<body>
<header>
  <h1>Monitor de Pesquisas Eleitorais — TSE</h1>
  <p class="subtitle">Eleições 2026 · Atualizado em {now.strftime('%d/%m/%Y às %H:%M')}</p>
  <div class="badge">{count} pesquisa{'s' if count != 1 else ''} registrada{'s' if count != 1 else ''} nas últimas 24h</div>
</header>
{archive}
{filters}
<div class="main">
{cards}
</div>
<footer>
  Fonte: <a href="https://dadosabertos.tse.jus.br/dataset/pesquisas-eleitorais-2026" target="_blank">
    Portal de Dados Abertos do TSE</a> ·
  <a href="https://pesqele-divulgacao.tse.jus.br/" target="_blank">Sistema PesqEle</a>
</footer>
<script>{FILTER_JS}</script>
</body>
</html>"""

# ── Execução principal ────────────────────────────────────────────────────────

def run():
    log.info("═══ Monitor TSE — iniciando (%s) ═══", datetime.now().strftime("%Y-%m-%d %H:%M"))
    state = load_state()

    # 1. Baixar CSV (sempre pequeno, ~1.5 MB)
    csv_changed = download_if_changed(TSE_CSV_URL, CSV_ZIP, "csv_etag", state)

    # 2. Ler pesquisas recentes
    surveys = read_recent_surveys(CSV_ZIP)

    # 3. Baixar questionários (apenas fora do CI — ZIP é muito grande para nuvem)
    if CI_MODE:
        log.info("Modo CI: questionários disponíveis via link no TSE.")
    elif not QST_ZIP.exists() or csv_changed:
        log.info("Verificando ZIP de questionários (pode levar alguns minutos)…")
        download_if_changed(TSE_QST_URL, QST_ZIP, "qst_etag", state)
    else:
        log.info("ZIP de questionários em cache (%s).", QST_ZIP.name)

    # 4. Extrair PDFs dos questionários (apenas fora do CI)
    if not CI_MODE:
        log.info("Extraindo questionários dos %d registro(s)…", len(surveys))
        for s in surveys:
            pdf_relative_path(s["NR_PROTOCOLO_REGISTRO"], QST_ZIP)

    # 5. Gerar relatório HTML
    html = generate_report(surveys, QST_ZIP if not CI_MODE else Path("/dev/null"))

    # Salva no índice principal
    INDEX_FILE.write_text(html, encoding="utf-8")

    # Arquiva com a data de hoje
    today = datetime.now().strftime("%Y-%m-%d")
    (ARCHIVE_DIR / f"{today}.html").write_text(html, encoding="utf-8")

    save_state(state)
    log.info("Relatório gerado em %s", INDEX_FILE)

    # Publica no GitHub Pages (se repositório git configurado)
    if not CI_MODE:
        publish_to_github(today)

    log.info("═══ Concluído ═══")


def publish_to_github(today: str):
    """Publica reports/ no branch gh-pages do GitHub."""
    import subprocess
    import tempfile
    import shutil

    repo_url = "https://github.com/Luizavila-svg/tse-monitor-pesquisas.git"

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log.info("Publicando relatório no GitHub Pages…")

            # Clona apenas o branch gh-pages (raso, mais rápido)
            subprocess.run(
                ["git", "clone", "--depth=1", "--branch=gh-pages", repo_url, tmpdir],
                check=True, capture_output=True
            )

            # Copia os arquivos gerados
            shutil.copy(INDEX_FILE, Path(tmpdir) / "index.html")
            archive_dst = Path(tmpdir) / "archive"
            archive_dst.mkdir(exist_ok=True)
            for f in ARCHIVE_DIR.glob("*.html"):
                shutil.copy(f, archive_dst / f.name)
            pdfs_dst = Path(tmpdir) / "pdfs"
            pdfs_dst.mkdir(exist_ok=True)
            for f in PDFS_DIR.glob("*.pdf"):
                shutil.copy(f, pdfs_dst / f.name)

            # Commit e push
            subprocess.run(["git", "config", "user.email", "avila.avila.luiz@gmail.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Luizavila-svg"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=tmpdir, check=True, capture_output=True)
            result = subprocess.run(
                ["git", "commit", "-m", f"Relatório {today}"],
                cwd=tmpdir, capture_output=True
            )
            if result.returncode == 0:
                subprocess.run(["git", "push", "origin", "gh-pages"], cwd=tmpdir, check=True, capture_output=True)
                log.info("Publicado em https://luizavila-svg.github.io/tse-monitor-pesquisas/")
            else:
                log.info("Sem mudanças para publicar.")

    except subprocess.CalledProcessError as e:
        log.warning("Erro ao publicar no GitHub: %s", e)
    except Exception as e:
        log.warning("Publicação ignorada: %s", e)


if __name__ == "__main__":
    run()
