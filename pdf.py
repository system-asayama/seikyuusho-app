"""請求書の PDF 出力（reportlab）。

日本語フォントは次の順で探す:
  1. 環境変数 PDF_FONT_PATH
  2. Docker イメージに入れた IPAex ゴシック
  3. Windows / macOS の標準日本語フォント
  4. reportlab 内蔵の CID フォント（フォント埋め込み無し）
"""
import glob
import os
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONT_NAME = "JPFont"
_FONT_READY = False


def _font_candidates() -> list[tuple[str, int]]:
    candidates: list[tuple[str, int]] = []
    env_path = os.environ.get("PDF_FONT_PATH")
    if env_path:
        candidates.append((env_path, 0))
    candidates += [(p, 0) for p in glob.glob("/usr/share/fonts/**/ipaexg.ttf", recursive=True)]
    candidates += [(p, 0) for p in glob.glob("/usr/share/fonts/**/ipag.ttf", recursive=True)]
    candidates += [(p, 0) for p in glob.glob("/usr/share/fonts/**/NotoSansCJK*-Regular.ttc", recursive=True)]
    windir = os.environ.get("WINDIR", "C:/Windows")
    candidates += [
        (os.path.join(windir, "Fonts", "meiryo.ttc"), 0),
        (os.path.join(windir, "Fonts", "YuGothM.ttc"), 0),
        (os.path.join(windir, "Fonts", "msgothic.ttc"), 0),
        ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 0),
    ]
    return candidates


def ensure_font() -> str:
    """日本語フォントを登録し、フォント名を返す。"""
    global _FONT_READY
    if _FONT_READY:
        return FONT_NAME
    for path, index in _font_candidates():
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(FONT_NAME, path, subfontIndex=index))
            _FONT_READY = True
            return FONT_NAME
        except Exception:  # noqa: BLE001 - 読めないフォントは次の候補へ
            continue
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    _FONT_READY = True
    # CID フォントは名前が固定なので別名を使えない。呼び出し側はこの戻り値を使う。
    return "HeiseiKakuGo-W5"


def yen(value) -> str:
    return f"¥{int(value):,}"


def render_invoice_pdf(invoice, company, summary) -> bytes:
    font = ensure_font()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"請求書 {invoice.number}",
        author=company.name or "",
    )

    base = ParagraphStyle("base", fontName=font, fontSize=10, leading=14)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, leading=12, textColor=colors.HexColor("#555555"))
    right = ParagraphStyle("right", parent=base, alignment=TA_RIGHT)
    title = ParagraphStyle("title", parent=base, fontSize=22, leading=28, alignment=TA_CENTER)
    h_client = ParagraphStyle("client", parent=base, fontSize=13, leading=18)
    amount_style = ParagraphStyle("amount", parent=base, fontSize=16, leading=20, alignment=TA_RIGHT)

    def p(text, style=base):
        return Paragraph(_esc(text).replace("\n", "<br/>"), style)

    story = []
    story.append(p("請 求 書", title))
    story.append(Spacer(1, 6 * mm))

    # ヘッダー: 左に宛先、右に番号・日付・自社情報
    client = invoice.client
    left_lines = []
    if client.postal_code or client.address:
        left_lines.append(f"〒{client.postal_code} {client.address}".strip())
    left = [p(f"{client.name} {client.honorific}".strip(), h_client)]
    if left_lines:
        left.append(p("\n".join(left_lines), small))
    if client.contact_name:
        left.append(p(f"{client.contact_name} 様", base))

    meta_lines = [f"請求書番号: {invoice.number}", f"発行日: {_fmt_date(invoice.issue_date)}"]
    if invoice.due_date:
        meta_lines.append(f"お支払期限: {_fmt_date(invoice.due_date)}")
    company_lines = [company.name]
    if company.postal_code or company.address:
        company_lines.append(f"〒{company.postal_code} {company.address}".strip())
    contact = " / ".join(x for x in (f"TEL {company.tel}" if company.tel else "", company.email) if x)
    if contact:
        company_lines.append(contact)
    if company.representative:
        company_lines.append(company.representative)
    if company.registration_number:
        company_lines.append(f"登録番号: {company.registration_number}")
    right_cell = [p("\n".join(meta_lines), right), Spacer(1, 4 * mm), p("\n".join(company_lines), right)]

    header = Table([[left, right_cell]], colWidths=[95 * mm, 79 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header)
    story.append(Spacer(1, 6 * mm))

    # 件名・請求金額
    if invoice.subject:
        story.append(p(f"件名: {invoice.subject}", base))
        story.append(Spacer(1, 2 * mm))
    story.append(p("下記の通りご請求申し上げます。", base))
    story.append(Spacer(1, 3 * mm))
    amount_tbl = Table(
        [[p("ご請求金額（税込）", base), p(yen(summary.total), amount_style)]],
        colWidths=[60 * mm, 60 * mm],
    )
    amount_tbl.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(amount_tbl)
    story.append(Spacer(1, 6 * mm))

    # 明細
    has_reduced = any(item.tax_rate == 8 for item in invoice.items)
    rows = [[p("品目", base), p("数量", right), p("単位", base), p("単価", right), p("税率", right), p("金額", right)]]
    for item in invoice.items:
        mark = " ※" if item.tax_rate == 8 else ""
        rows.append(
            [
                p(item.description + mark, base),
                p(_fmt_qty(item.quantity), right),
                p(item.unit or "", base),
                p(_fmt_price(item.unit_price), right),
                p(f"{item.tax_rate}%", right),
                p(yen(item.amount), right),
            ]
        )
    items_tbl = Table(rows, colWidths=[74 * mm, 18 * mm, 14 * mm, 28 * mm, 14 * mm, 26 * mm], repeatRows=1)
    items_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9eef5")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#bbbbbb")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(items_tbl)
    story.append(Spacer(1, 4 * mm))

    # 集計（税率ごとの区分記載）
    sum_rows = [[p("小計（税抜）", base), p(yen(summary.subtotal), right)]]
    for rs in summary.by_rate:
        sum_rows.append([p(f"{rs.rate}%対象 {yen(rs.subtotal)}　消費税", small), p(yen(rs.tax), right)])
    sum_rows.append([p("消費税合計", base), p(yen(summary.tax), right)])
    sum_rows.append([p("合計（税込）", base), p(yen(summary.total), right)])
    sum_tbl = Table(sum_rows, colWidths=[70 * mm, 40 * mm], hAlign="RIGHT")
    sum_tbl.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(sum_tbl)
    if has_reduced:
        story.append(Spacer(1, 2 * mm))
        story.append(p("※ は軽減税率（8%）対象品目です。", small))
    story.append(Spacer(1, 8 * mm))

    # 振込先・備考
    if company.bank_info:
        story.append(p("お振込先", base))
        story.append(p(company.bank_info, small))
        story.append(Spacer(1, 4 * mm))
    if invoice.notes:
        story.append(p("備考", base))
        story.append(p(invoice.notes, small))

    doc.build(story)
    return buf.getvalue()


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(d) -> str:
    return d.strftime("%Y年%m月%d日") if d else ""


def _fmt_qty(q) -> str:
    q = float(q)
    return f"{q:,.0f}" if q == int(q) else f"{q:,.2f}".rstrip("0").rstrip(".")


def _fmt_price(v) -> str:
    v = float(v)
    return f"¥{v:,.0f}" if v == int(v) else f"¥{v:,.2f}"
