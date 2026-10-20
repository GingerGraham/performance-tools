"""Single-file HTML report: CSS in a <style> block, inline SVG charts, no JavaScript,
no external fonts or scripts, so it opens offline and attaches to a ticket.

Light and dark follow prefers-color-scheme. Charts always render on a light
surface. A print stylesheet makes "Print to PDF" usable.

Everything that came from the bundle (host names, command lines, kernel log
text) is HTML-escaped: bundles are untrusted input.
"""

import datetime
import html

from . import findings
from .charts import inline_svg
from .report_common import (anchor, catalogue_for, charts_for, coverage_lines, evidence_rows,
                            generated_by, header_facts, kernel_hypotheses, layout_facts, settings_rows,
                            summary_hypotheses, time_above_rows)
from .timeutil import stamp_minutes

esc = html.escape

CSS = """
:root {
  --bg: #fafaf8; --surface: #ffffff; --ink: #0b0b0b; --ink-2: #52514e; --rule: #dedcd6;
  --accent: #2a78d6; --crit: #d03b3b; --warn: #b87b00; --ok: #1a7f5a; --code-bg: #f1f0ec;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161615; --surface: #1f1f1d; --ink: #f2f1ed; --ink-2: #a9a7a0; --rule: #34332f;
    --accent: #6aa6ee; --crit: #ee7a7a; --warn: #e2ab3a; --ok: #4fc596; --code-bg: #2a2a27;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.7rem; margin: 0 0 4px; }
h2 { font-size: 1.25rem; margin: 40px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--rule); }
h3 { font-size: 1.05rem; margin: 0 0 6px; }
p { margin: 6px 0; }
a { color: var(--accent); }
code, pre { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 0.88em; }
code { background: var(--code-bg); padding: 1px 4px; border-radius: 3px; }
.meta { color: var(--ink-2); margin: 0 0 16px; }
dl.facts { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 12px 0; }
dl.facts dt { color: var(--ink-2); }
dl.facts dd { margin: 0; overflow-wrap: anywhere; }
.finding { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px;
  padding: 14px 16px; margin: 12px 0; }
.finding h3 span.conf { font-size: 0.78rem; font-weight: 600; padding: 1px 8px; border-radius: 10px;
  border: 1px solid currentColor; margin-left: 6px; white-space: nowrap; }
.conf-high { color: var(--crit); } .conf-medium { color: var(--warn); } .conf-low { color: var(--ink-2); }
.finding .small { color: var(--ink-2); font-size: 0.9rem; }
.tablewrap { overflow-x: auto; margin: 10px 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
ul.steps { margin: 6px 0 0 18px; padding: 0; }
figure.chart { margin: 14px 0; background: #ffffff; border: 1px solid var(--rule); border-radius: 6px;
  padding: 6px; color: #0b0b0b; }
figure.chart svg { display: block; width: 100%; height: auto; }
figcaption { font-size: 0.82rem; color: #52514e; padding: 2px 4px 0; }
.note { color: var(--ink-2); }
.badge-kernel { color: var(--crit); font-weight: 600; }
footer { margin-top: 48px; color: var(--ink-2); font-size: 0.85rem; }
nav.toc ul { list-style: none; padding: 0; margin: 8px 0; display: flex; flex-wrap: wrap; gap: 4px 16px; }
@media print {
  :root { --bg: #fff; --surface: #fff; --ink: #000; --ink-2: #444; --rule: #bbb; --accent: #000; }
  body { font-size: 11pt; background: #fff; }
  main { max-width: none; padding: 0; }
  .finding, figure.chart, tr { break-inside: avoid; }
  h2 { break-after: avoid; }
  a { text-decoration: none; }
  nav.toc { display: none; }
}
"""


def table(headers, rows, numeric_from=None):
    out = ['<div class="tablewrap"><table><thead><tr>']
    out.extend("<th>%s</th>" % esc(h) for h in headers)
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for index, cell in enumerate(row):
            numeric = numeric_from is not None and index >= numeric_from
            out.append('<td class="num">%s</td>' % esc(str(cell)) if numeric else "<td>%s</td>" % esc(str(cell)))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def paragraphs(lines):
    return "".join("<p>%s</p>" % esc(line) for line in lines)


def figure(name, title, svg):
    return '<figure class="chart" id="chart-%s">%s<figcaption>%s</figcaption></figure>' % (
        esc(name), inline_svg(svg, name), esc(title))


