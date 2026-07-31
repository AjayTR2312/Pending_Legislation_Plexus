"""
Summary Tab UI Demo — standalone preview of the new Summary layout.

Run:  streamlit run summary_ui_demo.py
"""

import streamlit as st
import pandas as pd

st.set_page_config(page_title="Summary Tab UI Demo", layout="wide")

# ── Sidebar: toggle scenarios ─────────────────────────────────────────────
st.sidebar.title("🎛️ Scenario Picker")
scenario = st.sidebar.radio(
    "Choose a scenario to preview:",
    [
        "✅ All perfect (100% / 100%)",
        "⚠️ Tag issues only",
        "⚠️ Content issues only",
        "❌ Both tag + content issues",
        "🟢 No tagging required",
    ],
)

# ── Build mock data based on scenario ─────────────────────────────────────

if scenario == "✅ All perfect (100% / 100%)":
    tag_total, tag_pass, tag_err, tag_warn = 164, 164, 0, 0
    content_pct = 99.87
    content_words, content_max = 2345, 2348
    content_issues_list = []
    tag_issues_list = []

elif scenario == "⚠️ Tag issues only":
    tag_total, tag_pass, tag_err, tag_warn = 164, 160, 3, 1
    content_pct = 99.95
    content_words, content_max = 2345, 2346
    content_issues_list = []
    tag_issues_list = [
        {"Status": "❌ Error", "Page": "3", "Line": "47",
         "Text": "(2) Provide transportation, if necessary...",
         "Issue": "Tag dropped: bold text not wrapped in [A>...<A]",
         "Suggested Fix": "Add [A>...<A] around the bold text"},
        {"Status": "❌ Error", "Page": "3", "Line": "37",
         "Text": "ENGROSSED HOUSE BILL No. 1018",
         "Issue": "Extra tag: title text incorrectly tagged",
         "Suggested Fix": "Remove [A>...<A] — this is a document title"},
        {"Status": "❌ Error", "Page": "2", "Line": "24",
         "Text": "this style type",
         "Issue": "Italic not tagged in PRINTING CODE section",
         "Suggested Fix": "Add [A>...<A] around italic text in PRINTING CODE"},
        {"Status": "⚠️ Warning", "Page": "4", "Line": "68",
         "Text": "Section 17 of this chapter",
         "Issue": "Partial match: text slightly differs from PDF source",
         "Suggested Fix": "Verify against highlighted PDF image"},
    ]

elif scenario == "⚠️ Content issues only":
    tag_total, tag_pass, tag_err, tag_warn = 164, 164, 0, 0
    content_pct = 97.52
    content_words, content_max = 2280, 2340
    content_issues_list = [
        {"Line": "43", "Type": "Delete", "Expected (PDF)": "pursuant to IC 12-17-12-11",
         "Output": "—", "Category": "Missing Words"},
        {"Line": "51", "Type": "Replace", "Expected (PDF)": "school age child care",
         "Output": "school-age childcare", "Category": "Word Change"},
        {"Line": "67", "Type": "Insert", "Expected (PDF)": "—",
         "Output": "the following:", "Category": "Extra Words"},
    ]
    tag_issues_list = []

