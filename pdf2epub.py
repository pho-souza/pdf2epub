#!/usr/bin/env python3
"""pdf2epub - converte PDFs (artigos) em EPUB otimizado para e-ink (Xteink X4, 480x800).

- Perfis de saída: --device xteink (480 px, tons de cinza) ou --device normal (Kindle etc., cores originais).
- Fórmulas: equações em destaque (fonte/símbolos matemáticos) viram imagem; --formulas text desativa.
- Notas de rodapé: --footnotes end (padrão, lista no fim do capítulo) ou linked (número no texto vira link
  para a nota, com link de volta).
- Texto vira XHTML reflowable (parágrafos, títulos, negrito/itálico).
- Tabelas, imagens e gráficos vetoriais são detectados e reinseridos como imagem (PNG em tons de cinza).
- Cabeçalhos/rodapés repetidos e números de página são removidos.
- Parágrafos quebrados entre páginas/colunas são unidos; hifenização de fim de linha é desfeita.

Uso:  python pdf2epub.py artigo.pdf [-o saida.epub] [--preset kindle|xteink|default] [--device xteink|normal] [--footnotes end|linked]
"""
import argparse
import io
import json
import re
import sys
import uuid
import zipfile
from collections import Counter
from html import escape
from pathlib import Path

import pymupdf
from PIL import Image

TABLE_STRATEGY = "lines"  # "text" também detecta tabelas sem bordas (mais falsos positivos)
PROFILES = {
    "xteink": {"width": 480, "margin": 10, "dpi": 220, "color": False},
    "normal": {"width": 1072, "margin": 20, "dpi": 200, "color": True},
}
SENT_END = tuple('.!?:;"”»)]')
CAPTION_RE = re.compile(r"^\s*(fig(ure|ura)?\.?|tab(le|ela)?\.?|gr[aá]fico|chart|quadro)\s*\d+", re.I)


# ---------------------------------------------------------------- geometria
def overlap_frac(a, b):
    """Fração da área de `a` coberta por `b`."""
    x0, y0, x1, y1 = max(a.x0, b.x0), max(a.y0, b.y0), min(a.x1, b.x1), min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = a.get_area()
    return ((x1 - x0) * (y1 - y0)) / area if area else 0.0