def finding_block(result, hypothesis, rendered, catalogue, full):
    """Summary card (full=False) or the detailed section (full=True)."""
    sections = findings.hypothesis_sections(result, hypothesis)
    conf = hypothesis.confidence
    out = ['<article class="finding" id="%s">' % (anchor(hypothesis) if full else anchor(hypothesis) + "-summary")]
    headline = "%s %s — %s" % (hypothesis.id, hypothesis.title, findings.subject_label(result, hypothesis))
    out.append('<h3>%s<span class="conf conf-%s">confidence %s</span></h3>' % (esc(headline), esc(conf), esc(conf)))
    out.append(paragraphs(sections["summary"]))
    out.append(paragraphs(sections["related"]))
    if sections["supporting"]:
        out.append("<p><strong>Supporting:</strong> %s.</p>" % esc("; ".join(sections["supporting"])))
    if sections["against"]:
        out.append("<p><strong>Against:</strong> %s.</p>" % esc("; ".join(sections["against"])))
    for line in sections["processes"]:
        out.append("<p>%s</p>" % esc(line))
    for line in sections["not_seen"]:
        out.append("<p><strong>Not seen:</strong> %s</p>" % esc(line))
    if hypothesis.notes:
        out.append('<p class="note">%s</p>' % esc(" ".join(hypothesis.notes)))
    if not full:
        out.append('<p class="small"><a href="#%s">Evidence, charts and next steps</a></p>' % anchor(hypothesis))
        out.append("</article>")
        return "".join(out)

    rows = evidence_rows(result, hypothesis)
    if rows:
        out.append(table(["Role", "Indicator", "Device", "Window", "Duration", "Mean", "Peak", "Score"], rows, numeric_from=4))
    if sections["kernel"]:
        out.append("<p><span class=\"badge-kernel\">Kernel log events in this window</span></p><ul>")
        out.extend("<li><code>%s</code></li>" % esc(line) for line in sections["kernel"])
        out.append("</ul>")
    for name in charts_for(result, hypothesis, catalogue):
        title = next(chart.title for chart in catalogue if chart.name == name)
        if name in rendered:
            out.append('<p class="small">%s: see <a href="#chart-%s">the chart above</a>.</p>' % (esc(title), esc(name)))
        else:
            rendered[name] = True
            out.append(figure(name, title, _render(catalogue, name)))
    out.append("<h4>Next steps</h4><ul class=\"steps\">")
    out.extend("<li>%s</li>" % esc(step) for step in hypothesis.next_steps)
    out.append("</ul></article>")
    return "".join(out)


_cache = {}


def _render(catalogue, name):
    key = (id(catalogue), name)
    if key not in _cache:
        chart = next(chart for chart in catalogue if chart.name == name)
        _cache[key] = chart.render("svg")
    return _cache[key]


