"""
validation_ui.py
================
Streamlit UI component for displaying LLM-as-Judge validation results.
Renders scores, grade badges, issue details, and visual charts.

Usage:
    from validation_ui import display_validation_results
    display_validation_results(result)  # result is a ValidationResult object
"""

import streamlit as st
import json


# ── Grade color mapping ──────────────────────────────────────────────────────
_GRADE_COLORS = {
    "A+": ("#1B5E20", "#E8F5E9", "🏆"),
    "A":  ("#2E7D32", "#E8F5E9", "✅"),
    "B+": ("#558B2F", "#F1F8E9", "👍"),
    "B":  ("#9E9D24", "#F9FBE7", "👌"),
    "C":  ("#F57F17", "#FFF8E1", "⚠️"),
    "D":  ("#E65100", "#FFF3E0", "⛔"),
    "F":  ("#B71C1C", "#FFEBEE", "❌"),
}


def _grade_badge_html(grade: str, score: float) -> str:
    """Generate HTML for a large grade badge."""
    fg, bg, icon = _GRADE_COLORS.get(grade, ("#424242", "#F5F5F5", "📊"))
    return f"""
    <div style="text-align:center; padding:20px; background:{bg};
                border-radius:16px; border:3px solid {fg}; margin:8px 0;">
        <div style="font-size:56px; line-height:1;">{icon}</div>
        <div style="font-size:48px; font-weight:800; color:{fg}; letter-spacing:2px;">{grade}</div>
        <div style="font-size:18px; color:{fg}; margin-top:4px;">{score:.1f}%</div>
    </div>
    """


def _score_bar_html(label: str, value: float, color: str) -> str:
    """Generate HTML for a single horizontal score bar."""
    bar_width = max(0, min(100, value))
    text_color = "#fff" if bar_width > 40 else "#333"
    return f"""
    <div style="margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:3px;">
            <span style="font-weight:600; font-size:14px;">{label}</span>
            <span style="font-weight:700; font-size:14px; color:{color};">{value:.1f}%</span>
        </div>
        <div style="background:#e0e0e0; border-radius:8px; height:24px; overflow:hidden;">
            <div style="width:{bar_width}%; height:100%; background:{color};
                        border-radius:8px; display:flex; align-items:center; justify-content:center;
                        font-size:12px; font-weight:600; color:{text_color}; transition:width 0.5s;">
                {value:.1f}%
            </div>
        </div>
    </div>
    """


def _issue_card_html(title: str, items: list, icon: str, color: str) -> str:
    """Generate HTML for a single issue category card."""
    count = len(items)
    if count == 0:
        return f"""
        <div style="padding:10px 14px; background:#F1F8E9; border-left:4px solid #4CAF50;
                    border-radius:6px; margin-bottom:8px; font-size:14px;">
            ✅ <strong>{title}</strong>: None found
        </div>
        """

    rows = ""
    for i, item in enumerate(items, 1):
        page = item.get("page", "?")
        text = item.get("text") or item.get("description", "")
        # Escape HTML
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        expected = item.get("expected", "")
        actual = item.get("actual", "")

        row = f"""
        <div style="padding:8px 12px; background:#fff; border-radius:4px;
                    margin-bottom:4px; border-left:3px solid {color};">
            <div style="font-size:13px;">
                <strong>{icon} {i}.</strong> &quot;{text}&quot;
                <span style="color:#757575; font-size:12px;"> — page {page}</span>
            </div>
        """
        if expected:
            expected = expected.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            row += f'<div style="font-size:12px; color:#616161; margin-left:20px;">Expected: <code>{expected}</code></div>'
        if actual:
            actual = actual.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            row += f'<div style="font-size:12px; color:#616161; margin-left:20px;">Actual: <code>{actual}</code></div>'
        row += "</div>"
        rows += row

    return f"""
    <div style="padding:12px; background:#FAFAFA; border-left:4px solid {color};
                border-radius:8px; margin-bottom:12px;">
        <div style="font-weight:700; font-size:15px; margin-bottom:8px; color:{color};">
            {icon} {title} ({count})
        </div>
        {rows}
    </div>
    """