def merge_rects(rects, gap=6):
    """Une retângulos que se tocam/sobrepõem (com folga `gap`)."""
    rects = [pymupdf.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out = []
        for r in rects:
            for i, o in enumerate(out):
                if (r + (-gap, -gap, gap, gap)).intersects(o):
                    out[i] = o | r
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


# ---------------------------------------------------------------- figuras
def find_figures(page, min_size=40):
    """Retorna lista de (rect, tipo) para tabelas, imagens e gráficos vetoriais."""
    pr = page.rect
    figs = []

    # tabelas
    try:
        for t in page.find_tables(strategy=TABLE_STRATEGY).tables:
            r = pymupdf.Rect(t.bbox)
            if r.width > min_size and r.height > min_size and t.row_count >= 2 and t.col_count >= 2:
                figs.append((r, "tabela"))
    except Exception:
        pass

    # imagens raster
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & pr
        if r.width >= min_size and r.height >= min_size:
            figs.append((r, "imagem"))

    # gráficos vetoriais: agrupa desenhos próximos
    try:
        for r in page.cluster_drawings():
            r = pymupdf.Rect(r) & pr
            if r.width >= min_size and r.height >= min_size:
                figs.append((r, "grafico"))
    except Exception:
        pass

    # remove contidos em outros / mescla sobrepostos
    figs.sort(key=lambda f: -f[0].get_area())
    kept = []
    for r, kind in figs:
        merged = False
        for i, (kr, kk) in enumerate(kept):
            if overlap_frac(r, kr) > 0.6:
                if kind == "tabela" and kk != "tabela":
                    kept[i] = (kr | r, "tabela")
                else:
                    kept[i] = (kr | r, kk)
                merged = True
                break
        if not merged:
            kept.append((r, kind))
    # um gráfico que cobre quase a página inteira é provavelmente fundo/moldura: ignora
    kept = [(r, k) for r, k in kept if not (k == "grafico" and r.get_area() > 0.85 * pr.get_area())]
    return sorted(kept, key=lambda f: (f[0].y0, f[0].x0))


NUMLIKE = re.compile(r"^[\d.,%/\-–−+()R$€£]+$")
SOURCE_RE = re.compile(r"^\s*(fonte|source|elabora|nota|notes?)\b", re.I)


def is_chart_label(block, body):
    """Texto típico de gráfico: rótulos de eixo (números), legenda ou título de eixo curtos."""
    toks, sizes, nl = [], [], 0
    for ln in block["lines"]:
        nl += 1
        for sp in ln["spans"]:
            if sp["text"].strip():
                toks += sp["text"].split()
                sizes.append(sp["size"])
    if not toks:
        return False
    text = " ".join(toks)
    if CAPTION_RE.match(text) or SOURCE_RE.match(text) or (sizes and max(sizes) > body * 1.1):
        return False
    rotated = any(abs(ln.get("dir", (1, 0))[1]) > 0.5 for ln in block["lines"])  # título de eixo vertical
    if rotated and len(text) <= 120:
        return True
    numeric = sum(1 for t in toks if NUMLIKE.match(t)) / len(toks)
    if numeric >= 0.7:
        return True
    return len(text) <= 80 and nl <= 4 and not text.rstrip().endswith(".")


def expand_chart_regions(figs, blocks, body, gap=21):
    """Gráficos vetoriais/imagens: inclui rótulos dos eixos e legendas (texto próximo) na região recortada."""
    out = []
    for r, kind in figs:
        if kind in ("grafico", "imagem"):
            cand = [(pymupdf.Rect(b["bbox"]), b) for b in blocks if is_chart_label(b, body)]
            changed = True
            while changed:
                changed = False
                for cr, b in list(cand):
                    dx = max(0, cr.x0 - r.x1, r.x0 - cr.x1)
                    dy = max(0, cr.y0 - r.y1, r.y0 - cr.y1)
                    if dx <= gap and dy <= gap:
                        r = r | cr
                        cand.remove((cr, b))
                        changed = True
        out.append((r, kind))
    return out


def render_region(page, rect, max_w, dpi, color=False, pad=1):
    """Renderiza a região como PNG (cinza ou colorido), dimensionado para a tela."""
    clip = (rect + (-pad, -pad, pad, pad)) & page.rect
    cs = pymupdf.csRGB if color else pymupdf.csGRAY
    pix = page.get_pixmap(clip=clip, dpi=dpi, colorspace=cs, alpha=False)
    img = Image.frombytes("RGB" if color else "L", (pix.width, pix.height), pix.samples)
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue(), img.size


# ---------------------------------------------------------------- fórmulas
MATH_FONT = re.compile(r"cmmi|cmsy|cmex|cmbsy|msam|msbm|mathematicalpi|cambria.?math|stix|mtmi|mtsy|mtex|"
                       r"symbolmt|^symbol|lmmath|latinmodern.?math|xits|mathjax|texgyre.*math|euclid", re.I)
MATH_CHARS = set("=+−×÷±∓≤≥≠≈≡∼∝∞∂∇∑∏∫√∈∉⊂⊃⊆⊇∪∩∀∃∧∨¬→←↔⇒⇔·∘⋅⊕⊗αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩ∥⟨⟩|{}[]^_")
EQ_NUM = re.compile(r"^\(?\s*\d{1,3}([.:]\d{1,3})?\s*\)?$")


def math_ratio(block):
    n = m = 0
    for ln in block["lines"]:
        for s in ln["spans"]:
            t = s["text"].replace(" ", "")
            if not t:
                continue
            n += len(t)
            if MATH_FONT.search(s["font"]):
                m += len(t)
            else:
                m += sum(1 for c in t if c in MATH_CHARS)
    return (m / n if n else 0.0), n


def find_formulas(page, blocks, figs):
    """Detecta equações em destaque (blocos dominados por fonte/símbolos matemáticos).
    Agrupa os pedaços próximos (frações, índices, número da equação) em uma região."""
    cands, others = [], []
    for b in blocks:
        r = pymupdf.Rect(b["bbox"])
        if any(overlap_frac(r, fr) > 0.5 for fr, _ in figs):
            continue
        ratio, n = math_ratio(b)
        (cands if (ratio >= 0.35 and 0 < n <= 220) else others).append((r, b, n))
    if not cands:
        return []
    regions = merge_rects([c[0] for c in cands], gap=5)
    out = []
    for reg in regions:
        for r, b, n in others:  # pedaços pequenos alinhados (número da equação, índices soltos)
            txt = " ".join(s["text"] for ln in b["lines"] for s in ln["spans"]).strip()
            inter = r & reg
            vert = (min(r.y1, reg.y1) - max(r.y0, reg.y0)) / max(r.height, 1)
            if n <= 12 and (EQ_NUM.match(txt) and vert > 0.5 or (r + (-3, -3, 3, 3)).intersects(reg)):
                reg = reg | r
        out.append((reg, "formula"))
    return out


# ---------------------------------------------------------------- texto
LIGS = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "\u00ad": ""})


