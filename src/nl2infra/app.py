"""NL2Infra UI. Shows the fix loop: what each draft changed and which violations each round removed.

The UI only reads RunState and the run log. Replay mode draws a saved run log and
needs no API key and no cluster.
"""
import html
import os
import sys

import streamlit as st
from dotenv import load_dotenv

# Ensure src and root are in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

load_dotenv()

import ui_data as ui
from storage import run_log

RUNS_DIR = os.environ.get("NL2INFRA_RUNS_DIR", "runs")
ROLES = ["junior_dev", "senior_dev", "platform_admin"]
LIVE, REPLAY = "Live run", "Replay a saved run"
TOOL_COLOURS = {"checkov": "#B3263A", "opa": "#15150F", "dry-run": "#6D6A60", "plan": "#E7CCCB"}

st.set_page_config(page_title="NL2Infra", layout="wide", initial_sidebar_state="collapsed")

HEADER = 'div[data-testid="stVerticalBlock"]:has(> div.element-container span.nl-header-marker)'
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap');
:root { --bg:#F2F0EB; --panel:#FBFAF7; --line:#D6D2C8; --ink:#15150F; --muted:#6D6A60; --red:#B3263A; --red-dark:#CC3445; --tint:#E7CCCB; }
html, body, [data-testid="stAppViewContainer"], .stApp { background:var(--bg); color:var(--ink); font-family:'Archivo', 'Helvetica Neue', Arial, sans-serif; }
* { border-radius:0 !important; box-shadow:none !important; }
header[data-testid="stHeader"], [data-testid="collapsedControl"], [data-testid="stToolbar"] { display:none; }
.block-container { padding:1.2rem 2rem 3rem 2rem; max-width:100%; }
h1, h2, h3, p, label, li, span, div { font-family:'Archivo', 'Helvetica Neue', Arial, sans-serif; }
code, pre, .mono { font-family:'IBM Plex Mono', Menlo, monospace; }
div.element-container:has(span.nl-header-marker) { display:none; }

/* header bar */
HEADER { background:var(--ink); padding:14px 20px 6px 20px; }
HEADER label p, HEADER [data-testid="stWidgetLabel"] p { color:#B9B5AA !important; font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; }
HEADER [role="radiogroup"] p { color:var(--panel) !important; font-family:'Archivo', sans-serif; font-size:14px; letter-spacing:0; text-transform:none; }
.nl-brand { color:var(--panel); font-weight:700; font-size:26px; line-height:1.1; }
.nl-brand-sub { color:#B9B5AA; font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; margin-top:4px; }
.nl-hval { color:var(--panel); font-family:'IBM Plex Mono', monospace; font-size:14px; margin-top:6px; word-break:break-all; }

/* labels and panels */
.nl-label { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin:0 0 6px 0; }
.nl-title { font-weight:700; font-size:20px; margin:18px 0 8px 0; }
.nl-panel { background:var(--panel); border:1px solid var(--line); padding:12px 14px; }
.nl-note { color:var(--muted); font-size:14px; }
.nl-red { color:var(--red); }
.nl-line { background:var(--panel); border:1px solid var(--line); padding:10px 14px; font-size:14px; }
.nl-line summary { cursor:pointer; list-style:none; }
.nl-line b { font-family:'IBM Plex Mono', monospace; font-weight:500; }

/* tables */
table.nl-table { width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); font-size:13px; }
table.nl-table th { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-weight:500; }
table.nl-table td { padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
table.nl-table td.m { font-family:'IBM Plex Mono', monospace; font-size:12.5px; white-space:nowrap; }
table.nl-table tr.fixed td { color:var(--muted); }
table.nl-table td.st-fail { color:var(--red); font-family:'IBM Plex Mono', monospace; font-size:12px; text-transform:uppercase; letter-spacing:.08em; white-space:nowrap; }
table.nl-table td.st-ok { font-family:'IBM Plex Mono', monospace; font-size:12px; text-transform:uppercase; letter-spacing:.08em; white-space:nowrap; }

/* timeline */
.nl-step { background:var(--panel); border:1px solid var(--line); padding:10px 12px; position:relative; }
.nl-step.sel { outline:2px solid var(--ink); outline-offset:-2px; }
.nl-step.now { outline:2px dashed var(--ink); outline-offset:-2px; }
.nl-step .lab { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
.nl-step .big { font-family:'IBM Plex Mono', monospace; font-size:46px; font-weight:600; line-height:1.05; margin-top:6px; }
.nl-step .big.bad { color:var(--red); }
.nl-step .cap { font-family:'IBM Plex Mono', monospace; font-size:10px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin-bottom:8px; }
.nl-step .tools { font-family:'IBM Plex Mono', monospace; font-size:11px; color:var(--muted); display:grid; grid-template-columns:1fr auto; row-gap:1px; text-transform:uppercase; letter-spacing:.06em; }
.nl-step .tools b { color:var(--ink); font-weight:500; text-align:right; }
.nl-step .tools b.bad { color:var(--red); }
.nl-step .status { font-weight:700; font-size:22px; margin-top:10px; text-transform:uppercase; letter-spacing:.02em; }
.nl-step.bad { border-color:var(--red); }
.nl-step.bad .status, .nl-step .reason { color:var(--red); }
.nl-step .reason { font-size:13px; margin-top:6px; word-break:break-word; }
.nl-step .sub { font-size:13px; color:var(--muted); margin-top:6px; word-break:break-word; }
.nl-step .sub.ink { color:var(--ink); }
/* Result and stopped cards span a round card plus its View button, so the strip is one height. */
.nl-step { height:192px; overflow:hidden; }
.nl-step.tall { height:230px; overflow:auto; }
.nl-strip .nl-step, .nl-strip .nl-step.tall { height:192px; }
.nl-chart { height:230px; }
.nl-strip { display:flex; gap:10px; }
.nl-strip .nl-step { flex:1 1 0; min-width:0; }

/* chart */
.nl-chart { background:var(--panel); border:1px solid var(--line); padding:10px 12px; }
.nl-bars { display:flex; align-items:flex-end; gap:10px; height:104px; border-bottom:1px solid var(--ink); margin-top:6px; }
.nl-bar { flex:1 1 0; display:flex; flex-direction:column; justify-content:flex-end; align-items:stretch; height:100%; }
.nl-bar .n { font-family:'IBM Plex Mono', monospace; font-size:11px; text-align:center; }
.nl-axis { display:flex; gap:10px; font-family:'IBM Plex Mono', monospace; font-size:10px; color:var(--muted); text-transform:uppercase; }
.nl-axis span { flex:1 1 0; text-align:center; white-space:nowrap; text-transform:none; }
.nl-legend { font-family:'IBM Plex Mono', monospace; font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:.06em; margin-top:6px; }
.nl-legend i { display:inline-block; width:9px; height:9px; margin:0 4px 0 8px; border:1px solid var(--ink); }

/* code and diff */
.nl-code { background:var(--ink); color:var(--panel); border:1px solid var(--ink); max-height:72vh; overflow:auto; }
table.nl-diff { width:100%; border-collapse:collapse; table-layout:fixed; font-family:'IBM Plex Mono', monospace; font-size:12.5px; line-height:1.5; }
table.nl-diff th { position:sticky; top:0; background:#2A2A22; color:#B9B5AA; font-size:11px; letter-spacing:.14em; text-transform:uppercase; text-align:left; padding:7px 10px; font-weight:500; z-index:1; }
table.nl-diff td { padding:0 8px; vertical-align:top; white-space:pre-wrap; word-break:break-all; font-family:'IBM Plex Mono', monospace; }
table.nl-diff td.n.hit { color:var(--red-dark); font-weight:600; }
table.nl-diff td.n { width:44px; color:#6D6A60; text-align:right; user-select:none; }
table.nl-diff td.eq { color:#8D897D; }
table.nl-diff td.rm { background:rgba(204,52,69,.30); color:var(--panel); box-shadow:inset 3px 0 0 var(--red-dark) !important; }
table.nl-diff td.ad { background:var(--line); color:var(--ink); }
table.nl-diff td.vd { background:#1D1D16; }
table.nl-diff td.mark { background:rgba(204,52,69,.14); }
table.nl-diff td.sep { width:10px; background:#2A2A22; padding:0; }
table.nl-diff td.skip { background:#2A2A22; color:#B9B5AA; text-align:center; font-size:11px; letter-spacing:.12em; text-transform:uppercase; padding:4px 0; }
.nl-code.fixed { height:460px; max-height:460px; }
.nl-fail { background:var(--panel); border:1px solid var(--red); border-left:4px solid var(--red); padding:8px 12px; margin-bottom:8px; font-size:13.5px; }
.nl-fail .head { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--red); margin-bottom:4px; }
.nl-fail b { font-family:'IBM Plex Mono', monospace; font-weight:600; color:var(--red); font-size:12.5px; }
.nl-fail.ok { border-color:var(--line); border-left:4px solid var(--line); color:var(--muted); }
.nl-request { background:var(--panel); border:1px solid var(--line); padding:12px 14px; font-size:16px; line-height:1.45; margin-bottom:10px; }
[data-testid="stExpander"] details { background:var(--panel); border:1px solid var(--line); }
[data-testid="stExpander"] summary p { font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--ink); }

/* summary */
.nl-summary { display:flex; background:var(--ink); color:var(--panel); margin-top:22px; }
.nl-summary div { flex:1 1 0; padding:12px 16px; border-right:1px solid #3A3A30; min-width:0; }
.nl-summary .k { font-family:'IBM Plex Mono', monospace; font-size:10px; letter-spacing:.14em; text-transform:uppercase; color:#B9B5AA; border:0; padding:0; }
.nl-summary .v { font-family:'IBM Plex Mono', monospace; font-size:16px; border:0; padding:2px 0 0 0; word-break:break-all; }
.nl-summary .v.bad { color:var(--red-dark); }

/* arms */
.nl-arm { background:var(--panel); border:1px solid var(--line); padding:10px 12px; margin-bottom:8px; }
.nl-arm .verdict { font-weight:700; font-size:22px; text-transform:uppercase; }
.nl-arm .verdict.bad { color:var(--red); }
.nl-file { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); margin:10px 0 4px 0; }

/* pipeline strip */
.nl-pipe { background:var(--panel); border:1px solid var(--line); padding:10px 6px 10px 6px; }
.nl-pipe .arc { position:relative; height:44px; }
.nl-pipe .arc svg { position:absolute; left:0; top:0; width:100%; height:44px; }
.nl-pipe .arc .lab { position:absolute; left:62.5%; top:0; transform:translateX(-50%); font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.08em; text-transform:uppercase; white-space:nowrap; background:var(--panel); padding:0 6px; color:var(--muted); }
.nl-pipe .arc.on .lab { color:var(--ink); font-weight:600; }
.nl-pipe .arc .head { position:absolute; left:56.25%; bottom:-1px; transform:translateX(-50%); width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; border-top:8px solid var(--line); }
.nl-pipe .arc.on .head { border-top-color:var(--ink); }
.nl-pipe .row { display:grid; grid-template-columns:repeat(8, minmax(0, 1fr)); }
.nl-pipe .cell { padding:0 11px; position:relative; min-width:0; }
.nl-pipe .cell:not(:last-child)::after { content:"→"; position:absolute; right:-7px; top:50%; transform:translateY(-50%); font-family:'IBM Plex Mono', monospace; font-size:14px; color:var(--muted); }
.nl-pipe .box { min-height:46px; display:flex; align-items:center; justify-content:center; text-align:center; padding:4px 4px; border:1px solid var(--line); background:var(--panel); color:var(--muted); font-family:'IBM Plex Mono', monospace; font-size:11.5px; letter-spacing:.08em; text-transform:uppercase; line-height:1.25; overflow-wrap:anywhere; }
.nl-pipe .box.done { background:var(--ink); border-color:var(--ink); color:var(--panel); }
.nl-pipe .box.failed, .nl-pipe .box.current { background:var(--red); border-color:var(--red); color:var(--panel); }
.nl-pipe .box.not_built { border:1px dashed var(--muted); background:transparent; }
.nl-pipe .box.skipped { border:1px solid var(--muted); }
.nl-pipe .note { font-family:'IBM Plex Mono', monospace; font-size:11px; color:var(--muted); text-align:center; padding:6px 4px 0 4px; line-height:1.3; overflow-wrap:anywhere; }
.nl-pipe .note.bad { color:var(--red); }
.nl-story { font-size:19px; font-weight:600; line-height:1.4; margin:12px 0 16px 0; padding-left:12px; border-left:4px solid var(--ink); }
.nl-story.bad, .nl-story.live { border-left-color:var(--red); }
.nl-story.live .nl-tag { margin:0 8px 0 0; vertical-align:2px; }
.nl-gloss { font-family:'IBM Plex Mono', monospace; font-size:11.5px; color:var(--muted); margin:-2px 0 10px 0; }
.nl-gloss b { color:var(--ink); font-weight:600; }
.nl-key { font-family:'IBM Plex Mono', monospace; font-size:11.5px; color:var(--muted); margin:2px 0 6px 0; }
.nl-key i { display:inline-block; width:22px; height:11px; margin:0 6px 0 0; vertical-align:-1px; border:1px solid var(--ink); }
.nl-key span { margin-right:18px; }

/* violation cards */
.nl-vcard { display:grid; grid-template-columns:minmax(0, 1.05fr) minmax(0, 1.25fr) minmax(0, 1.5fr); background:var(--panel); border:1px solid var(--line); margin-bottom:8px; }
.nl-vcard.fixed { border-left:4px solid var(--ink); }
.nl-vcard.failing, .nl-vcard.new { border-left:4px solid var(--red); }
.nl-vcard > div { padding:10px 14px; min-width:0; }
.nl-vcard > div + div { border-left:1px solid var(--line); }
.nl-vcard .name { font-weight:700; font-size:17px; line-height:1.25; }
.nl-vcard .id { font-family:'IBM Plex Mono', monospace; font-size:11.5px; color:var(--muted); margin-top:4px; overflow-wrap:anywhere; }
.nl-vcard .what { font-size:14px; line-height:1.4; }
.nl-vcard .src { font-family:'IBM Plex Mono', monospace; font-size:11px; color:var(--muted); margin-top:6px; overflow-wrap:anywhere; }
.nl-vcard .ev { font-family:'IBM Plex Mono', monospace; font-size:12.5px; background:var(--ink); color:var(--panel); padding:4px 8px; margin-top:4px; overflow-wrap:anywhere; }
.nl-vcard .evnote { font-size:13.5px; color:var(--muted); line-height:1.4; }
.nl-tag { display:inline-block; font-family:'IBM Plex Mono', monospace; font-size:10.5px; letter-spacing:.12em; text-transform:uppercase; padding:2px 7px; border:1px solid var(--ink); margin-bottom:6px; }
.nl-tag.bad { border-color:var(--red); color:var(--red); }
@media (max-width: 900px) {
  .nl-vcard { grid-template-columns:1fr; }
  .nl-vcard > div + div { border-left:0; border-top:1px solid var(--line); }
  .nl-pipe .box { font-size:9.5px; letter-spacing:.02em; }
  .nl-pipe .cell { padding:0 7px; }
  .nl-pipe .note { font-size:9.5px; }
  .nl-story { font-size:16px; }
  .nl-step { height:auto; min-height:206px; overflow:visible; }
  .nl-step.tall { height:auto; min-height:244px; }
  .nl-chart { height:auto; min-height:244px; }
  .nl-step .big { font-size:38px; }
  .nl-summary { flex-wrap:wrap; }
  .nl-summary div { flex:1 1 30%; }
}
/* header: wrap into tidy blocks when the window is narrow */
HEADER [data-testid="stHorizontalBlock"] { flex-wrap:wrap; row-gap:18px; }
HEADER [data-testid="column"] { min-width:170px; flex:1 1 170px !important; }

/* Streamlit widgets */
.stButton > button, .stDownloadButton > button { font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.12em; text-transform:uppercase; border:1px solid var(--ink); background:var(--panel); color:var(--ink); padding:.45rem .8rem; }
.stButton > button:hover { border-color:var(--red); color:var(--red); }
.stButton > button[kind="primary"] { background:var(--red); border-color:var(--red); color:var(--panel); }
.stButton > button[kind="primary"]:hover { background:var(--ink); border-color:var(--ink); color:var(--panel); }
.stButton > button p { font-family:'IBM Plex Mono', monospace; font-size:12px; }
textarea, input, [data-baseweb="select"] > div { background:var(--panel) !important; border-color:var(--line) !important; font-family:'Archivo', sans-serif !important; color:var(--ink) !important; }
[data-testid="stWidgetLabel"] p { font-family:'IBM Plex Mono', monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
[role="radiogroup"] p { font-family:'IBM Plex Mono', monospace; font-size:12.5px; letter-spacing:0; text-transform:none; color:var(--ink); }
.stTabs [data-baseweb="tab-list"] { gap:0; border-bottom:1px solid var(--ink); }
.stTabs [data-baseweb="tab"] { font-family:'IBM Plex Mono', monospace; font-size:12px; letter-spacing:.14em; text-transform:uppercase; padding:10px 18px; color:var(--muted); }
.stTabs [aria-selected="true"] { color:var(--ink); background:var(--panel); }
.stTabs [data-baseweb="tab-highlight"] { background:var(--red); }
</style>
""".replace("HEADER", HEADER)


# ------------------------------------------------------------------ html helpers

def esc(value) -> str:
    """Escapes text for HTML. '$' is escaped too, so Streamlit never reads YAML as maths."""
    return html.escape("" if value is None else str(value)).replace("$", "&#36;")


def show(markup: str) -> None:
    # One line, starting with a tag: Markdown then passes it through as a raw HTML block.
    st.markdown(markup.replace("\n", ""), unsafe_allow_html=True)


def label(text: str) -> str:
    return f'<div class="nl-label">{esc(text)}</div>'


def pipeline_strip(run: dict, waiting: bool = False, current: str = None) -> str:
    """Section 1 of the Run tab: the eight stages, what this run did in each, and the fix loop."""
    view = ui.pipeline_stages(run, waiting, current=current)
    boxes = "".join(f'<div class="cell"><div class="box {esc(stage["state"])}">{esc(stage["label"])}</div></div>'
                    for stage in view["stages"])
    notes = "".join(
        f'<div class="note {"bad" if stage["state"] in ("failed", "current") else ""}">{esc(stage["note"]) or "&nbsp;"}</div>'
        for stage in view["stages"])
    if view["looped"]:
        loop_label = view["loop"]
    elif run and run.get("status") == "passed":
        loop_label = "no loop needed"
    else:
        loop_label = "fix loop, up to 5 rounds"
    stroke = "#15150F" if view["looped"] else "#D6D2C8"
    # The arc runs from Fix (6th box) back to the Gauntlet (5th box): centres at 68.75% and 56.25%.
    arc = (f'<div class="arc {"on" if view["looped"] else ""}"><svg viewBox="0 0 800 44" preserveAspectRatio="none" aria-hidden="true">'
           f'<path d="M 550 44 C 550 8, 450 8, 450 38" fill="none" stroke="{stroke}" stroke-width="2" '
           f'vector-effect="non-scaling-stroke" {"" if view["looped"] else 'stroke-dasharray="5 4"'}/></svg>'
           f'<span class="lab">{esc(loop_label)}</span><i class="head"></i></div>')
    return f'<div class="nl-pipe">{arc}<div class="row">{boxes}</div><div class="row">{notes}</div></div>'


def status_line(text: str) -> str:
    """The one-line status of a run in progress, in the place the story line takes once it has finished."""
    return f'<div class="nl-story live"><span class="nl-tag bad">Live</span> {esc(text)}</div>'


def story_line(run: dict, waiting: bool = False) -> str:
    bad = bool(run) and run.get("status") in ("escalated", "rejected")
    return f'<div class="nl-story {"bad" if bad else ""}">{esc(ui.story(run, waiting))}</div>'


TOOL_GLOSS_LINE = ('<div class="nl-gloss"><b>Gauntlet: 4 checkers.</b> ' + " &nbsp;&middot;&nbsp; ".join(
    f'<b>{html.escape(ui.TOOL_LABELS[tool])}</b> = {html.escape(ui.TOOL_GLOSS[tool])}' for tool in ui.TOOLS) + '</div>')


def violation_cards(cards: list) -> str:
    if not cards:
        return '<div class="nl-panel nl-note">No violations in this round or the one before it.</div>'
    out = ""
    for card in cards:
        kind = {"fixed in this round": "fixed", "still failing": "failing", "new in this round": "new"}[card["status"]]
        tag = {"fixed": "Fixed in this round", "failing": "Still failing", "new": "New"}[kind]
        where = " &middot; ".join(esc(part) for part in (card.get("file"), ui.where(card)) if part and part != "-")
        if card["known"]:
            what = f'<div class="what">{esc(card["why"])}</div><div class="src">Checker says: {esc(card["message"])}</div>'
        else:
            # No entry in the rule dictionary: the title is the checker's own message and nothing is added to it.
            what = '<div class="what nl-note">No plain-English description is on file for this rule. The title is the checker\'s own message.</div>'
        evidence = "".join(f'<div class="ev">{esc(line)}</div>' for line in card["evidence"])
        if card["evidence"]:
            evidence = '<div class="evnote">Changed lines in the diff:</div>' + evidence
        else:
            evidence = f'<div class="evnote">{esc(card["evidence_note"])}</div>'
        out += (f'<div class="nl-vcard {kind}"><div>{label("Rule")}<div class="name">{esc(card["name"])}</div>'
                f'<div class="id">{esc(card["rule_id"])} &middot; {esc(ui.TOOL_LABELS.get(card["tool"], card["tool"]))}</div></div>'
                f'<div>{label("What was wrong")}{what}<div class="src">{where}</div></div>'
                f'<div>{label("How it was fixed")}<span class="nl-tag {"" if kind == "fixed" else "bad"}">{tag}</span>{evidence}</div></div>')
    return out


def plan_table(plan: dict) -> str:
    rows = ""
    for res in plan.get("resources", []):
        spec = res.get("spec") or {}
        images = [spec["image"]] if isinstance(spec.get("image"), str) else [
            c.get("image") for c in spec.get("containers") or [] if isinstance(c, dict) and c.get("image")]
        rows += (f'<tr><td class="m">{esc(res.get("type"))}</td><td class="m">{esc(res.get("name"))}</td>'
                 f'<td class="m">{esc(res.get("namespace") or "(cluster)")}</td>'
                 f'<td class="m">{esc(", ".join(images) or "-")}</td><td class="m">{esc(spec.get("replicas", "-"))}</td></tr>')
    return ('<table class="nl-table"><tr><th>Kind</th><th>Name</th><th>Namespace</th><th>Image</th><th>Replicas</th></tr>'
            f'{rows}</table>')


def round_card(step: dict, selected: bool = False) -> str:
    tools = "".join(
        f'<span title="{esc(ui.TOOL_GLOSS[tool])}">{esc(ui.TOOL_LABELS[tool])}</span>'
        f'<b class="{"bad" if step["tools"].get(tool) else ""}">{step["tools"].get(tool, 0)}</b>'
        for tool in ui.TOOLS)
    return (f'<div class="nl-step {"sel" if selected else ""}"><div class="lab">{esc(step["label"])} &rarr; Gauntlet</div>'
            f'<div class="big {"bad" if step["count"] else ""}">{step["count"]}</div><div class="cap">violations</div>'
            f'<div class="tools">{tools}</div></div>')


def stage_card(step: dict) -> str:
    return (f'<div class="nl-step tall bad"><div class="lab">{esc(step["label"])}</div>'
            '<div class="status">Stopped</div><div class="reason">This step did not complete.</div></div>')


def end_card(step: dict, log: dict) -> str:
    extra = ""
    if step["passed"]:
        rounds = log.get("iterations") or 0
        extra = f'<div class="sub">{"First draft, no fix needed" if rounds == 0 else f"After {rounds} fix round(s)"}</div>'
        headline, detail = ui.pull_request_note(log)
        extra += f'<div class="sub ink">{esc(headline)}</div>'
        if detail:
            extra += f'<div class="sub">{esc(detail)[:200]}</div>'
    elif step["reason"]:
        extra = f'<div class="reason">{esc(ui.plain_reason(step["reason"]))}</div>'
    elif not step["running"] and log.get("arm") in ("A", "B"):
        extra = f'<div class="reason">Arm {esc(log["arm"])}: one call, no fix loop. Violations remain.</div>'
    bad = not step["passed"] and not step["running"]
    return (f'<div class="nl-step tall {"bad" if bad else ""} {"now" if step["running"] else ""}"><div class="lab">Result</div>'
            f'<div class="status">{esc(step["label"])}</div>{extra}</div>')


def live_strip(log: dict, doing: str) -> str:
    """The timeline while a run is in progress: finished rounds, then the step being worked on."""
    cards = "".join(round_card(s) for s in ui.timeline(log) if s["kind"] == "round")
    return (f'<div class="nl-strip">{cards}<div class="nl-step now"><div class="lab">In progress</div>'
            f'<div class="status">{esc(doing)}</div><div class="sub">This strip fills in as each step finishes.</div></div></div>')


def chart(rounds: list) -> str:
    totals = [len(r["violations"]) for r in rounds]
    top = max(totals + [1])
    bars = ""
    for entry in rounds:
        counts = ui.tool_counts(entry["violations"])
        segments = "".join(
            f'<div title="{esc(ui.TOOL_LABELS[t])}: {counts[t]}" style="height:{counts[t] / top * 84:.1f}px;background:{TOOL_COLOURS[t]};'
            f'border:1px solid #15150F;border-bottom:0"></div>' for t in ui.TOOLS if counts[t])
        bars += f'<div class="nl-bar"><div class="n">{len(entry["violations"])}</div>{segments}</div>'
    axis = "".join(f'<span>{esc(ui.round_label(r["round"]))}</span>' for r in rounds)
    legend = "".join(f'<i style="background:{TOOL_COLOURS[t]}"></i>{esc(ui.TOOL_LABELS[t])}' for t in ui.TOOLS)
    return (f'<div class="nl-chart">{label("Violations per round, by tool")}<div class="nl-bars">{bars}</div>'
            f'<div class="nl-axis">{axis}</div><div class="nl-legend">{legend}</div></div>')


def line_cell(marks: dict, line) -> str:
    """The line-number cell. A line a tool pointed at (never line 1) is shown in red, with the rules on hover."""
    rules = marks.get(line) or []
    if rules:
        return f'<td class="n hit" title="{esc(", ".join(rules))}">{line}</td>'
    return f'<td class="n">{line or ""}</td>'


def diff_table(rows: list, marks: dict, left_title: str, right_title: str) -> str:
    body = ""
    for row in rows:
        if row["tag"] == "skip":
            body += f'<tr><td class="skip" colspan="5">&middot;&middot;&middot; {row["count"]} unchanged line{"" if row["count"] == 1 else "s"}</td></tr>'
            continue
        left_class = {"equal": "eq", "removed": "rm", "changed": "rm", "added": "vd"}[row["tag"]]
        right_class = {"equal": "eq", "removed": "vd", "changed": "ad", "added": "ad"}[row["tag"]]
        if row["left_no"] in marks and left_class == "eq":
            left_class = "eq mark"
        body += (f'<tr>{line_cell(marks, row["left_no"])}'
                 f'<td class="{left_class}">{esc(row["left"])}</td><td class="sep"></td>'
                 f'<td class="n">{row["right_no"] or ""}</td><td class="{right_class}">{esc(row["right"])}</td></tr>')
    return ('<div class="nl-code"><table class="nl-diff"><tr><th style="width:44px"></th>'
            f'<th>{esc(left_title)}</th><th style="width:10px"></th><th style="width:44px"></th><th>{esc(right_title)}</th></tr>'
            f'{body}</table></div>')


def failure_strip(failures: list, draft: str) -> str:
    """What the draft on the left failed, by rule. Checkov gives no useful line, so this is not in the gutter."""
    if not failures:
        return f'<div class="nl-fail ok">{esc(draft)} had no violations in this file.</div>'
    items = "".join(f'<div><b>{esc(rule)}</b> &ndash; {esc(message)}</div>' for rule, message in failures)
    return f'<div class="nl-fail"><div class="head">{esc(draft)} failed:</div>{items}</div>'


def file_table(text: str, marks: dict, title: str) -> str:
    """One file, full width. Used when there is nothing to compare."""
    body = "".join(
        f'<tr>{line_cell(marks, number)}<td class="{"mark" if number in marks else ""}">{esc(line)}</td></tr>'
        for number, line in enumerate(text.splitlines(), start=1))
    return (f'<div class="nl-code"><table class="nl-diff"><tr><th style="width:44px"></th>'
            f'<th>{esc(title)}</th></tr>{body}</table></div>')


def violations_table(rows: list) -> str:
    if not rows:
        return '<div class="nl-panel nl-note">No violations in this round or the one before it.</div>'
    body = ""
    for row in rows:
        fixed = row["status"] == "fixed in this round"
        body += (f'<tr class="{"fixed" if fixed else ""}"><td class="{"st-ok" if fixed else "st-fail"}">{esc(row["status"])}</td>'
                 f'<td class="m">{esc(ui.TOOL_LABELS.get(row["tool"], row["tool"]))}</td><td class="m">{esc(row["rule_id"])}</td>'
                 f'<td class="m">{esc(row["severity"])}</td><td class="m">{esc(row.get("file") or "-")}</td>'
                 f'<td class="m">{esc(ui.where(row))}</td><td>{esc(row["message"])}</td></tr>')
    # A real line number is shown as "line N"; line 1 or no line means the tool only named the resource.
    where_head = "Line" if all(ui.has_real_line(row) for row in rows) else \
        "Resource" if not any(ui.has_real_line(row) for row in rows) else "Line or resource"
    return ('<table class="nl-table"><tr><th>Status</th><th>Tool</th><th>Rule ID</th><th>Severity</th><th>File</th>'
            f'<th>{where_head}</th><th>Message</th></tr>{body}</table>')


def summary_strip(log: dict, log_path: str) -> str:
    metrics = log.get("metrics") or {}
    status = log.get("status") or "running"
    cells = [
        ("Final status", status.upper(), status not in ("passed", "running")),
        ("Fix rounds", log.get("iterations", 0), False),
        ("Runtime", f'{metrics.get("total_time", 0):.1f} s', False),
        ("Tokens in", f'{metrics.get("tokens_in", 0):,}', False),
        ("Tokens out", f'{metrics.get("tokens_out", 0):,}', False),
        ("Run log", log_path, False),
    ]
    return '<div class="nl-summary">' + "".join(
        f'<div><div class="k">{esc(k)}</div><div class="v {"bad" if bad else ""}">{esc(v)}</div></div>' for k, v, bad in cells
    ) + "</div>"


# ------------------------------------------------------------------ live pipeline

@st.cache_resource
def get_pipeline():
    # Built only for a live run. One per server process: it holds the runs waiting for approval.
    from pipeline import Pipeline
    return Pipeline(auto_approve=False, run_dir=RUNS_DIR)


def live_view():
    """(log, awaiting_approval, log_path) for the live run in this session, or (None, False, None)."""
    request_id = st.session_state.get("live_id")
    if not request_id:
        return None, False, None
    path = os.path.join(RUNS_DIR, f"{request_id}.json")
    try:
        pipeline = get_pipeline()
        return run_log(pipeline.state(request_id)), pipeline.awaiting_approval(request_id), path
    except Exception:
        # The server restarted and lost the in-memory run; fall back to its saved log if it finished.
        return ui.load_log(path), False, path


# ------------------------------------------------------------------ page

st.markdown(CSS, unsafe_allow_html=True)

with st.container():
    show('<span class="nl-header-marker"></span>')
    brand, model_slot, role_col, mode_col, log_col = st.columns([3, 3, 2, 3, 4])
    with brand:
        show('<div class="nl-brand">NL2Infra</div><div class="nl-brand-sub">Natural language to Kubernetes, checked</div>')
    mode = mode_col.radio("Mode", [LIVE, REPLAY], key="mode")
    saved_logs = ui.list_run_logs(RUNS_DIR)
    replay_choice = None
    if mode == REPLAY:
        if saved_logs:
            replay_choice = log_col.selectbox(f"Saved run (from {RUNS_DIR}/)", saved_logs, key="replay_log")
        else:
            with log_col:
                show(f'<div class="nl-hval">No run logs in {esc(RUNS_DIR)}/</div>')
    role = role_col.selectbox("Role", ROLES, key="role") if mode == LIVE else None

if mode == REPLAY:
    log_path = os.path.join(RUNS_DIR, replay_choice) if replay_choice else None
    log = ui.load_log(log_path) if log_path else None
    awaiting = False
else:
    log, awaiting, log_path = live_view()

with model_slot:
    if log:
        model_id = log.get("model_id") or log.get("model")
    else:
        model_id = "mock" if os.environ.get("MOCK_MODE", "false").lower() == "true" else os.environ.get("MODEL_NAME", "not set")
    show(f'<div class="nl-brand-sub">Model</div><div class="nl-hval">{esc(model_id)}</div>')
if mode == REPLAY:
    with role_col:
        saved_role = (log.get("role") or log.get("user_role")) if log else "-"
        show(f'<div class="nl-brand-sub">Role (saved run)</div><div class="nl-hval">{esc(saved_role)}</div>')

tab_run, tab_arms = st.tabs(["Run", "Compare arms"])


# ------------------------------------------------------------------ tab 1: run

def render_request_and_plan():
    """Section 2. The open form before approval; one line after it."""
    finished = log is not None and not awaiting
    if finished:
        plan = log.get("approved_plan") or log.get("plan")
        prompt = log.get("prompt") or log.get("user_prompt") or ""
        show(label("Request") + f'<div class="nl-request">{esc(prompt)}</div>')
        if plan:
            count = len(plan["resources"])
            state_word = "Plan approved" if log.get("approved_plan") else "Plan not approved"
            with st.expander(f"{state_word}: {count} resource{'' if count == 1 else 's'}"):
                show(plan_table(plan))
        else:
            show('<div class="nl-panel nl-note">No plan was made for this request.</div>')
        if mode == LIVE and st.button("New request", key="new_request"):
            st.session_state.pop("live_id", None)
            st.rerun()
        return None

    left, right = st.columns([2, 3])
    with left:
        show(label("Request"))
        prompt = st.text_area(
            "Request", key="request", height=150, label_visibility="collapsed", disabled=awaiting,
            placeholder="e.g. A Redis cache with one replica in the dev namespace, using redis:7.2-alpine, with a ClusterIP service on port 6379.",
        )
        if not awaiting and st.button("Submit request", type="primary", key="submit"):
            if not prompt.strip():
                show('<div class="nl-panel nl-red">Enter a request first.</div>')
                return None
            try:
                pipeline = get_pipeline()
            except Exception as error:
                show(f'<div class="nl-panel nl-red">The live pipeline could not start: {esc(error)}. '
                     'Replay a saved run instead, or set the API key in .env.</div>')
                return None
            with st.spinner("Checking the request and planning"):
                state = pipeline.start(prompt, role)
            st.session_state["live_id"] = state.request_id
            st.rerun()
    with right:
        show(label("Plan"))
        if not awaiting:
            show('<div class="nl-panel nl-note">No plan yet. Submit a request; nothing is generated until you approve its plan.</div>')
            return None
        show(plan_table(log["plan"]))
        show('<div class="nl-note" style="margin:8px 0">Nothing is generated until you approve. The approved plan is what the Gauntlet checks against.</div>')
        approve_col, reject_col, _ = st.columns([1, 1, 3])
        if approve_col.button("Approve", type="primary", key="approve", use_container_width=True):
            return True
        if reject_col.button("Reject", key="reject", use_container_width=True):
            return False
    return None


def render_timeline(run_key: str) -> int:
    """Section 3. Returns the selected round number."""
    rounds = log.get("rounds") or []
    steps = ui.timeline(log, awaiting)
    selected_key = f"selected_{run_key}"
    if rounds and st.session_state.get(selected_key) not in range(len(rounds)):
        st.session_state[selected_key] = len(rounds) - 1
    selected = st.session_state.get(selected_key, 0)

    show('<div class="nl-title">Fix loop</div>' + TOOL_GLOSS_LINE)
    strip, side = st.columns([5, 2]) if rounds else (st.container(), None)
    with strip:
        columns = st.columns(len(steps))
        for column, step in zip(columns, steps):
            with column:
                if step["kind"] == "round":
                    show(round_card(step, selected=step["round"] == selected))
                    is_selected = step["round"] == selected
                    if st.button("Showing" if is_selected else "View", key=f"view_{run_key}_{step['round']}",
                                 type="primary" if is_selected else "secondary", use_container_width=True):
                        st.session_state[selected_key] = step["round"]
                        st.rerun()
                elif step["kind"] == "stage":
                    show(stage_card(step))
                else:
                    show(end_card(step, log))
    if side is not None:
        with side:
            show(chart(rounds))
    return selected


def render_diff(run_key: str, selected: int):
    """Section 4. Before and after for the selected round, aligned."""
    rounds = log["rounds"]
    after = rounds[selected]
    show(f'<div class="nl-title">What {esc(ui.round_label(selected)).lower()} changed</div>' if selected else
         '<div class="nl-title">First draft</div>')

    single = selected == 0
    if single and len(rounds) == 1:
        verdict = "It passed the Gauntlet first time." if log.get("status") == "passed" else "No fix round followed it."
        show(f'<div class="nl-note" style="margin-bottom:8px">This run has only one draft. {verdict} The file is shown full width.</div>')
    elif single:
        show('<div class="nl-note" style="margin-bottom:8px">This is the first draft, so there is nothing before it to compare. '
             'What it failed is listed above the file. Select a fix round to see a diff.</div>')

    compare, view = "previous round", "Full file"
    pick_file, pick_compare, pick_view = st.columns([3, 2, 2])
    if not single:
        compare = pick_compare.radio("Compare", ["previous round", "first draft"], key=f"compare_{run_key}", horizontal=True)
        view = pick_view.radio("View", ["Changes only", "Full file"], key=f"view_mode_{run_key}", horizontal=True)
    before = rounds[0] if compare == "first draft" else rounds[selected - 1] if not single else after
    changed = [] if single else ui.changed_files(before["files"], after["files"])
    names = list(after["files"])
    if not names:
        show('<div class="nl-panel nl-note">This round has no files.</div>')
        return
    shown = {(f"{name}  (changed)" if name in changed else name): name for name in names}
    filename = shown[pick_file.radio(
        "File", list(shown), key=f"file_{run_key}_{selected}_{compare}", horizontal=True,
        index=names.index(ui.default_file(names, before["violations"], changed)),
    )]

    marks = ui.gutter_marks(before["violations"], filename)
    show(failure_strip(ui.file_failures(before["violations"], filename), ui.round_label(before["round"])))
    if single:
        show(file_table(after["files"][filename], marks, f"{ui.round_label(selected)} · {filename}"))
        return
    rows = ui.diff_rows(before["files"].get(filename, ""), after["files"][filename])
    removed, added = ui.diff_stats(rows)
    if removed == added == 0:
        show(f'<div class="nl-note" style="margin-bottom:6px">{esc(filename)} did not change in this comparison.'
             + (' Switch to Full file to read it.' if view == "Changes only" else '') + '</div>')
        if view == "Changes only":
            return
    else:
        show(f'<div class="nl-note mono" style="margin-bottom:6px">{removed} line(s) removed or replaced, {added} line(s) added.</div>')
    if view == "Changes only":
        rows = ui.compact_rows(rows, context=3)
    show('<div class="nl-key"><span><i style="background:#B3263A"></i>red = removed</span>'
         '<span><i style="background:#D6D2C8"></i>light = added</span></div>')
    show(diff_table(rows, marks, f"Before · {ui.round_label(before['round'])}", f"After · {ui.round_label(selected)}"))


with tab_run:
    # In a slot, so a live run can redraw the strip and its status line as each stage finishes.
    top_slot = st.empty()
    top_slot.markdown((pipeline_strip(log, awaiting) + story_line(log, awaiting)).replace("\n", ""), unsafe_allow_html=True)
    decision = render_request_and_plan()

    if decision is not None:
        # Approve or reject. On approval the strip fills in as the steps finish.
        pipeline = get_pipeline()
        request_id = st.session_state["live_id"]
        if decision:
            show('<div class="nl-title">Fix loop</div>' + TOOL_GLOSS_LINE)
            slot = st.empty()
            live = {"run": log, "stage": "generate", "status": "Draft 1: calling the model, one call per resource"}

            def repaint(status: str = None):
                # Called after every graph node, before every model call and before every retry wait.
                if status:
                    live["status"] = status
                top_slot.markdown(
                    (pipeline_strip(live["run"], current=live["stage"]) + status_line(live["status"])).replace("\n", ""),
                    unsafe_allow_html=True)
                slot.markdown(live_strip(live["run"], live["status"]).replace("\n", ""), unsafe_allow_html=True)

            def node_finished(name, state):
                # The state handed over can be one step behind; wait until it shows this node's result.
                live["run"] = ui.wait_for_node(lambda: run_log(pipeline.state(request_id)), name, live["run"])
                live["stage"], status = ui.live_position(name, live["run"])
                repaint(status)

            repaint()
            with ui.watch_llm(pipeline.llm, repaint):
                pipeline.resume(request_id, approved=True, on_step=node_finished)
        else:
            pipeline.resume(request_id, approved=False)
        st.rerun()

    if mode == REPLAY and not log:
        show('<div class="nl-panel nl-note" style="margin-top:14px">Pick a saved run in the header. '
             + ('That file is not a run log.' if replay_choice else f'There are no run logs in {esc(RUNS_DIR)}/ yet.') + '</div>')
    elif log and not awaiting:
        run_key = f"{mode}_{log['request_id']}"
        selected_round = render_timeline(run_key)
        if log.get("rounds"):
            render_diff(run_key, selected_round)
            show(f'<div class="nl-title">Violations, {esc(ui.round_label(selected_round)).lower()}</div>')
            show(violation_cards(ui.violation_cards(log["rounds"], selected_round)))
            with st.expander("Show raw table"):
                show(violations_table(ui.violation_rows(log["rounds"], selected_round)))
        else:
            show('<div class="nl-panel nl-note" style="margin-top:14px">This run ended before any file was generated, '
                 'so there are no drafts or violations to show.</div>')
        show(summary_strip(log, log_path))


# ------------------------------------------------------------------ tab 2: compare arms

with tab_arms:
    folders = ui.benchmark_folders(RUNS_DIR)
    if not folders:
        show(f'<div class="nl-panel nl-note">No benchmark output under {esc(RUNS_DIR)}/. Run benchmarks/run_benchmark.py first.</div>')
    else:
        pick_folder, pick_request = st.columns([2, 3])
        folder = pick_folder.selectbox("Benchmark folder", folders, key="arms_folder")
        requests = ui.benchmark_requests(os.path.join(RUNS_DIR, folder))
        chosen = pick_request.selectbox("Request", list(requests), key="arms_request")
        logs = {arm: ui.load_log(path) for arm, path in requests.get(chosen, {}).items()}
        any_log = next((entry for entry in logs.values() if entry), None)
        request_id, problems = ui.arm_consistency(chosen, logs)
        if problems:
            # Never show one request's text beside another request's results.
            show('<div class="nl-fail"><div class="head">These logs do not belong to one request</div>'
                 + "".join(f"<div>{esc(problem)}</div>" for problem in problems) + '</div>')
            st.stop()
        if any_log:
            show(f'<div class="nl-line"><b>REQUEST {esc(request_id)}</b> &nbsp;{esc(any_log.get("prompt"))} &nbsp;&nbsp;<b>ROLE</b> &nbsp;{esc(any_log.get("role"))}'
                 f' &nbsp;&nbsp;<b>MODEL</b> &nbsp;{esc(any_log.get("model_id"))}</div>')
            show('<div class="nl-note" style="margin:8px 0 12px 0">Pass = 0 violations from Checkov, OPA and server dry-run on the final '
                 'files. Plan conformance is counted separately. Read-only, from the saved logs.</div>')
        names = {"A": "A · plain call", "B": "B · rules in prompt", "C": "C · full pipeline"}
        verdicts = {arm: ui.arm_verdict(logs.get(arm)) for arm in "ABC"}

        def verdict_word(verdict: dict) -> str:
            if not verdict["available"]:
                return "No saved run"
            if verdict["outcome"] not in ("scored", "no_output"):
                return "No result"
            return "Pass" if verdict["passed"] else "Fail"

        summary_rows = ""
        for arm in "ABC":
            v = verdicts[arm]
            scored = v["available"] and v["outcome"] in ("scored", "no_output")
            cells = [v["violations"], v["conformance_violations"], v["fix_rounds"],
                     f'{v["tokens"]:,}' if v.get("tokens") is not None else "-",
                     f'{v["runtime"]:.1f} s' if v.get("runtime") is not None else "-"] if scored else ["-"] * 5
            word = verdict_word(v)
            summary_rows += (f'<tr><td class="m">{esc(names[arm])}</td>'
                             f'<td class="{"st-ok" if word == "Pass" else "st-fail"}">{esc(word)}</td>'
                             + "".join(f'<td class="m">{esc(c)}</td>' for c in cells) + '</tr>')
        show('<table class="nl-table" style="margin-bottom:6px"><tr><th>Arm</th><th>Verdict</th><th>Violations</th><th>Off-plan</th>'
             f'<th>Fix rounds</th><th>Tokens</th><th>Runtime</th></tr>{summary_rows}</table>')
        show('<div class="nl-note" style="margin-bottom:14px">Off-plan: resources or values that differ from the shared reference plan. '
             'Tokens and runtime exclude the shared planning call.</div>')

        for column, arm in zip(st.columns(3), "ABC"):
            with column:
                verdict = verdicts[arm]
                word = verdict_word(verdict)
                if not verdict["available"]:
                    show(f'<div class="nl-arm">{label(names[arm])}<div class="nl-note">No saved run for this arm.</div></div>')
                    continue
                if word == "No result":
                    show(f'<div class="nl-arm">{label(names[arm])}<div class="verdict bad">No result</div><div class="nl-note nl-red">'
                         f'{esc(verdict["outcome"])}: {esc(ui.plain_reason(logs[arm].get("rejection_reason")))}</div></div>')
                    continue
                show(f'<div class="nl-arm">{label(names[arm])}'
                     f'<div class="verdict {"" if verdict["passed"] else "bad"}">{word}</div>'
                     f'<div class="mono" style="font-size:13px">{verdict["violations"]} violation(s) · '
                     f'{verdict["conformance_violations"]} off-plan · {verdict["fix_rounds"]} fix round(s)</div></div>')

                def violation_rows(items: list) -> str:
                    return "".join(
                        f'<tr><td class="m nl-red">{esc(v["rule_id"])}</td><td class="m">{esc(ui.TOOL_LABELS.get(v["tool"], v["tool"]))}</td>'
                        f'<td>{esc(v["message"])}</td></tr>' for v in items)

                found = verdict["violation_list"]
                head = '<tr><th>Rule ID</th><th>Tool</th><th>Message</th></tr>'
                if found:
                    show(f'<table class="nl-table">{head}{violation_rows(found[:5])}</table>')
                    if len(found) > 5:
                        with st.expander(f"Show all {len(found)}"):
                            show(f'<table class="nl-table">{head}{violation_rows(found)}</table>')
                else:
                    show('<div class="nl-panel nl-note">No violations from Checkov, OPA or dry-run.</div>')
                if verdict["off_plan_list"]:
                    listed = "".join(f'<tr><td class="m">{esc(v["rule_id"])}</td><td>{esc(ui.difference_only(v["message"]))}</td></tr>'
                                     for v in verdict["off_plan_list"])
                    show(f'<div class="nl-file">Off-plan ({len(verdict["off_plan_list"])})</div>'
                         f'<table class="nl-table"><tr><th>Check</th><th>Difference from the plan</th></tr>{listed}</table>'
                         '<div class="nl-note" style="margin-top:6px">Arms A and B never see the plan, so a different resource name '
                         'counts as off-plan. Off-plan does not affect pass or fail.</div>')

                with st.expander("Show final files"):
                    if not verdict["files"]:
                        show('<div class="nl-panel nl-note">The model produced no usable files.</div>')
                    else:
                        # One panel per arm, all the same height; each file is a titled block inside it.
                        tables = "".join(
                            file_table(text, {}, filename).replace('<div class="nl-code">', "", 1)[:-len("</div>")]
                            for filename, text in verdict["files"].items())
                        show(f'<div class="nl-code fixed">{tables}</div>')