def host_report(result, charts_available=True):
    """Full HTML document for one host."""
    host = result.host
    zone = host.zone
    catalogue = catalogue_for(result)
    _cache.clear()
    parts = []
    parts.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append("<title>Performance diagnosis %s</title>" % esc(host.host))
    parts.append("<style>%s</style></head><body><main>" % CSS)

    parts.append("<h1>Performance diagnosis: %s</h1>" % esc(host.host))
    parts.append('<p class="meta">Generated by %s on %s. Times are in the host zone unless stated.</p>' % (
        esc(generated_by()), esc(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))))
    parts.append('<nav class="toc"><ul>%s</ul></nav>' % "".join(
        '<li><a href="#%s">%s</a></li>' % (target, label) for target, label in (
            ("summary", "Summary"), ("timeline", "Timeline"), ("findings", "Findings"), ("transients", "Transient events"),
            ("time-above", "Time above threshold"), ("layout", "Host and storage"), ("coverage", "Data coverage"),
            ("settings", "Analysis settings"))))
    parts.append('<dl class="facts">%s</dl>' % "".join(
        "<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in header_facts(result)))

    parts.append('<h2 id="summary">Summary</h2>')
    summary = summary_hypotheses(result)
    if summary:
        rendered = {}
        parts.extend(finding_block(result, hypothesis, rendered, catalogue, full=False) for hypothesis in summary)
        measured_count = len(findings.measured_hypotheses(result))
        if measured_count > len(summary):
            parts.append('<p class="note">%d further hypotheses are listed under Findings.</p>' % (
                measured_count - len(summary)))
    else:
        parts.append("<p>No hypothesis was raised: no sustained episode on a primary indicator in this window.</p>")
    from_kernel = kernel_hypotheses(result)
    if from_kernel:
        parts.append('<h3>Kernel log findings</h3><p class="note">Raised by kernel log events alone. They are not '
                     "ranked against the measured hypotheses.</p>")
        rendered = {}
        parts.extend(finding_block(result, hypothesis, rendered, catalogue, full=False) for hypothesis in from_kernel)

    parts.append('<h2 id="timeline">Indicator timeline</h2>')
    if any(chart.name == "timeline" for chart in catalogue):
        parts.append(figure("timeline", "Indicator timeline", _render(catalogue, "timeline")))
    else:
        parts.append("<p>No indicator breached its threshold for a sustained period, and no impactful transient was found.</p>")

    parts.append('<h2 id="findings">Findings</h2>')
    rendered = {"timeline": True}
    if result.hypotheses:
        parts.extend(finding_block(result, hypothesis, rendered, catalogue, full=True) for hypothesis in result.hypotheses)
    else:
        parts.append("<p>None.</p>")

    parts.append('<h2 id="transients">Transient events</h2>')
    if result.impactful:
        rows = [(e.id, e.rule, e.device or "-", stamp_minutes(e.start, zone), findings.duration_text(e.duration_s),
                 "; ".join(e.evidence)) for e in result.impactful]
        parts.append(table(["Id", "Indicator", "Device", "Start", "Duration", "Evidence"], rows))
        if result.omitted_transients:
            parts.append("<p class=\"note\">%d more impactful transients are in findings.json.</p>" % result.omitted_transients)
    else:
        parts.append("<p>No impactful transients.</p>")
    for line in result.transient_summaries:
        parts.append("<p>Repeated: %s.</p>" % esc(line))
    if result.absorbed:
        parts.append("<p>Absorbed transients (no paired latency, queue or pressure indicator breached): "
                     "%d.</p>" % sum(result.absorbed.values()))
        parts.append(table(["Indicator", "Absorbed bursts"], sorted(result.absorbed.items()), numeric_from=1))

    parts.append('<h2 id="time-above">Time above threshold</h2>')
    parts.append("<p class=\"note\">Share of samples past the warning and critical thresholds over the whole window. "
                 "Tail and extreme columns are the 95th percentile and maximum, or for rules that fire when a value "
                 "is too low, the 5th percentile and minimum.</p>")
    rows = time_above_rows(result)
    parts.append(table(["Indicator", "Role", "Past warn", "Past crit", "Median", "Tail", "Extreme", "Fires when"], rows)
                 if rows else "<p>No indicators were evaluated.</p>")

    parts.append('<h2 id="layout">Host and storage layout</h2>')
    layout = layout_facts(host)
    if layout is None:
        parts.append("<p>No hardware inventory in the bundle. Run collect-hw-inventory.sh on the host and gather again.</p>")
    else:
        facts, disks, mounts = layout
        parts.append('<dl class="facts">%s</dl>' % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in facts))
        parts.append(table(["Device", "Kernel name", "Class", "Size", "Model", "Parallelism", "Built on"], disks))
        if mounts:
            parts.append(table(["Mount point", "Type", "Block device"], mounts))

    parts.append('<h2 id="coverage">Data coverage and limits</h2>')
    parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % esc(line) for line in coverage_lines(result)))

    parts.append('<h2 id="settings">Analysis settings</h2>')
    parts.append(table(["Setting", "Value", "Source"], settings_rows(result)))
    parts.append("<p class=\"note\">Every other threshold is the built-in default (engineering starting points, not vendor figures).</p>")

    parts.append("<footer>%s. This report is a single file with no scripts or external resources.</footer>" % esc(generated_by()))
    parts.append("</main></body></html>")
    return "".join(parts)


def index_page(results, links):
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
             "<title>Performance diagnosis index</title><style>%s</style></head><body><main>" % CSS,
             "<h1>Performance diagnosis</h1><p class=\"meta\">%d hosts</p>" % len(results)]
    rows = []
    for result, link in zip(results, links):
        top = result.hypotheses[0] if result.hypotheses else None
        rows.append('<tr><td><a href="%s">%s</a></td><td>%s</td></tr>' % (
            esc(link), esc(result.host.host),
            esc("%s %s (%s)" % (top.id, top.title, top.confidence)) if top else "no hypothesis raised"))
    parts.append('<div class="tablewrap"><table><thead><tr><th>Host</th><th>Top finding</th></tr></thead><tbody>%s'
                 "</tbody></table></div></main></body></html>" % "".join(rows))
    return "".join(parts)