def display_validation_results(result):
    """
    Render a beautiful validation results dashboard in Streamlit.

    Args:
        result: ValidationResult object from Validation.py
    """

    st.markdown("---")
    st.markdown("## 🔍 Validation Report — LLM-as-Judge")

    # ── Top row: Grade badge + Score bars + Key metrics ──────────────────────
    col_grade, col_scores, col_metrics = st.columns([1, 2, 1])

    with col_grade:
        st.markdown(_grade_badge_html(result.grade(), result.overall_score_pct),
                     unsafe_allow_html=True)

    with col_scores:
        st.markdown("#### 📊 Accuracy Scores")
        # Tag accuracy bar
        tag_color = "#4CAF50" if result.tag_accuracy_pct >= 90 else "#FF9800" if result.tag_accuracy_pct >= 70 else "#F44336"
        st.markdown(_score_bar_html("Tag Accuracy", result.tag_accuracy_pct, tag_color),
                     unsafe_allow_html=True)
        # Content accuracy bar
        content_color = "#4CAF50" if result.content_accuracy_pct >= 90 else "#FF9800" if result.content_accuracy_pct >= 70 else "#F44336"
        st.markdown(_score_bar_html("Content Accuracy", result.content_accuracy_pct, content_color),
                     unsafe_allow_html=True)
        # Overall score bar
        overall_color = "#4CAF50" if result.overall_score_pct >= 90 else "#FF9800" if result.overall_score_pct >= 70 else "#F44336"
        st.markdown(_score_bar_html("Overall Score", result.overall_score_pct, overall_color),
                     unsafe_allow_html=True)

    with col_metrics:
        st.markdown("#### 📋 Summary")
        st.metric("Judge Model", result.judge_model)
        st.metric("PDF Pages", result.pdf_pages)
        st.metric("Total Issues", result.total_issues(),
                   delta=f"-{result.total_issues()}" if result.total_issues() > 0 else "0",
                   delta_color="inverse")
        st.metric("Confidence", result.confidence.capitalize())

    # ── Structural stats row ─────────────────────────────────────────────────
    st.markdown("#### 🏗️ Structural Analysis")
    s = result.structural_stats
    sc1, sc2, sc3, sc4, sc5 = st.columns(5)
    sc1.metric("[A>] Tags", s.get("addition_tags", 0))
    sc2.metric("[D>] Tags", s.get("deletion_tags", 0))
    sc3.metric("Words", f"{s.get('word_count', 0):,}")
    sc4.metric("Lines", s.get("line_count", 0))

    # Structural health indicator
    problems = []
    if s.get("mismatched_additions") or s.get("mismatched_deletions"):
        problems.append("Mismatched tags")
    if s.get("nested_tags", 0) > 0:
        problems.append(f"{s['nested_tags']} nested")
    empty_count = s.get("empty_addition_tags", 0) + s.get("empty_deletion_tags", 0)
    if empty_count > 0:
        problems.append(f"{empty_count} empty")
    sc5.metric("Health", "⚠️ " + ", ".join(problems) if problems else "✅ Clean")

    # ── Issue detail cards ───────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 🔎 Issue Details")

    if result.total_issues() == 0:
        st.success("🎉 **No issues detected!** The conversion looks perfect.")
    else:
        # Issue count summary in colored pills
        issue_counts = [
            ("Missed [A>]", len(result.missed_additions), "#E53935"),
            ("Missed [D>]", len(result.missed_deletions), "#E53935"),
            ("False [A>]",  len(result.false_additions),  "#FF6F00"),
            ("False [D>]",  len(result.false_deletions),  "#FF6F00"),
            ("Dropped Text", len(result.dropped_text),    "#6A1B9A"),
            ("Misplaced",   len(result.misplaced_tags),   "#1565C0"),
            ("Split/Merge", len(result.split_merge_issues), "#00695C"),
            ("Layout",      len(result.layout_issues),    "#37474F"),
        ]

        # Convert hex color to rgba for background
        def hex_to_rgba(hex_color, alpha=0.12):
            hex_color = hex_color.lstrip('#')
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            return f"rgba({r},{g},{b},{alpha})"

        pills_html = '<div style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px;">'
        for label, count, color in issue_counts:
            bg = hex_to_rgba(color, 0.12)
            border = hex_to_rgba(color, 0.25)
            pills_html += (
                f'<div style="padding:4px 12px; border-radius:20px; font-size:13px; font-weight:600;'
                f' background:{bg}; color:{color}; border:1px solid {border};">'
                f'{label}: {count}</div>'
            )
        pills_html += '</div>'
        st.markdown(pills_html, unsafe_allow_html=True)

        # Expandable issue cards
        with st.expander("🔴 Missed Tags (formatted in PDF, but no tag in output)", expanded=True):
            st.markdown(
                _issue_card_html("Missed Additions", result.missed_additions, "✗", "#E53935")
                + _issue_card_html("Missed Deletions", result.missed_deletions, "✗", "#E53935"),
                unsafe_allow_html=True,
            )

        with st.expander("🟠 False Tags (tagged in output, but NOT formatted in PDF)", expanded=len(result.false_additions) + len(result.false_deletions) > 0):
            st.markdown(
                _issue_card_html("False Additions", result.false_additions, "+", "#FF6F00")
                + _issue_card_html("False Deletions", result.false_deletions, "+", "#FF6F00"),
                unsafe_allow_html=True,
            )

        with st.expander("🟣 Content Issues (dropped text, misplaced, split/merge)", expanded=len(result.dropped_text) + len(result.misplaced_tags) + len(result.split_merge_issues) > 0):
            st.markdown(
                _issue_card_html("Dropped Text", result.dropped_text, "⊘", "#6A1B9A")
                + _issue_card_html("Misplaced Tags", result.misplaced_tags, "↔", "#1565C0")
                + _issue_card_html("Split/Merge Issues", result.split_merge_issues, "↕", "#00695C"),
                unsafe_allow_html=True,
            )

        with st.expander("⚪ Layout Issues", expanded=len(result.layout_issues) > 0):
            st.markdown(
                _issue_card_html("Layout Issues", result.layout_issues, "⇄", "#37474F"),
                unsafe_allow_html=True,
            )

    # ── Judge reasoning ──────────────────────────────────────────────────────
    if result.reasoning:
        with st.expander("🧠 Judge Reasoning (detailed analysis)", expanded=False):
            st.markdown(
                f'<div style="font-family:Georgia,serif; font-size:14px; line-height:1.7; '
                f'background:#FAFAFA; padding:16px; border-radius:8px; border-left:4px solid #1976D2;">'
                f'{result.reasoning.replace(chr(10), "<br>")}</div>',
                unsafe_allow_html=True,
            )

    # ── Golden comparison (if available) ─────────────────────────────────────
    if result.golden_f1 is not None:
        st.markdown("---")
        st.markdown("#### 📏 Golden Reference Comparison")
        g1, g2, g3 = st.columns(3)
        g1.metric("Precision", f"{result.golden_precision:.1%}")
        g2.metric("Recall", f"{result.golden_recall:.1%}")
        g3.metric("F1 Score", f"{result.golden_f1:.1%}")

    # ── Download validation report ───────────────────────────────────────────
    st.markdown("---")
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        report_text = result.detailed_report()
        st.download_button(
            label="📄 Download Validation Report (.txt)",
            data=report_text,
            file_name="validation_report.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with col_dl2:
        json_data = {
            "state_code": result.state_code,
            "judge_model": result.judge_model,
            "pdf_pages": result.pdf_pages,
            "confidence": result.confidence,
            "tag_accuracy_pct": result.tag_accuracy_pct,
            "content_accuracy_pct": result.content_accuracy_pct,
            "overall_score_pct": result.overall_score_pct,
            "grade": result.grade(),
            "total_issues": result.total_issues(),
            "structural_stats": result.structural_stats,
            "missed_additions": result.missed_additions,
            "missed_deletions": result.missed_deletions,
            "false_additions": result.false_additions,
            "false_deletions": result.false_deletions,
            "dropped_text": result.dropped_text,
            "misplaced_tags": result.misplaced_tags,
            "split_merge_issues": result.split_merge_issues,
            "layout_issues": result.layout_issues,
            "reasoning": result.reasoning,
        }
        st.download_button(
            label="📊 Download Validation Data (.json)",
            data=json.dumps(json_data, indent=2, ensure_ascii=False),
            file_name="validation_results.json",
            mime="application/json",
            use_container_width=True,
        )