def span_html(spans):
    out = []
    mx = max((s["size"] for s in spans if s["text"].strip()), default=0)
    for s in spans:
        t = s["text"].translate(LIGS)
        if not t.strip():
            out.append(escape(t))
            continue
        h = escape(t)
        bold = bool(s["flags"] & 16) or "bold" in s["font"].lower()
        ital = bool(s["flags"] & 2) or "italic" in s["font"].lower() or "oblique" in s["font"].lower()
        if (s["flags"] & 1 or s["size"] < 0.8 * mx) and len(t.strip()) < 5:  # sobrescrito
            h = f"<sup>{h}</sup>"
        if bold:
            h = f"<b>{h}</b>"
        if ital:
            h = f"<i>{h}</i>"
        out.append(h)
    return "".join(out)


def block_lines(block):
    """Lista de (texto_plano, html) por linha."""
    res = []
    for ln in block["lines"]:
        spans = [s for s in ln["spans"] if s["text"]]
        if not spans:
            continue
        res.append(("".join(s["text"] for s in spans), span_html(spans)))
    return res


def join_lines(lines):
    """Une linhas de um bloco desfazendo hifenização."""
    text = ""
    for i, (plain, html) in enumerate(lines):
        html = html.strip()
        if not text:
            text = html
        elif re.search(r"[A-Za-zÀ-ÿ]-(</[bi]>)*$", text) and re.match(r"(<[bi]>)*[a-zà-ÿ]", html):
            text = re.sub(r"-((?:</[bi]>)*)$", r"\1", text) + html
        else:
            text += " " + html
    text = text.replace("</b> <b>", " ").replace("</i> <i>", " ")
    return re.sub(r"\s+", " ", text).strip()


def dominant_size(doc, sample=40):
    cnt = Counter()
    n = len(doc)
    pages = range(n) if n <= sample else range(0, n, max(1, n // sample))
    for pno in pages:
        for b in doc[pno].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    cnt[round(s["size"], 1)] += len(s["text"].strip())
    return cnt.most_common(1)[0][0] if cnt else 10.0


def find_running_headers(doc):
    """Textos repetidos no topo/rodapé em várias páginas (cabeçalho, rodapé, nº de página)."""
    n = len(doc)
    if n < 3:
        return set()
    cnt = Counter()
    for page in doc:
        h = page.rect.height
        seen = set()
        for b in page.get_text("blocks"):
            if b[6] != 0:
                continue
            y0, y1 = b[1], b[3]
            if y1 < h * 0.09 or y0 > h * 0.92:
                key = re.sub(r"\d+", "#", " ".join(b[4].split()))
                if key and key not in seen:
                    seen.add(key)
                    cnt[key] += 1
    return {k for k, v in cnt.items() if v >= max(3, n * 0.3)}


def order_blocks(blocks, page_w):
    """Ordem de leitura: detecta 2 colunas e lê a esquerda antes da direita."""
    if not blocks:
        return blocks
    mid = page_w / 2
    narrow = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) < page_w * 0.55]
    left = [b for b in narrow if b["bbox"][2] <= mid + 8]
    right = [b for b in narrow if b["bbox"][0] >= mid - 8]
    if len(narrow) >= 0.6 * len(blocks) and len(left) >= 2 and len(right) >= 2:
        full = [b for b in blocks if b not in left and b not in right]
        # blocos de largura total dividem a página em faixas verticais
        full.sort(key=lambda b: b["bbox"][1])
        out, ys = [], [f["bbox"][1] for f in full]
        bounds = [-1e9] + [f["bbox"][3] for f in full] + [1e9]
        for i in range(len(full) + 1):
            lo, hi = bounds[i], (ys[i] if i < len(ys) else 1e9)
            band = lambda lst: sorted((b for b in lst if lo <= b["bbox"][1] < hi), key=lambda b: b["bbox"][1])
            out += band(left) + band(right)
            if i < len(full):
                out.append(full[i])
        return out
    return sorted(blocks, key=lambda b: (round(b["bbox"][1] / 4), b["bbox"][0]))


