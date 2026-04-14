"""Convert Schkopau_Model_Explained.md to a print-ready HTML with KaTeX math rendering.

Usage:
    python md_to_pdf.py

Opens the rendered HTML in the default browser.
Use the browser's Print → Save as PDF (Ctrl+P) to produce the final PDF.
"""

import pathlib, webbrowser, markdown, re

# Toggle: True = green-highlighted new sections; False = normal colours throughout.
HIGHLIGHT_MODE = False

MD_FILE = pathlib.Path(__file__).with_name("Schkopau_Model_Explained.md")
OUT_FILE = MD_FILE.with_suffix(".html")

md_text = MD_FILE.read_text(encoding="utf-8")

html_body = markdown.markdown(
    md_text,
    extensions=[
        "tables",
        "fenced_code",
        "pymdownx.arithmatex",
    ],
    extension_configs={
        "pymdownx.arithmatex": {
            "generic": True,
        }
    },
)

html_doc = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Schkopau MTP &ndash; Model Documentation</title>

<!-- KaTeX for math rendering -->
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.css"
      crossorigin="anonymous">
<script defer
        src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/katex.min.js"
        crossorigin="anonymous"></script>
<script defer
        src="https://cdn.jsdelivr.net/npm/katex@0.16.21/dist/contrib/auto-render.min.js"
        crossorigin="anonymous"
        onload="renderMathInElement(document.body, {{
            delimiters: [
                {{left: '$$', right: '$$', display: true}},
                {{left: '$', right: '$', display: false}},
                {{left: '\\\\(', right: '\\\\)', display: false}},
                {{left: '\\\\[', right: '\\\\]', display: true}}
            ],
            throwOnError: false
        }});"></script>

<style>
    @page {{
        size: A4;
        margin: 20mm 18mm 20mm 18mm;
    }}
    body {{
        font-family: 'Segoe UI', Calibri, Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.55;
        color: #1a1a1a;
        max-width: 210mm;
        margin: 0 auto;
        padding: 15mm 20mm;
    }}
    h1 {{
        font-size: 22pt;
        border-bottom: 3px solid #003366;
        padding-bottom: 8px;
        color: #003366;
        margin-top: 0;
    }}
    h2 {{
        font-size: 16pt;
        color: #003366;
        border-bottom: 1.5px solid #ddd;
        padding-bottom: 4px;
        margin-top: 32px;
        page-break-after: avoid;
    }}
    h3 {{
        font-size: 13pt;
        color: #1a3d5c;
        margin-top: 22px;
        page-break-after: avoid;
    }}
    h4 {{
        font-size: 11.5pt;
        color: #1a3d5c;
        margin-top: 16px;
        page-break-after: avoid;
    }}
    p, li {{
        text-align: justify;
        orphans: 3;
        widows: 3;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        margin: 12px 0;
        font-size: 10pt;
        page-break-inside: avoid;
    }}
    th {{
        background-color: #003366;
        color: white;
        padding: 6px 10px;
        text-align: left;
        font-weight: 600;
    }}
    th code {{
        background: none;
        color: white;
        padding: 0;
    }}
    td {{
        padding: 5px 10px;
        border-bottom: 1px solid #ddd;
    }}
    tr:nth-child(even) td {{
        background-color: #f7f9fb;
    }}
    code {{
        background-color: #f0f2f5;
        padding: 1px 5px;
        border-radius: 3px;
        font-size: 10pt;
        font-family: Consolas, 'Courier New', monospace;
    }}
    pre {{
        background-color: #f0f2f5;
        padding: 12px 16px;
        border-radius: 4px;
        border-left: 3px solid #003366;
        overflow-x: auto;
        font-size: 9.5pt;
        line-height: 1.45;
        page-break-inside: avoid;
    }}
    pre code {{
        background: none;
        padding: 0;
    }}
    blockquote {{
        border-left: 4px solid #003366;
        margin: 16px 0;
        padding: 8px 16px;
        background-color: #f7f9fb;
        color: #333;
        font-style: italic;
    }}
    hr {{
        border: none;
        border-top: 2px solid #003366;
        margin: 28px 0;
    }}
    .arithmatex {{
        overflow-x: auto;
    }}
    .katex-display {{
        margin: 14px 0;
    }}
    strong {{
        color: #003366;
    }}

    /* Colored code annotations — each .c1–.c8 class gives
       a distinct comment colour so readers can match
       annotations to specific lines at a glance. */
    pre.annotated {{
        background-color: #f8f9fa;
        padding: 14px 18px;
        border-radius: 4px;
        border-left: 3px solid #003366;
        overflow-x: auto;
        font-size: 9.5pt;
        line-height: 1.55;
        font-family: Consolas, 'Courier New', monospace;
        page-break-inside: avoid;
    }}
    .c1 {{ color: #2563eb; font-style: italic; }}   /* blue    */
    .c2 {{ color: #059669; font-style: italic; }}   /* green   */
    .c3 {{ color: #d97706; font-style: italic; }}   /* amber   */
    .c4 {{ color: #7c3aed; font-style: italic; }}   /* purple  */
    .c5 {{ color: #dc2626; font-style: italic; }}   /* red     */
    .c6 {{ color: #0891b2; font-style: italic; }}   /* cyan    */
    .c7 {{ color: #be185d; font-style: italic; }}   /* pink    */
    .c8 {{ color: #4338ca; font-style: italic; }}   /* indigo  */

    /* Print tweaks */
    @media print {{
        body {{
            padding: 0;
            font-size: 10.5pt;
        }}
        h2 {{
            page-break-before: auto;
        }}
        pre, table, blockquote {{
            page-break-inside: avoid;
        }}
        a {{
            color: #1a1a1a;
            text-decoration: none;
        }}
    }}
</style>
</head>
<body>
{html_body}
</body>
</html>
"""

# --- Post-process: handle <!-- NEW_START --> / <!-- NEW_END --> markers ---
if HIGHLIGHT_MODE:
    html_doc = html_doc.replace(
        "<!-- NEW_START -->",
        '<div style="border-left:4px solid #16a34a; background:#f0fdf4; padding:8px 16px; margin:16px 0;">'
    ).replace("<!-- NEW_END -->", "</div>")
else:
    html_doc = re.sub(r"<!--\s*NEW_(?:START|END)\s*-->", "", html_doc)

OUT_FILE.write_text(html_doc, encoding="utf-8")
print(f"Written: {OUT_FILE}")
print("Opening in browser — use Ctrl+P → Save as PDF")
webbrowser.open(str(OUT_FILE.resolve()))