elif scenario == "❌ Both tag + content issues":
    tag_total, tag_pass, tag_err, tag_warn = 164, 158, 4, 2
    content_pct = 96.30
    content_words, content_max = 2250, 2340
    content_issues_list = [
        {"Line": "43", "Type": "Delete", "Expected (PDF)": "pursuant to IC 12-17-12-11",
         "Output": "—", "Category": "Missing Words"},
        {"Line": "58", "Type": "Replace", "Expected (PDF)": "General Assembly",
         "Output": "general assembly", "Category": "Case Change"},
    ]
    tag_issues_list = [
        {"Status": "❌ Error", "Page": "3", "Line": "47",
         "Text": "(2) Provide transportation...",
         "Issue": "Tag dropped: strikethrough text not wrapped in [D>...<D]",
         "Suggested Fix": "Add [D>...<D] around the struck-through text"},
        {"Status": "❌ Error", "Page": "3", "Line": "37",
         "Text": "ENGROSSED HOUSE BILL No. 1018",
         "Issue": "Extra tag: title text incorrectly tagged as addition",
         "Suggested Fix": "Remove [A>...<A] — this is a document title, not added text"},
        {"Status": "❌ Error", "Page": "2", "Line": "24",
         "Text": "this style type",
         "Issue": "Italic not tagged in PRINTING CODE section",
         "Suggested Fix": "Wrap with [A>...<A] per italic_section_only rule"},
        {"Status": "❌ Error", "Page": "3", "Line": "50",
         "Text": "(3) Provide efficient and effective...",
         "Issue": "Wrong tag type: should be [D>...<D] but found [A>...<A]",
         "Suggested Fix": "Change tag from [A>...<A] to [D>...<D]"},
        {"Status": "⚠️ Warning", "Page": "4", "Line": "65",
         "Text": "cost and expense standards",
         "Issue": "Partial match: minor text difference from PDF source",
         "Suggested Fix": "Check against highlighted PDF page"},
        {"Status": "⚠️ Warning", "Page": "3", "Line": "43",
         "Text": "NEW",
         "Issue": "Partial match: word 'NEW' may need bold tagging",
         "Suggested Fix": "Verify bold formatting in source PDF"},
    ]

else:  # No tagging required
    tag_total, tag_pass, tag_err, tag_warn = 0, 0, 0, 0
    content_pct = 100.0
    content_words, content_max = 1200, 1200
    content_issues_list = []
    tag_issues_list = []


# ── Render the Summary tab ────────────────────────────────────────────────

st.title("📊 Summary Tab — UI Preview")
st.caption("This is a standalone demo of how the Summary tab looks. Pick scenarios from the sidebar.")

# ── Chips ─────────────────────────────────────────────────────────────────

_chip = (
    'display:inline-flex; align-items:center; gap:6px; '
    'padding:8px 16px; border-radius:10px; font-size:0.85rem; font-weight:600;'
)

# Conversion chip
_conv_chip = (
    f'<div style="{_chip} background:#f0fdf4; color:#166534; border:1px solid #bbf7d0;">'
    f'<span style="font-size:1rem;">📝</span> Conversion '
    f'<span style="font-weight:800;">12.3s</span></div>'
)

# Validation chip
_val_chip = (
    f'<div style="{_chip} background:#eff6ff; color:#1e40af; border:1px solid #bfdbfe;">'
    f'<span style="font-size:1rem;">🔍</span> Validation '
    f'<span style="font-weight:800;">8.1s</span></div>'
)

# Size chip
_size_chip = (
    f'<div style="{_chip} background:#fefce8; color:#854d0e; border:1px solid #fde68a;">'
    f'<span style="font-size:1rem;">📦</span> File Size '
    f'<span style="font-weight:800;">112 KB</span></div>'
)

# Content accuracy chip
if content_pct >= 99:
    _ca_bg, _ca_fg, _ca_bdr = '#f0fdf4', '#166534', '#bbf7d0'
elif content_pct >= 95:
    _ca_bg, _ca_fg, _ca_bdr = '#fefce8', '#854d0e', '#fde68a'
else:
    _ca_bg, _ca_fg, _ca_bdr = '#fef2f2', '#991b1b', '#fecaca'

_ca_detail = f" ({content_words:,}/{content_max:,} words"
if content_issues_list:
    _ca_detail += f", {len(content_issues_list)} issue{'s' if len(content_issues_list) != 1 else ''}"
_ca_detail += ")"

_content_acc_chip = (
    f'<div style="{_chip} background:{_ca_bg}; color:{_ca_fg}; border:1px solid {_ca_bdr};">'
    f'<span style="font-size:1rem;">📊</span> Content Accuracy '
    f'<span style="font-weight:800;">{content_pct:.2f}%</span>'
    f'<span style="font-size:0.75rem; font-weight:400; margin-left:4px;">{_ca_detail}</span></div>'
)

# Tag accuracy chip
if tag_total == 0:
    _ta_pct = 100.0
    _ta_bg, _ta_fg, _ta_bdr = '#f0fdf4', '#166534', '#bbf7d0'
    _ta_detail = " (No formatting modifications detected — no tagging required)"