# ---------------------------------------------------------------- conversão
class Item:
    def __init__(self, kind, **kw):
        self.kind = kind  # p | h | img
        self.__dict__.update(kw)


def extract(doc, args):
    body = dominant_size(doc)
    headers = find_running_headers(doc)
    items, images = [], []
    heading_sizes = Counter()

    for pno, page in enumerate(doc):
        figs = find_figures(page, args.min_fig)
        pw = page.rect.width
        blocks = [b for b in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"] if b["type"] == 0]

        figs = expand_chart_regions(figs, blocks, body, args.chart_margin)
        if args.formulas == "image":
            figs = sorted(figs + find_formulas(page, blocks, figs), key=lambda f: (f[0].y0, f[0].x0))

        text_items = []
        for b in order_blocks(blocks, pw):
            r = pymupdf.Rect(b["bbox"])
            if any(overlap_frac(r, fr) > 0.5 for fr, _ in figs):
                continue
            lines = block_lines(b)
            if not lines:
                continue
            plain = " ".join(p for p, _ in lines).translate(LIGS).strip()
            if not plain:
                continue
            key = re.sub(r"\d+", "#", " ".join(plain.split()))
            if key in headers or re.fullmatch(r"[-–— ]*\d{1,4}[-–— ]*", plain):
                continue
            sizes = [s["size"] for ln in b["lines"] for s in ln["spans"] if s["text"].strip()]
            size = max(set(round(x, 1) for x in sizes), key=sizes.count) if sizes else body
            allbold = all((s["flags"] & 16) or "bold" in s["font"].lower()
                          for ln in b["lines"] for s in ln["spans"] if s["text"].strip())
            is_h = not CAPTION_RE.match(plain) and len(plain) < 180 and (size >= body * 1.18 or (allbold and len(plain) < 90 and not plain.endswith(".")))
            if is_h and size >= body * 1.18:
                heading_sizes[round(size, 1)] += 1
            is_fn = (not is_h and r.y0 > page.rect.height * 0.75 and size <= body * 0.9
                     and re.match(r"^\s*\d{1,3}\b", plain))
            text_items.append((r.y0, Item("h" if is_h else ("fn" if is_fn else "p"), html=join_lines(lines), plain=plain,
                                          size=size, cont=False, x0=r.x0, page=pno)))

        # posiciona figuras na ordem de leitura (antes do 1º bloco que começa abaixo delas)
        out = list(text_items)
        for fr, kind in figs:
            data, (w, h) = render_region(page, fr, args.width - 2 * args.margin, args.dpi, args.color)
            name = f"img{len(images) + 1:04d}.png"
            images.append((name, data))
            it = Item("img", src=name, w=w, h=h, figtype=kind)
            idx = next((i for i, (y, _) in enumerate(out) if y >= fr.y0 - 1), len(out))
            out.insert(idx, (fr.y0, it))
        items += [it for _, it in out]

    return items, images, body, heading_sizes


def merge_paragraphs(items):
    """Une parágrafos quebrados entre colunas/páginas."""
    out = []
    for it in items:
        if (it.kind == "p" and out and out[-1].kind == "p"
                and not out[-1].plain.rstrip().endswith(SENT_END)
                and re.match(r"[a-zà-ÿ(]", it.plain)
                and not CAPTION_RE.match(it.plain)):
            prev = out[-1]
            if re.search(r"[A-Za-zÀ-ÿ]-$", prev.plain):
                prev.html = prev.html[:-1] + it.html
            else:
                prev.html += " " + it.html
            prev.plain += " " + it.plain
        else:
            out.append(it)
    return out


def split_footnotes(items):
    """Um bloco de rodapé pode conter várias notas seguidas (cada número em sobrescrito inicia uma)."""
    out = []
    for it in items:
        if it.kind == "fn":
            parts = [p for p in re.split(r"(?=<sup>\d{1,3}</sup>)|(?<=[.;:)] )(?=\d{1,3} [A-ZÀ-Ý])", it.html) if p.strip()]
            if len(parts) > 1:
                for p in parts:
                    n = Item(**{**it.__dict__, "html": p.strip()})
                    out.append(n)
                continue
        out.append(it)
    return out


def link_footnotes(items):
    """Associa números sobrescritos no texto às notas da mesma página (links ida e volta)."""
    by_page = {}
    for it in items:
        if it.kind != "fn":
            continue
        m = re.match(r"^\s*(?:<sup>)?\s*(\d{1,3})\s*(?:</sup>)?\s*", it.html)
        if m:
            it.num, it.html = m.group(1), it.html[m.end():]
            it.fid = f"fn{it.page}_{it.num}"
            it.refid = None
            by_page.setdefault(it.page, {})[it.num] = it
    for it in items:
        if it.kind not in ("p", "h"):
            continue

        def sub(m, it=it):
            fn = by_page.get(it.page, {}).get(m.group(1))
            if not fn:
                return m.group(0)
            ref_id = ""
            if fn.refid is None:
                fn.refid = f"ref{it.page}_{fn.num}"
                ref_id = f' id="{fn.refid}"'
            return f'<sup><a epub:type="noteref"{ref_id} href="@@{fn.fid}">{m.group(1)}</a></sup>'

        it.html = re.sub(r"<sup>(\d{1,3})</sup>", sub, it.html)


def assign_levels(items, heading_sizes, body):
    sizes = sorted((s for s in heading_sizes), reverse=True)
    for it in items:
        if it.kind != "h":
            continue
        if it.size >= body * 1.18 and sizes:
            rank = min(range(len(sizes)), key=lambda i: abs(sizes[i] - round(it.size, 1)))
            it.level = min(rank + 1, 3)
        else:
            it.level = 3


def build_chapters(items, doc, title):
    """Divide em capítulos nos títulos de nível mais alto presentes."""
    levels = Counter(it.level for it in items if it.kind == "h")
    split_level = None
    for lv in (1, 2, 3):
        if 2 <= levels.get(lv, 0) <= 60:
            split_level = lv
            break
    chapters, cur = [], {"title": title, "items": []}
    for it in items:
        if split_level and it.kind == "h" and it.level <= split_level and cur["items"]:
            chapters.append(cur)
            cur = {"title": re.sub(r"<sup>.*?</sup>|<[^>]+>", "", it.html), "items": []}
        cur["items"].append(it)
    if cur["items"]:
        chapters.append(cur)
    # evita capítulos minúsculos sem texto (só títulos seguidos)
    chapters = chapters or [{"title": title, "items": items}]
    # notas de rodapé vão para o fim do capítulo (no modo linked, o capítulo que contém a referência)
    original = [[i for i in ch["items"] if i.kind == "fn"] for ch in chapters]
    allfns = [i for g in original for i in g]
    for ch in chapters:
        ch["items"] = [i for i in ch["items"] if i.kind != "fn"]
    for fn in allfns:
        target = None
        if getattr(fn, "refid", None):
            key = f'id="{fn.refid}"'
            target = next((ch for ch in chapters if any(key in getattr(i, "html", "") for i in ch["items"])), None)
        fn.target = target
    for ch in chapters:
        ch["_fns"] = []
    for ch, group in zip(chapters, original):
        for fn in group:
            (fn.target or ch)["_fns"].append(fn)
    for ch in chapters:
        if ch["_fns"]:
            ch["items"] += [Item("h", html="Notas", plain="Notas", level=3)] + ch["_fns"]
    return chapters


# ---------------------------------------------------------------- EPUB
CSS = """
body { margin: 0; padding: 0; line-height: 1.35; text-align: left; }
p { margin: 0 0 0.7em 0; text-indent: 0; }
h1, h2, h3 { margin: 1.2em 0 0.5em 0; line-height: 1.2; page-break-after: avoid; }
h1 { font-size: 1.5em; } h2 { font-size: 1.25em; } h3 { font-size: 1.1em; }
.fn { font-size: 0.85em; margin: 0 0 0.4em 0; }
.fig { margin: 1em 0; text-align: center; page-break-inside: avoid; }
.fig img { max-width: 100%; height: auto; }
"""


def xhtml(title, body_html):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="pt">
<head><meta charset="utf-8"/><title>{escape(title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
{body_html}
</body></html>"""


def write_epub(path, title, author, lang, chapters, images):
    uid = f"urn:uuid:{uuid.uuid4()}"
    files = []
    for i, ch in enumerate(chapters, 1):
        parts = []
        for it in ch["items"]:
            if it.kind == "h":
                parts.append(f"<h{it.level}>{re.sub(r'</?[bi]>', '', it.html)}</h{it.level}>")
            elif it.kind == "fn":
                if getattr(it, "num", None) and it.refid:
                    parts.append(f'<p class="fn" id="{it.fid}" epub:type="footnote">'
                                 f'<a href="@@{it.refid}">{it.num}</a> {it.html}</p>')
                else:
                    parts.append(f'<p class="fn">{it.html}</p>')
            elif it.kind == "p":
                parts.append(f"<p>{it.html}</p>")
            else:
                parts.append(f'<div class="fig"><img src="images/{it.src}" alt="{it.figtype}" '
                             f'width="{it.w}" height="{it.h}"/></div>')
        files.append((f"ch{i:03d}.xhtml", ch["title"], xhtml(ch["title"], "\n".join(parts))))

    # resolve links entre arquivos (@@id -> arquivo.xhtml#id)
    where = {}
    for fname, _, content in files:
        for m in re.finditer(r'id="((?:fn|ref)\d+_\d+)"', content):
            where[m.group(1)] = fname
    files = [(f, t, re.sub(r'href="@@([^"]+)"', lambda m: f'href="{where.get(m.group(1), f)}#{m.group(1)}"', c))
             for f, t, c in files]

    manifest = ['<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
    manifest += [f'<item id="c{i}" href="{f}" media-type="application/xhtml+xml"/>' for i, (f, _, _) in enumerate(files, 1)]
    manifest += [f'<item id="{n}" href="images/{n}" media-type="image/png"/>' for n, _ in images]
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(files) + 1))

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">{uid}</dc:identifier><dc:title>{escape(title)}</dc:title>
<dc:creator>{escape(author)}</dc:creator><dc:language>{lang}</dc:language>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest>{''.join(manifest)}</manifest><spine toc="ncx">{spine}</spine></package>"""

    nav = xhtml("Sumário", '<nav epub:type="toc"><h1>Sumário</h1><ol>' + "".join(
        f'<li><a href="{f}">{escape(t)}</a></li>' for f, t, _ in files) + "</ol></nav>")
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="{uid}"/></head>
<docTitle><text>{escape(title)}</text></docTitle><navMap>""" + "".join(
        f'<navPoint id="n{i}" playOrder="{i}"><navLabel><text>{escape(t)}</text></navLabel><content src="{f}"/></navPoint>'
        for i, (f, t, _) in enumerate(files, 1)) + "</navMap></ncx>"

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)
        for f, _, content in files:
            z.writestr(f"OEBPS/{f}", content, compress_type=zipfile.ZIP_DEFLATED)
        for n, data in images:
            z.writestr(f"OEBPS/images/{n}", data, compress_type=zipfile.ZIP_STORED)


def convert(src, dst, args):
    doc = pymupdf.open(src)
    meta = doc.metadata or {}
    items, images, body, hs = extract(doc, args)
    items = split_footnotes(items)
    if args.footnotes == "linked":
        link_footnotes(items)
    items = merge_paragraphs(items)
    assign_levels(items, hs, body)
    heads = [i for i in items if i.kind == "h" and i.size >= body * 1.18]
    top = max(heads, key=lambda i: i.size) if heads else None
    title = (meta.get("title") or "").strip() or (re.sub(r"<sup>.*?</sup>|<[^>]+>", "", top.html) if top else Path(src).stem)
    author = (meta.get("author") or "").strip() or "Desconhecido"
    chapters = build_chapters(items, doc, title)
    write_epub(dst, title, author, args.lang, chapters, images)
    nfig = sum(1 for i in items if i.kind == "img")
    print(f"{dst}: {len(doc)} páginas -> {len(chapters)} capítulos, {nfig} figuras/tabelas")


DEFAULTS = {"device": "xteink", "footnotes": "end", "formulas": "image", "width": None, "margin": None,
            "dpi": None, "color": None, "chart_margin": 21, "min_fig": 40, "lang": "pt", "text_tables": False}
CHOICES = {"device": PROFILES.keys(), "footnotes": ("end", "linked"), "formulas": ("image", "text")}
PARAMS_FILE = Path(__file__).with_name("parameters.json")


def load_preset(path, name):
    """Lê o preset `name` de um JSON {"default": {...}, "kindle": {...}, ...}. O preset sobrepõe o 'default'."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if name not in data and name != "default":
        sys.exit(f"Preset '{name}' não existe em {path}. Disponíveis: {', '.join(k for k in data if not k.startswith('_'))}")
    merged = {}
    for key in dict.fromkeys(["default", name]):
        for k, v in (data.get(key) or {}).items():
            if k.startswith("_"):
                continue
            k = k.replace("-", "_")
            if k not in DEFAULTS:
                print(f"aviso: parâmetro desconhecido '{k}' no preset '{key}' ignorado", file=sys.stderr)
            elif k in CHOICES and v not in CHOICES[k]:
                sys.exit(f"Valor inválido para '{k}' no preset '{key}': {v!r} (use {', '.join(CHOICES[k])})")
            else:
                merged[k] = v
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("-o", "--output", help="arquivo de saída (só com um PDF)")
    ap.add_argument("--preset", help="nome do preset em parameters.json (ex.: kindle, xteink, default)")
    ap.add_argument("--params", default=str(PARAMS_FILE), help="arquivo JSON com os presets (padrão: parameters.json ao lado do script)")
    ap.add_argument("--device", choices=list(PROFILES),
                    help="xteink: 480 px, tons de cinza (padrão); normal: Kindle/outros, cores originais, 1072 px")
    ap.add_argument("--footnotes", choices=["end", "linked"],
                    help="end: notas listadas no fim do capítulo (padrão); linked: número no texto vira link para a nota")
    ap.add_argument("--formulas", choices=["image", "text"],
                    help="image: equações em destaque viram imagem (padrão); text: mantém como texto")
    ap.add_argument("--width", type=int, help="largura máxima das imagens em px (sobrescreve o perfil)")
    ap.add_argument("--margin", type=int, help="margem lateral em px (sobrescreve o perfil)")
    ap.add_argument("--dpi", type=int, help="DPI de renderização das figuras (sobrescreve o perfil)")
    ap.add_argument("--color", action=argparse.BooleanOptionalAction, help="imagens coloridas ou em tons de cinza (sobrescreve o perfil)")
    ap.add_argument("--chart-margin", type=float,
                    help="distância máxima (pt) para incluir rótulos/títulos de eixo e legendas no recorte do gráfico (padrão 21)")
    ap.add_argument("--min-fig", type=float, help="tamanho mínimo (pt) para considerar figura (padrão 40)")
    ap.add_argument("--lang", help="idioma do EPUB (padrão pt)")
    ap.add_argument("--text-tables", action=argparse.BooleanOptionalAction,
                    help="detecta também tabelas sem bordas (heurístico)")
    args = ap.parse_args()

    # precedência: valores internos < preset (parameters.json) < linha de comando
    cfg = dict(DEFAULTS)
    if args.preset or Path(args.params).is_file():
        if Path(args.params).is_file():
            cfg.update(load_preset(args.params, args.preset or "default"))
        else:
            sys.exit(f"Arquivo de parâmetros não encontrado: {args.params}")
    for k in DEFAULTS:
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v
    prof = PROFILES[cfg["device"]]
    for k in ("width", "margin", "dpi", "color"):
        if cfg[k] is None:
            cfg[k] = prof[k]
    for k, v in cfg.items():
        setattr(args, k, v)

    global TABLE_STRATEGY
    TABLE_STRATEGY = "text" if args.text_tables else "lines"
    for p in args.pdf:
        out = args.output if args.output and len(args.pdf) == 1 else str(Path(p).with_suffix(".epub"))
        convert(p, out, args)


if __name__ == "__main__":
    sys.exit(main())
