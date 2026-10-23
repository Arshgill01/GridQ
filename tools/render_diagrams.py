"""Render docs/diagrams/*.mmd to light and dark SVGs in docs/img/ (dev-time only: needs Chrome + internet).

    python tools/render_diagrams.py

GitHub's built-in Mermaid renderer refuses these flowcharts, so the README ships pre-rendered SVGs
in the dashboard's palette. Edit the .mmd source, then re-run this.
"""
import html
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
THEMES = {
    "light": dict(primaryColor="#f3efe6", primaryBorderColor="#1b1d1a", primaryTextColor="#1b1d1a",
                  lineColor="#44463f", clusterBkg="#e9e4d8", clusterBorder="#c6bfae", titleColor="#1b1d1a",
                  edgeLabelBackground="#f3efe6", tertiaryColor="#e9e4d8", fontFamily=FONT, fontSize="15px"),
    "dark": dict(primaryColor="#181b1f", primaryBorderColor="#c2beb2", primaryTextColor="#ebe7dc",
                 lineColor="#c2beb2", clusterBkg="#1f2328", clusterBorder="#3a3f46", titleColor="#ebe7dc",
                 edgeLabelBackground="#181b1f", tertiaryColor="#1f2328", fontFamily=FONT, fontSize="15px"),
}


def main():
    jobs = [(p.stem, t, p.read_text()) for p in sorted((ROOT / "docs/diagrams").glob("*.mmd")) for t in THEMES]
    js = ("const J=" + json.dumps(jobs) + ",T=" + json.dumps(THEMES) + ";(async()=>{const o={};"
          "for(const [n,t,src] of J){mermaid.initialize({startOnLoad:false,htmlLabels:false,theme:'base',themeVariables:T[t],"
          "flowchart:{htmlLabels:false,curve:'basis',padding:14}});"
          "try{const r=await mermaid.render('d'+Object.keys(o).length,src);o[n+'-'+t]=r.svg;}"
          "catch(e){o['ERROR '+n+'-'+t]=String(e.message||e);}}"
          "document.getElementById('o').textContent=JSON.stringify(o);})();")
    page = f'<html><body><textarea id="o">pending</textarea><script src="{MERMAID}"></script><script>{js}</script></body></html>'
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(page)
    dom = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=20000", "--dump-dom",
                          f"file://{f.name}"], capture_output=True, text=True, timeout=120).stdout
    out = json.loads(html.unescape(re.search(r'<textarea id="o">(.*?)</textarea>', dom, re.S).group(1)))
    for name, svg in out.items():
        if name.startswith("ERROR"):
            raise SystemExit(f"{name}: {svg}")
        svg = svg.replace("<br>", "<br/>")   # standalone SVG must be well-formed XML
        (ROOT / "docs/img" / f"{name}.svg").write_text(svg)
        print("wrote", f"docs/img/{name}.svg", len(svg))


if __name__ == "__main__":
    main()
