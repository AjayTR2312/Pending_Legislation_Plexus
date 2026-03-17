# PDF-to-Text Conversion Application - Deployment Guide

## Overview
A PDF-to-Text conversion application that converts legislative bill PDFs from U.S. states into clean, markup-tagged plain text files for the NetScan data ingestion system. Powered by Claude Opus 4.6 visual interpretation — each PDF page is rendered as a high-resolution image and the AI visually reads the page for superior accuracy in detecting strikethrough, underline, bold, italic, and mixed formatting.

## Features
- **Vision-First Conversion**: PDF pages rendered as 400 DPI images — Claude Opus 4.6 visually reads each page for formatting detection
- **Auto State Detection**: Filename-pattern matching (instant) with AI content analysis fallback (~5–10s)
- **Single-Pass Processing**: All pages sent in one API call for cross-page consistency (128K max output tokens)
- **State-Specific Prompt Engineering**: Dynamic prompt construction from template library + per-state rules
- **Web Interface**: Streamlit-based GUI with live progress tracking, session management, and file download
- **CLI Support**: Standalone script for command-line batch processing
- **In-Memory Processing**: PDF rendering and image encoding without temp files on disk

## Folder Structure

```
Pending_Legislation/
├── app/                              # Streamlit Web Application
│   ├── streamlit_app.py              # UI + orchestration (calls process_with_claude_opus)
│   └── session_manager.py            # Session lifecycle (UUID folders, cleanup)
│
├── Opus4.6/                          # Conversion Engine
│   ├── Claude_Opus_4.6_Test.py       # Main conversion engine (imported by Streamlit app)
│   ├── conversion_prompts.md         # Prompt template library (2 templates + components)
│   ├── netscan_state_template.json   # State-specific configuration (45+ states)
│   └── logs/                         # Timestamped execution logs
│
├── tmp/sessions/{uuid}/              # Runtime Session Folders
│   ├── input/                        # Uploaded PDF files
│   ├── output/                       # Converted .txt files
│   └── logs/                         # Per-session execution logs
│
├── requirements.txt                  # Python dependencies
├── Dockerfile                        # Container configuration
└── README.md                         # This file
```

## Supported States (28 Total)
- AR (Arkansas), CA (California), CO (Colorado), CT (Connecticut)
- DC (District of Columbia), DE (Delaware), FL (Florida), GA (Georgia)
- HI (Hawaii), IA (Iowa), ID (Idaho), IL (Illinois)
- IN (Indiana), KS (Kansas), ME (Maine), MN (Minnesota)
- NC (North Carolina), ND (North Dakota), NE (Nebraska), NY (New York)
- OH (Ohio), OK (Oklahoma), SD (South Dakota), TN (Tennessee)
- TX (Texas), VT (Vermont), WI (Wisconsin), WY (Wyoming)

## Quick Start

### Prerequisites
- Python 3.11+
- Access to Thomson Reuters Anthropic AI Platform
- Network access to `aiplatform.gcs.int.thomsonreuters.com`

### Setup Instructions

1. **Clone the repository and create virtual environment**:
   ```bash
   cd Pending_Legislation
   python -m venv .venv
   .venv\Scripts\Activate.ps1       # Windows PowerShell
   pip install -r requirements.txt
   ```

2. **Launch the Web Application**:
   ```bash
   cd app
   streamlit run streamlit_app.py
   ```

3. **Access the Application**:
   - **Web Interface**: Open your browser to `http://localhost:8501`
   - Upload a PDF, the system auto-detects the state and processes it
   - Preview and download the tagged text output

### Web Interface Features

The Streamlit web interface provides:
- **File Management**: Easy upload and organization by state
- **Real-time Processing**: Live progress tracking with visual indicators
- **File Browser**: View and download all generated files
- **Analytics Dashboard**: Processing statistics and metrics
- **Session Logs**: Real-time activity monitoring
- **Tag Visualization**: Highlighted legislative tags in verified files

### CLI Usage (Standalone)

```bash
cd Opus4.6

# Auto-detect state from filename or content
python Claude_Opus_4.6_Test.py document.pdf

# Specify state explicitly
python Claude_Opus_4.6_Test.py document.pdf FL

# Specify state and output path
python Claude_Opus_4.6_Test.py document.pdf FL output.txt
```

## Processing Pipeline

### Production Flow (5 Steps)

```
User Uploads PDF  →  Connect to AI  →  Detect State  →  Build Prompt  →  Visual Conversion  →  Output
```

| Step | Action | Result |
|------|--------|--------|
| **1** | Authenticate with TR AI Platform | Claude Opus 4.6 client ready |
| **2** | Detect state from filename or AI content analysis | State code identified (e.g., FL) |
| **3** | Load state rules from JSON + build prompt from template library | State-specific prompt assembled (~4000–7000 chars) |
| **4** | Render all pages at 400 DPI → send images + prompt to Claude Opus 4.6 | Raw text with `[A>...<A]` and `[D>...<D]` tags |
| **5** | Post-process (strip protected clauses) → save tagged text | Conversion complete |

### Markup Tag System

| Tag | Opening | Closing | Meaning | Example |
|-----|---------|---------|---------|---------|
| **Add** | `[A>` | `<A]` | Added/new text | `[A>new language<A]` |
| **Delete** | `[D>` | `<D]` | Struck/deleted text | `[D>old provision<D]` |

**Forbidden Formats:** Markdown (`**`, `*`, `~~`, `__`) is strictly prohibited in output.

## State-Specific Configuration

All state rules are defined in `Opus4.6/netscan_state_template.json`. Each state entry specifies:

```json
{
  "state_name": "California",
  "abbreviation": "CA",
  "file_type": "PDF",
  "deletions": "Page numbers and footer",
  "no_tag_clauses": ["The people of the State of California do enact as follows:"],
  "add_markup": {
    "underline": false, "bold": false, "italic": true,
    "all_caps": false, "brackets": false,
    "custom_rules": ["State-specific add rules"]
  },
  "delete_markup": {
    "strike_out": true, "brackets": false,
    "custom_rules": ["State-specific delete rules"]
  }
}
```
