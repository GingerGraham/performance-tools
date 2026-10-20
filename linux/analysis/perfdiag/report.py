"""Writes report.html, report.md and findings.json for each host."""

import os

from . import findings, report_html, report_md
from .charts import safe_name


def write_reports(results, outdir, fmt):
    """Returns the paths written. One report per host plus an index page for several hosts."""
    paths = []
    multiple = len(results) > 1
    links = []
    for result in results:
        target = os.path.join(outdir, safe_name(result.host.host)) if multiple else outdir
        os.makedirs(target, exist_ok=True)
        if fmt in ("html", "both"):
            path = os.path.join(target, "report.html")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(report_html.host_report(result))
            paths.append(path)
            links.append(os.path.relpath(path, outdir))
        if fmt in ("md", "both"):
            text, pngs = report_md.host_markdown(result, os.path.join(target, "charts"))
            path = os.path.join(target, "report.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            paths.append(path)
            paths.extend(pngs)
        json_path = os.path.join(target, "findings.json")
        findings.write_json(result, json_path)
        paths.append(json_path)
    if multiple and fmt in ("html", "both"):
        index = os.path.join(outdir, "index.html")
        with open(index, "w", encoding="utf-8") as handle:
            handle.write(report_html.index_page(results, links))
        paths.append(index)
    return paths