else:
    _ta_pct = (tag_pass / tag_total * 100.0)
    if _ta_pct >= 99:
        _ta_bg, _ta_fg, _ta_bdr = '#f0fdf4', '#166534', '#bbf7d0'
    elif _ta_pct >= 95:
        _ta_bg, _ta_fg, _ta_bdr = '#fefce8', '#854d0e', '#fde68a'
    else:
        _ta_bg, _ta_fg, _ta_bdr = '#fef2f2', '#991b1b', '#fecaca'
    _ta_detail = f" ({tag_pass}/{tag_total}"
    if tag_err:
        _ta_detail += f", {tag_err} error{'s' if tag_err != 1 else ''}"
    if tag_warn:
        _ta_detail += f", {tag_warn} warning{'s' if tag_warn != 1 else ''}"
    _ta_detail += ")"

_tag_acc_chip = (
    f'<div style="{_chip} background:{_ta_bg}; color:{_ta_fg}; border:1px solid {_ta_bdr};">'
    f'<span style="font-size:1rem;">🏷️</span> Tag Accuracy '
    f'<span style="font-weight:800;">{_ta_pct:.1f}%</span>'
    f'<span style="font-size:0.75rem; font-weight:400; margin-left:4px;">{_ta_detail}</span></div>'
)

# State pill
_state_pill = (
    '<span style="background:#e0e7ff; color:#3730a3; padding:4px 12px; '
    'border-radius:20px; font-size:0.78rem; font-weight:600;">🏛️ IN — Indiana</span>'
)

# ── Summary card ──────────────────────────────────────────────────────────
_summary_html = (
    '<div style="background:#ffffff; border-radius:14px; padding:24px 28px; margin:8px 0 20px;'
    ' border:1px solid #e2e8f0; box-shadow:0 2px 12px rgba(0,0,0,.05);">'
    '<div style="display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap;">'
    '<span style="font-size:1.5rem;">✅</span>'
    '<span style="font-size:1.2rem; font-weight:700; color:#15803d;">Processing Complete</span>'
    f'{_state_pill}'
    '</div>'
    '<div style="display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px;">'
    f'{_conv_chip}{_val_chip}{_size_chip}{_content_acc_chip}{_tag_acc_chip}'
    '</div>'
    '<div style="background:#f8fafc; border-radius:8px; padding:8px 16px;'
    ' display:flex; align-items:center; justify-content:space-between;'
    ' border:1px solid #e2e8f0;">'
    '<span style="font-size:0.84rem; color:#475569;">⏱️ Total processing time</span>'
    '<span style="font-size:1rem; font-weight:800; color:#0f172a;">20.4s</span>'
    '</div>'
    '<p style="margin:12px 0 0; font-size:0.82rem; color:#64748b;">'
    'Your PDF has been converted to tagged legislative text with Claude Opus 4.6. '
    'Review the chips above for accuracy metrics.'
    '</p></div>'
)

st.markdown(_summary_html, unsafe_allow_html=True)

# ── Tag Issues expander ───────────────────────────────────────────────────
if tag_err > 0 or tag_warn > 0:
    _issue_label = []
    if tag_err:
        _issue_label.append(f"{tag_err} error{'s' if tag_err != 1 else ''}")
    if tag_warn:
        _issue_label.append(f"{tag_warn} warning{'s' if tag_warn != 1 else ''}")
    with st.expander(f"🏷️ Tag Issues ({', '.join(_issue_label)})", expanded=False):
        if tag_issues_list:
            st.dataframe(
                pd.DataFrame(tag_issues_list),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No tag issue details available.")

# ── Content Issues expander ───────────────────────────────────────────────
if content_issues_list:
    with st.expander(f"📊 Content Issues ({len(content_issues_list)} difference{'s' if len(content_issues_list) != 1 else ''})", expanded=False):
        st.dataframe(
            pd.DataFrame(content_issues_list),
            use_container_width=True,
            hide_index=True,
        )

# ── Submit button ─────────────────────────────────────────────────────────
st.markdown("---")
_c1, _c2 = st.columns([3, 1])
with _c1:
    st.markdown(
        '<p style="font-size:0.88rem; color:#475569; margin:4px 0;">'
        '📤 Submit approved file to <code>/ftp/keyed</code> for downstream processing.</p>',
        unsafe_allow_html=True,
    )
with _c2:
    st.button("🚀 Submit to Keyed", type="primary", use_container_width=True)
