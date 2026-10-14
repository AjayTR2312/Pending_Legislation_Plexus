import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  applyFix,
  approveReview,
  comparePageUrl,
  fetchText,
  getCachedTagAudit,
  getApproval,
  getCompare,
  getLlmConfig,
  getLocations,
  getSubmitConfig,
  getValidation,
  resetEdits,
  runTagAudit,
  saveText,
  submitToKeyed,
  undoFix,
  workingTextUrl,
} from "../api";
import { isError, mergeUp, splitAt, toText, untag, wrapTag } from "../textEdit";
import type { EditOutcome } from "../textEdit";
import ActivityLog from "./ActivityLog";
import JobMetrics from "./JobMetrics";
import ScoreCard from "./ScoreCard";
import Icon from "./Icon";
import FindingCompare from "./FindingCompare";
import type {
  ApprovalStatus,
  CompareBox,
  CompareData,
  Job,
  LlmConfig,
  Marker,
  ReviewTarget,
  RuleCatalog,
  SubmitConfig,
  TagAuditResult,
  ValidationReport,
} from "../types";

interface Props {
  target: ReviewTarget;
  job?: Job;
  onBack: () => void;
  /** Extra content for the Summary tab (e.g. artifact downloads). */
  extra?: ReactNode;
}

type View = "compare" | "summary" | "log";
type SourceFilter = "all" | "content" | "tags" | "paragraph" | "rules" | "layout" | "revisions";

const DELIM_SPLIT = /(\[A>|<A\]|\[D>|<D\])/;
const SEV_RANK: Record<string, number> = { error: 0, warning: 1, info: 2 };
const ECHO_MS = 80; // ignore the scroll event our own programmatic scroll causes
const JUMP_MS = 750; // pause scroll-sync while a click-jump animates...
const SETTLE_MS = 180; // ...and keep pausing until the animation has been quiet this long (long jumps take > JUMP_MS)

function sevOf(a: Marker, b: Marker | undefined): Marker {
  if (!b) return a;
  return (SEV_RANK[a.severity] ?? 3) < (SEV_RANK[b.severity] ?? 3) ? a : b;
}

function lsNum(key: string, fallback: number): number {
  try {
    const v = Number(localStorage.getItem(key));
    return Number.isFinite(v) && v > 0 ? v : fallback;
  } catch {
    return fallback;
  }
}

function lsSetNum(key: string, v: number) {
  try {
    localStorage.setItem(key, String(v));
  } catch {
    /* private window: the layout just is not remembered */
  }
}

const DEFAULT_FINDINGS_PX = 340;
const DEFAULT_PDF_FR = 1.3;
const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

function lsGet(key: string): string[] {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "[]") as string[];
  } catch {
    return [];
  }
}

function lsSet(key: string, value: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable — reviewed state just won't persist */
  }
}

/** Scroll `container` so `el` sits `ratio` of the way down its viewport. */
function scrollElIntoPane(container: HTMLElement | null, el: HTMLElement | null, ratio = 0.3) {
  if (!container || !el) return;
  container.scrollTo({ top: Math.max(el.offsetTop - container.clientHeight * ratio, 0), behavior: "smooth" });
}

/** Where a DOM point sits in the converted text: which line, and the character offset in that line. */
function pointInLine(node: Node, off: number): { li: number; off: number } | null {
  const el = node.nodeType === 1 ? (node as Element) : node.parentElement;
  const lineEl = el?.closest(".text-line") as HTMLElement | null;
  const body = lineEl?.querySelector(".line-body");
  if (!lineEl || !body || lineEl.dataset.li === undefined) return null;
  const r = document.createRange();
  r.setStart(body, 0);
  try {
    r.setEnd(node, off);
  } catch {
    return null;
  }
  const frag = r.cloneContents();
  frag.querySelectorAll(".insert-flag").forEach((n) => n.remove()); // the ⚑ glyphs are not text
  return { li: Number(lineEl.dataset.li), off: (frag.textContent ?? "").length };
}

export default function ReviewWorkspace({
  target,
  job,
  onBack,
  extra,
}: Props) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [raw, setRaw] = useState("");
  const [compare, setCompare] = useState<CompareData | null>(null);
  const [markers, setMarkers] = useState<Marker[]>([]);
  const [catalog, setCatalog] = useState<RuleCatalog | null>(null);
  const [loadMsg, setLoadMsg] = useState("Running validation…");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("compare");
  const [audit, setAudit] = useState<TagAuditResult | null>(null);
  const [auditRunning, setAuditRunning] = useState(false);
  const [approval, setApproval] = useState<ApprovalStatus | null>(null);
  const [submitCfg, setSubmitCfg] = useState<SubmitConfig | null>(null);
  const [llmCfg, setLlmCfg] = useState<LlmConfig | null>(null);
  const [showPdf, setShowPdf] = useState(true);
  const [showText, setShowText] = useState(true);

  const [activeId, setActiveId] = useState<string | null>(null);
  const [selKeys, setSelKeys] = useState<Set<string>>(new Set());
  const [syncOn, setSyncOn] = useState(true);
  const [zoom, setZoom] = useState(100);
  // Pane layout: drag the dividers; it is remembered between visits.
  const [findingsPx, setFindingsPx] = useState(() => lsNum("pl.findingsPx", DEFAULT_FINDINGS_PX));
  const [pdfFr, setPdfFr] = useState(() => lsNum("pl.pdfFr", DEFAULT_PDF_FR));
  const [showLn, setShowLn] = useState(() => lsNum("pl.showLn", 1) === 1);
  // Full page: hides the app header and navigation so the panes get the whole window.
  const [fullPage, setFullPage] = useState(() => lsNum("pl.fullPage", 2) === 1);
  useEffect(() => lsSetNum("pl.fullPage", fullPage ? 1 : 2), [fullPage]);
  useEffect(() => {
    document.body.classList.toggle("focus-review", fullPage);
    return () => document.body.classList.remove("focus-review");
  }, [fullPage]);
  useEffect(() => lsSetNum("pl.findingsPx", findingsPx), [findingsPx]);
  useEffect(() => lsSetNum("pl.pdfFr", pdfFr), [pdfFr]);
  useEffect(() => lsSetNum("pl.showLn", showLn ? 1 : 2), [showLn]);
  const [panelOpen, setPanelOpen] = useState(true);
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [sevFilter, setSevFilter] = useState<"all" | "error" | "warning">("all");
  const [hideReviewed, setHideReviewed] = useState(true);
  const [reviewed, setReviewed] = useState<Set<string>>(new Set());
  const [expandedParents, setExpandedParents] = useState<Set<string>>(new Set());
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ line: number; value: string; caret?: number } | null>(null);
  const [band, setBand] = useState<{ page: number; x0: number; y0: number; x1: number; y1: number } | null>(null);

  const pdfRef = useRef<HTMLDivElement | null>(null);
  const textRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null); // the open line editor, if any
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const lineRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const wordRefs = useRef<Record<string, HTMLSpanElement | null>>({});
  // Last selection / caret inside the text pane, as (line, character offset) — used by the edit tools.
  const selRef = useRef<{ li: number; start: number; end: number; endLi: number } | null>(null);
  const lockUntil = useRef(0);
  const echoUntil = useRef({ pdf: 0, text: 0 });
  const dragRef = useRef<{ page: number; x0: number; y0: number; moved: boolean; key?: string } | null>(null);

  const reviewedKey = report ? `reviewed:${report.key}` : null;

  // ── Loading ───────────────────────────────────────────────────────────────
  const load = useCallback(async () => {
    setError(null);
    try {
      setLoadMsg("Running validation…");
      const r = await getValidation(target.textPath, target.pdfPath, target.stateCode, target.jobId);
      setReport(r);
      setApproval(await getApproval(r.key));
      setRaw(await fetchText(workingTextUrl(r.key)));
      if (target.pdfPath) {
        setLoadMsg("Aligning PDF and text…");
        setCompare(await getCompare(target.textPath, target.pdfPath, r.key));
        setLoadMsg("Locating findings…");
        const located = await getLocations(r.key, target.textPath, target.pdfPath);
        setMarkers(located.markers);
        setCatalog(located.catalog);
        setAudit(await getCachedTagAudit(target.textPath, target.pdfPath));
      }
      setLoadMsg("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setLoadMsg("");
    }
  }, [target.textPath, target.pdfPath, target.stateCode]);

  useEffect(() => {
    load();
  }, [load]);

  // Messages disappear by themselves.
  useEffect(() => {
    if (!error && !notice) return;
    const id = setTimeout(() => { setError(null); setNotice(null); }, 7000);
    return () => clearTimeout(id);
  }, [error, notice]);

  useEffect(() => {
    if (reviewedKey) setReviewed(new Set(lsGet(reviewedKey)));
  }, [reviewedKey]);

  useEffect(() => {
    getSubmitConfig().then(setSubmitCfg).catch(() => setSubmitCfg(null));
    getLlmConfig().then(setLlmCfg).catch(() => setLlmCfg(null));
  }, []);

  // Remember where the reviewer's caret/selection is, so the toolbar buttons can act on it.
  useEffect(() => {
    function onSel() {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0 || !textRef.current) return;
      const range = sel.getRangeAt(0);
      if (!textRef.current.contains(range.startContainer)) return;
      const a = pointInLine(range.startContainer, range.startOffset);
      const b = pointInLine(range.endContainer, range.endOffset);
      if (a && b) selRef.current = { li: a.li, start: a.off, end: b.off, endLi: b.li };
    }
    document.addEventListener("selectionchange", onSel);
    return () => document.removeEventListener("selectionchange", onSel);
  }, []);


  const lines = useMemo(() => compare?.lines ?? raw.split(/\r?\n/), [compare, raw]);
  const cmap = compare?.map;

  // ── Derived geometry for sync ─────────────────────────────────────────────
  const { lineAnchor, pdfAnchors } = useMemo(() => {
    const anchors: ({ pos: number } | null)[] = new Array(lines.length).fill(null);
    const real: { pos: number; line: number }[] = [];
    if (cmap) {
      for (let li = 0; li < lines.length; li++) {
        const wc = lines[li].split(" ").length;
        for (let wi = 0; wi < wc; wi++) {
          const b = cmap[`${li}_${wi}`];
          if (b) {
            anchors[li] = { pos: b.page + b.y };
            real.push({ pos: b.page + b.y, line: li });
            break;
          }
        }
      }
    }
    // Carry the previous anchor over unlinked lines (headers, page numbers).
    let last = 0;
    const filled = anchors.map((a) => {
      if (a) last = a.pos;
      return { pos: a ? a.pos : last };
    });
    real.sort((a, b) => a.pos - b.pos || a.line - b.line);
    return { lineAnchor: filled, pdfAnchors: real };
  }, [lines, cmap]);

  const byPage = useMemo(() => {
    const out: Record<number, { key: string; box: CompareBox }[]> = {};
    if (!cmap) return out;
    for (const [key, box] of Object.entries(cmap)) (out[box.page] ??= []).push({ key, box });
    return out;
  }, [cmap]);

  // Per-word tag membership so tagged regions read as coloured spans.
  const wordTag = useMemo(() => {
    const out = new Map<string, "A" | "D">();
    let state: "A" | "D" | null = null;
    lines.forEach((line, li) => {
      line.split(" ").forEach((w, wi) => {
        if (w.includes("[A>")) state = "A";
        else if (w.includes("[D>")) state = "D";
        if (state) out.set(`${li}_${wi}`, state);
        if ((state === "A" && w.includes("<A]")) || (state === "D" && w.includes("<D]"))) state = null;
      });
    });
    return out;
  }, [lines]);

  // ── Marker lookup tables ──────────────────────────────────────────────────
  const markerById = useMemo(() => new Map(markers.map((m) => [m.id, m])), [markers]);

  const { wordMarks, gutterMarks, insertMarks } = useMemo(() => {
    const wm = new Map<string, Marker>();
    const gm = new Map<number, Marker>();
    const im = new Map<string, Marker[]>();
    for (const m of markers) {
      if (m.source === "revisions") continue; // already coloured by tag type
      const endLine = m.end_line ?? m.line;
      for (let li = m.line; li <= Math.min(endLine, m.line + 400); li++) gm.set(li, sevOf(m, gm.get(li)));
      if (m.insertion && m.w0 !== null) {
        const k = `${m.line}_${m.w0}`;
        im.set(k, [...(im.get(k) ?? []), m]);
      } else if (m.w0 !== null) {
        let n = 0;
        for (let li = m.line; li <= endLine && n < 3000; li++) {
          const words = (lines[li] ?? "").split(" ").length;
          const from = li === m.line ? m.w0 : 0;
          const to = li === endLine ? (m.w1 ?? words - 1) : words - 1;
          for (let wi = from; wi <= to; wi++, n++) wm.set(`${li}_${wi}`, sevOf(m, wm.get(`${li}_${wi}`)));
        }
      }
    }
    return { wordMarks: wm, gutterMarks: gm, insertMarks: im };
  }, [markers, lines]);

  const active = activeId ? markerById.get(activeId) ?? null : null;

  const activeKeys = useMemo(() => {
    const s = new Set<string>();
    if (!active || active.w0 === null || active.insertion) return s;
    const endLine = active.end_line ?? active.line;
    for (let li = active.line; li <= endLine; li++) {
      const words = (lines[li] ?? "").split(" ").length;
      const from = li === active.line ? active.w0 : 0;
      const to = li === endLine ? (active.w1 ?? words - 1) : words - 1;
      for (let wi = from; wi <= to; wi++) s.add(`${li}_${wi}`);
    }
    return s;
  }, [active, lines]);

  // ── Issue list ────────────────────────────────────────────────────────────
  const listed = useMemo(() => {
    return markers.filter((m) => {
      if (m.parent) return false;
      if (sourceFilter === "all" ? m.source === "revisions" : m.source !== sourceFilter) return false;
      if (sevFilter !== "all" && m.severity !== sevFilter) return false;
      if (hideReviewed && reviewed.has(m.id)) return false;
      return true;
    });
  }, [markers, sourceFilter, sevFilter, hideReviewed, reviewed]);

  const childrenOf = useMemo(() => {
    const out = new Map<string, Marker[]>();
    for (const m of markers) if (m.parent) out.set(m.parent, [...(out.get(m.parent) ?? []), m]);
    return out;
  }, [markers]);

  const counts = useMemo(() => {
    const top = markers.filter((m) => !m.parent && m.source !== "revisions");
    const revs = markers.filter((m) => m.source === "revisions");
    return {
      revisions: revs.length,
      revisionsDone: revs.filter((m) => reviewed.has(m.id)).length,
      total: top.length,
      done: top.filter((m) => reviewed.has(m.id)).length,
      error: top.filter((m) => m.severity === "error" && !reviewed.has(m.id)).length,
      warning: top.filter((m) => m.severity === "warning" && !reviewed.has(m.id)).length,
      content: top.filter((m) => m.source === "content").length,
      tags: top.filter((m) => m.source === "tags").length,
      paragraph: top.filter((m) => m.source === "paragraph").length,
      rules: top.filter((m) => m.source === "rules").length,
      layout: top.filter((m) => m.source === "layout").length,
    };
  }, [markers, reviewed]);

  // ── Scroll helpers ────────────────────────────────────────────────────────
  const pdfPx = useCallback((pos: number): number | null => {
    const page = Math.min(Math.max(Math.floor(pos), 0), (compare?.pages.length ?? 1) - 1);
    const el = pageRefs.current[page];
    if (!el) return null;
    return el.offsetTop + (pos - page) * el.offsetHeight;
  }, [compare]);

  const posAtPdfPx = useCallback((px: number): number => {
    const n = compare?.pages.length ?? 0;
    for (let i = n - 1; i >= 0; i--) {
      const el = pageRefs.current[i];
      if (el && el.offsetTop <= px) return i + Math.min((px - el.offsetTop) / Math.max(el.offsetHeight, 1), 0.999);
    }
    return 0;
  }, [compare]);

  const holdSync = () => {
    lockUntil.current = performance.now() + JUMP_MS;
  };

  const jumpPdf = useCallback((pos: number, ratio = 0.3) => {
    const px = pdfPx(pos);
    if (px === null || !pdfRef.current) return;
    holdSync();
    pdfRef.current.scrollTo({ top: Math.max(px - pdfRef.current.clientHeight * ratio, 0), behavior: "smooth" });
  }, [pdfPx]);

  const jumpText = useCallback((line: number, ratio = 0.3) => {
    holdSync();
    scrollElIntoPane(textRef.current, lineRefs.current[line] ?? null, ratio);
  }, []);

  /** Line whose top is at or above `px` in the text pane, plus the fraction through it. */
  const lineAtTextPx = useCallback((px: number): number => {
    let lo = 0;
    let hi = lines.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      const el = lineRefs.current[mid];
      if (el && el.offsetTop <= px) lo = mid;
      else hi = mid - 1;
    }
    const el = lineRefs.current[lo];
    return lo + (el ? Math.min(Math.max((px - el.offsetTop) / Math.max(el.offsetHeight, 1), 0), 0.999) : 0);
  }, [lines.length]);

  const onTextScroll = useCallback(() => {
    if (!syncOn || !compare) return;
    const now = performance.now();
    if (now < lockUntil.current) {
      lockUntil.current = now + SETTLE_MS; // still animating from a jump: do not let sync cancel it
      return;
    }
    if (now < echoUntil.current.text) return;
    const text = textRef.current;
    const pdf = pdfRef.current;
    if (!text || !pdf) return;
    const f = lineAtTextPx(text.scrollTop);
    const li = Math.floor(f);
    const a = lineAnchor[li]?.pos ?? 0;
    const b = lineAnchor[Math.min(li + 1, lineAnchor.length - 1)]?.pos ?? a;
    const px = pdfPx(a + (b - a) * (f - li));
    if (px === null) return;
    echoUntil.current.pdf = now + ECHO_MS;
    pdf.scrollTop = px;
  }, [syncOn, compare, lineAtTextPx, lineAnchor, pdfPx]);

  const onPdfScroll = useCallback(() => {
    if (!syncOn || !compare || pdfAnchors.length === 0) return;
    const now = performance.now();
    if (now < lockUntil.current) {
      lockUntil.current = now + SETTLE_MS;
      return;
    }
    if (now < echoUntil.current.pdf) return;
    const text = textRef.current;
    const pdf = pdfRef.current;
    if (!text || !pdf) return;
    const pos = posAtPdfPx(pdf.scrollTop);
    let lo = 0;
    let hi = pdfAnchors.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (pdfAnchors[mid].pos <= pos) lo = mid;
      else hi = mid - 1;
    }
    const a = pdfAnchors[lo];
    const n = pdfAnchors[Math.min(lo + 1, pdfAnchors.length - 1)];
    const frac = n.pos > a.pos ? Math.min(Math.max((pos - a.pos) / (n.pos - a.pos), 0), 1) : 0;
    const lineF = a.line + (n.line - a.line) * frac;
    const li = Math.floor(lineF);
    const el = lineRefs.current[li];
    if (!el) return;
    echoUntil.current.text = now + ECHO_MS;
    text.scrollTop = el.offsetTop + (lineF - li) * el.offsetHeight;
  }, [syncOn, compare, pdfAnchors, posAtPdfPx]);

  // ── Selection / focus in either direction ─────────────────────────────────
  const focusKeys = useCallback((keys: string[], from: "text" | "pdf") => {
    if (keys.length === 0) return;
    const sorted = [...keys].sort((a, b) => {
      const [al, aw] = a.split("_").map(Number);
      const [bl, bw] = b.split("_").map(Number);
      return al - bl || aw - bw;
    });
    setSelKeys(new Set(sorted));
    setActiveId(null);
    const first = sorted[0];
    if (from === "text") {
      const box = cmap?.[first];
      if (box) jumpPdf(box.page + box.y);
    } else {
      holdSync();
      scrollElIntoPane(textRef.current, wordRefs.current[first]?.closest(".text-line") as HTMLElement | null);
    }
  }, [cmap, jumpPdf]);

  const onTextMouseUp = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !textRef.current) return;
    const keys: string[] = [];
    for (const [k, el] of Object.entries(wordRefs.current)) {
      if (el && sel.containsNode(el, true) && cmap?.[k]) keys.push(k);
    }
    if (keys.length) focusKeys(keys, "text");
  }, [cmap, focusKeys]);

  const selectMarker = useCallback((m: Marker) => {
    setActiveId(m.id);
    setSelKeys(new Set());
    setDraft(m.expected || m.output || "");
    setEditing(null);
    setNotice(null);
    jumpText(m.line);
    if (m.boxes.length) jumpPdf(m.boxes[0].page + m.boxes[0].y);
    else if (lineAnchor[m.line]) jumpPdf(lineAnchor[m.line].pos);
  }, [jumpText, jumpPdf, lineAnchor]);

  const step = useCallback((delta: number) => {
    if (listed.length === 0) return;
    const idx = active ? listed.findIndex((m) => m.id === active.id) : -1;
    const next = Math.min(Math.max(idx + delta, 0), listed.length - 1);
    selectMarker(listed[next]);
    requestAnimationFrame(() =>
      document.querySelector(`[data-mid="${listed[next].id}"]`)?.scrollIntoView({ block: "nearest" }));
  }, [listed, active, selectMarker]);

  const toggleReviewed = useCallback((id: string, advance = false) => {
    setReviewed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      if (reviewedKey) lsSet(reviewedKey, [...next]);
      return next;
    });
    if (advance) {
      const idx = listed.findIndex((x) => x.id === id);
      if (idx >= 0 && idx < listed.length - 1) step(1);
      else setActiveId(null);
    }
  }, [reviewedKey, step, listed]);

  // ── Edits ─────────────────────────────────────────────────────────────────
  const reloadAfterEdit = useCallback(async (msg: string) => {
    setNotice(msg);
    setActiveId(null);
    setSelKeys(new Set());
    setEditing(null);
    await load();
  }, [load]);

  const handleApply = useCallback(async () => {
    if (!report || !active || active.issue_id === null) return;
    const original = active.output || active.expected;
    if (!original) return;
    setBusy(true);
    setError(null);
    try {
      await applyFix(report.key, original, draft);
      await reloadAfterEdit(`Applied fix for ${active.title.toLowerCase()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [report, active, draft, reloadAfterEdit]);

  const handleUndo = useCallback(async () => {
    if (!report) return;
    setBusy(true);
    try {
      await undoFix(report.key);
      await reloadAfterEdit("Last edit reverted");
    } finally {
      setBusy(false);
    }
  }, [report, reloadAfterEdit]);

  async function handleReset() {
    if (!report) return;
    setBusy(true);
    try {
      await resetEdits(report.key);
      await reloadAfterEdit("All edits discarded");
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveLine() {
    if (!report || !editing) return;
    const next = [...lines];
    next[editing.line] = editing.value;
    setBusy(true);
    setError(null);
    try {
      await saveText(report.key, next.join("\n") + (raw.endsWith("\n") ? "\n" : ""));
      await reloadAfterEdit(`Saved line ${editing.line + 1}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function openInsert(m: Marker) {
    const words = (lines[m.line] ?? "").split(" ");
    const at = Math.min((m.w0 ?? 0) + 1, words.length);
    const value = [...words.slice(0, at), m.expected, ...words.slice(at)].join(" ");
    setEditing({ line: m.line, value });
    scrollElIntoPane(textRef.current, lineRefs.current[m.line] ?? null);
  }

  /** Tag / untag / split / merge at the reviewer's selection, saved as one undo step. */
  /** The line editor is open: the tag buttons work on what is selected (or where the cursor is) inside it. */
  function runEditInEditor(kind: "A" | "D" | "untag" | "split" | "merge"): boolean {
    const ta = taRef.current;
    if (!editing || !ta) return false;
    if (kind === "split" || kind === "merge") {
      setNotice(null);
      setError("Save or cancel the line edit first, then split or merge.");
      return true;
    }
    const s = ta.selectionStart;
    const e = ta.selectionEnd;
    const one = [editing.value];
    let out: EditOutcome;
    if (kind === "untag") {
      out = untag(one, 0, s);
      if (isError(out) && e !== s) out = untag(one, 0, e);
    } else {
      out = wrapTag(one, 0, s, e, kind);
    }
    if (isError(out)) {
      setNotice(null);
      setError(out.error);
      return true;
    }
    const value = out.lines[0];
    setError(null);
    setNotice(out.message);
    setEditing({ line: editing.line, value });
    requestAnimationFrame(() => {
      const t = taRef.current;
      if (!t) return;
      let caret = out.caret ?? Math.min(s, value.length);
      if (kind !== "untag" && out.caret === undefined) caret = value.indexOf(kind === "A" ? "<A]" : "<D]", s) + 3; // just after the new tag
      t.focus();
      t.setSelectionRange(caret, caret);
    });
    return true;
  }

  async function runEdit(kind: "A" | "D" | "untag" | "split" | "merge") {
    if (!report) return;
    if (runEditInEditor(kind)) return;
    const sel = selRef.current;
    const fallback = active ? active.line : null;
    let out: EditOutcome;
    if (kind === "merge") {
      const li = sel ? sel.li : fallback;
      out = li === null ? { error: "Click inside a paragraph first." } : mergeUp(lines, li);
    } else if (!sel) {
      out = { error: "Click or select text in the converted text first." };
    } else if (kind === "A" || kind === "D") {
      out = sel.li !== sel.endLi ? { error: "Select text inside one paragraph." } : wrapTag(lines, sel.li, sel.start, sel.end, kind);
    } else if (kind === "untag") {
      out = untag(lines, sel.li, sel.start);
    } else {
      out = splitAt(lines, sel.li, sel.start);
    }
    if (isError(out)) {
      setNotice(null);
      setError(out.error);
      return;
    }
    if (out.caret !== undefined) {
      selRef.current = null;
      setError(null);
      setNotice(out.message);
      setEditing({ line: out.focus, value: out.lines[out.focus], caret: out.caret });
      requestAnimationFrame(() => scrollElIntoPane(textRef.current, lineRefs.current[out.focus] ?? null));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await saveText(report.key, toText(out.lines, raw));
      selRef.current = null;
      await reloadAfterEdit(out.message);
      requestAnimationFrame(() => scrollElIntoPane(textRef.current, lineRefs.current[out.focus] ?? null));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  /** Approve = snapshot of the saved text. Editing afterwards makes it stale. */
  async function handleApprove() {
    if (!report) return;
    if (counts.error > 0 && !window.confirm(
      `${counts.error} error finding${counts.error > 1 ? "s are" : " is"} not marked reviewed.\n\nApprove this version anyway?`
    )) return;
    setBusy(true);
    setError(null);
    try {
      const res = await approveReview(report.key, job?.id);
      setApproval(res);
      setNotice("Approved. You can now submit it to Keyed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleSubmit() {
    if (!report || !approval?.approved) return;
    const where = submitCfg ? `${submitCfg.host}:${submitCfg.remote_dir}` : "the Keyed SFTP folder";
    if (!window.confirm(`Submit ${report.name} to Keyed?\n\n${where}\n\nThis uploads the approved version.`)) return;
    setBusy(true);
    setError(null);
    try {
      const res = await submitToKeyed(report.key);
      setApproval(res);
      setNotice(`Submitted to Keyed — ${res.remote_path} (${res.bytes.toLocaleString()} bytes, size verified).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleAudit() {
    if (!target.pdfPath) return;
    const pages = compare?.pages.length ?? job?.page_count;
    if (!window.confirm(
      `Run the tag audit?\n\nThis sends ${pages ? `${pages} highlighted page image${pages > 1 ? "s" : ""}` : "highlighted page images"} ` +
      `to ${llmCfg?.primary ?? "the model"} and COSTS TOKENS. It can take a few minutes, and the cost is added to LLM cost afterwards.`
    )) return;
    setAuditRunning(true);
    setError(null);
    try {
      setAudit(await runTagAudit(target.textPath, target.pdfPath, target.stateCode));
      setNotice("Tag audit complete");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAuditRunning(false);
    }
  }

  // ── Keyboard ──────────────────────────────────────────────────────────────
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT" || e.ctrlKey || e.metaKey) return;
      if (view !== "compare") return;
      if (e.key === "j" || e.key === "n") step(1);
      else if (e.key === "k" || e.key === "p") step(-1);
      else if (e.key === "r" && active) toggleReviewed(active.id, true);
      else if (e.key === "u") handleUndo();
      else if (e.key === "Escape") {
        setActiveId(null);
        setSelKeys(new Set());
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, step, active, toggleReviewed, handleUndo]);

  // ── PDF rubber-band selection ─────────────────────────────────────────────
  function frac(e: React.PointerEvent<HTMLDivElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height };
  }

  function onPagePointerDown(e: React.PointerEvent<HTMLDivElement>, page: number) {
    if (e.button !== 0) return;
    const f = frac(e);
    dragRef.current = { page, x0: f.x, y0: f.y, moved: false, key: (e.target as HTMLElement).dataset.k };
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function onPagePointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d) return;
    const f = frac(e);
    if (!d.moved && Math.hypot(f.x - d.x0, f.y - d.y0) < 0.008) return;
    d.moved = true;
    setBand({ page: d.page, x0: d.x0, y0: d.y0, x1: f.x, y1: f.y });
  }

  function onPagePointerUp(e: React.PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) return;
    if (!d.moved) {
      setBand(null);
      if (d.key) focusKeys([d.key], "pdf");
      else setSelKeys(new Set());
      return;
    }
    const f = frac(e);
    const [xa, xb] = [Math.min(d.x0, f.x), Math.max(d.x0, f.x)];
    const [ya, yb] = [Math.min(d.y0, f.y), Math.max(d.y0, f.y)];
    const hit = (byPage[d.page] ?? [])
      .filter(({ box }) => box.x < xb && box.x + box.w > xa && box.y < yb && box.y + box.h > ya)
      .map(({ key }) => key);
    setBand(null);
    focusKeys(hit, "pdf");
  }

  // ── Render ────────────────────────────────────────────────────────────────
  if (error && !report) return <p className="error">{error}</p>;
  if (!report) return <p className="muted">{loadMsg || "Loading…"}</p>;

  const integrityIssues = report.integrity.issues ?? [];
  const flagged = counts.total;

  function renderWord(word: string) {
    if (!DELIM_SPLIT.test(word)) return word;
    return word.split(DELIM_SPLIT).map((part, i) =>
      DELIM_SPLIT.test(part) && i % 2 === 1 ? (
        <span key={i} className="delim">{part}</span>
      ) : (
        part
      ));
  }

  function renderPdfPane() {
    if (!compare) {
      return <p className="muted">{target.pdfPath ? loadMsg || "Loading…" : "No source PDF available — location and sync are disabled."}</p>;
    }
    return compare.pages.map((p) => (
      <div
        key={p.index}
        className="pdf-page"
        style={{ aspectRatio: `1 / ${p.aspect}`, width: `${zoom}%` }}
        ref={(el) => { pageRefs.current[p.index] = el; }}
        onPointerDown={(e) => onPagePointerDown(e, p.index)}
        onPointerMove={onPagePointerMove}
        onPointerUp={onPagePointerUp}
      >
        <img src={comparePageUrl(compare.key, p.index)} alt={`Page ${p.index + 1}`} draggable={false} />

        {/* Faint severity boxes for every finding on this page. */}
        {markers.filter((m) => !m.parent && m.source !== "revisions" && m.id !== activeId).flatMap((m) =>
          m.boxes.filter((b) => b.page === p.index).map((b, i) => (
            <span
              key={`${m.id}-${i}`}
              className={`mbox mbox-${m.severity}${reviewed.has(m.id) ? " mbox-done" : ""}`}
              style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%` }}
            />
          )))}

        {/* The active finding (and its related children) in strong colour. */}
        {active && [active, ...(childrenOf.get(active.id) ?? [])].flatMap((m) =>
          m.boxes.filter((b) => b.page === p.index).map((b, i) => (
            <span
              key={`a-${m.id}-${i}`}
              className={`mbox mbox-active mbox-${m.severity}`}
              style={{ left: `${b.x * 100}%`, top: `${b.y * 100}%`, width: `${b.w * 100}%`, height: `${b.h * 100}%` }}
            />
          )))}

        {(byPage[p.index] ?? []).map(({ key, box }) => (
          <span
            key={key}
            data-k={key}
            className={selKeys.has(key) ? "wbox sel" : "wbox"}
            style={{ left: `${box.x * 100}%`, top: `${box.y * 100}%`, width: `${box.w * 100}%`, height: `${box.h * 100}%` }}
          />
        ))}

        {band && band.page === p.index && (
          <span
            className="band"
            style={{
              left: `${Math.min(band.x0, band.x1) * 100}%`,
              top: `${Math.min(band.y0, band.y1) * 100}%`,
              width: `${Math.abs(band.x1 - band.x0) * 100}%`,
              height: `${Math.abs(band.y1 - band.y0) * 100}%`,
            }}
          />
        )}
        <span className="page-label">Page {p.index + 1}</span>
      </div>
    ));
  }

  function renderTextPane() {
    return lines.map((line, li) => {
      const gm = gutterMarks.get(li);
      const isActiveLine = !!active && li >= active.line && li <= (active.end_line ?? active.line);
      if (editing && editing.line === li) {
        return (
          <div key={li} data-li={li} className="text-line editing" ref={(el) => { lineRefs.current[li] = el; }}>
            <span className="line-no">{li + 1}</span>
            <div className="line-edit">
              <textarea
                autoFocus
                ref={(el) => {
                  taRef.current = el;
                  // first render after "[A>" with no selection: put the cursor between the new tags
                  if (el && editing.caret !== undefined && el.dataset.placed !== "1") {
                    el.dataset.placed = "1";
                    el.focus();
                    el.setSelectionRange(editing.caret, editing.caret);
                  }
                }}
                value={editing.value}
                rows={Math.min(Math.max(Math.ceil(editing.value.length / 90), 2), 10)}
                onChange={(e) => setEditing({ line: li, value: e.target.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) handleSaveLine();
                  if (e.key === "Escape") setEditing(null);
                }}
              />
              <div className="approve-row">
                <button onClick={handleSaveLine} disabled={busy}>Save line (Ctrl+Enter)</button>
                <button className="link-button" onClick={() => setEditing(null)}>Cancel</button>
              </div>
            </div>
          </div>
        );
      }
      return (
        <div
          key={li}
          data-li={li}
          className={`text-line${isActiveLine ? " line-active" : ""}`}
          ref={(el) => { lineRefs.current[li] = el; }}
          onDoubleClick={(e) => {
            if ((e.target as HTMLElement).closest(".insert-flag")) return;
            window.getSelection()?.removeAllRanges();
            setEditing({ line: li, value: line });
          }}
        >
          <button
            className={`line-no${gm ? ` gut gut-${gm.severity}` : ""}`}
            title={gm ? gm.title : "Double-click the line to edit it"}
            onClick={() => gm && selectMarker(gm)}
          >
            {li + 1}
          </button>
          <span className="line-body">
            {line.split(" ").map((word, wi) => {
              const key = `${li}_${wi}`;
              const linked = !!cmap && key in cmap;
              const mk = wordMarks.get(key);
              const flags = insertMarks.get(key);
              const tag = wordTag.get(key);
              const cls = [
                "tw",
                linked ? "linked" : "unlinked",
                tag === "A" ? "tag-add" : tag === "D" ? "tag-del" : "",
                mk ? `mk mk-${mk.severity}${reviewed.has(mk.id) ? " mk-done" : ""}` : "",
                activeKeys.has(key) ? "mk-active" : "",
                selKeys.has(key) ? "sel" : "",
              ].filter(Boolean).join(" ");
              return (
                <Fragment key={wi}>
                  <span
                    ref={(el) => { wordRefs.current[key] = el; }}
                    data-k={key}
                    className={cls}
                    title={mk?.title}
                    onClick={() => {
                      if (window.getSelection()?.isCollapsed === false) return;
                      if (mk && !mk.parent) selectMarker(mk);
                      else if (linked) focusKeys([key], "text");
                    }}
                  >
                    {renderWord(word)}
                  </span>
                  {flags?.map((m) => (
                    <button
                      key={m.id}
                      className={`insert-flag insert-${m.severity}${m.id === activeId ? " insert-active" : ""}`}
                      title={`${m.title} — expected here`}
                      onClick={() => selectMarker(m)}
                    >
                      ⚑
                    </button>
                  ))}{" "}
                </Fragment>
              );
            })}
          </span>
        </div>
      );
    });
  }

  function renderIssueItem(m: Marker, child = false) {
    const isActive = m.id === activeId;
    const done = reviewed.has(m.id);
    const kids = childrenOf.get(m.id) ?? [];
    const snippet = (m.output || m.expected || m.detail || "").replace(/\s+/g, " ").slice(0, 110);
    // Replacing with the PDF wording only makes sense when the PDF has wording and the text differs in place.
    const canApply = m.issue_id !== null && !m.insertion && !!m.expected && m.kind !== "content_reordered";
    return (
      <div key={m.id} data-mid={m.id} className={`iss${isActive ? " iss-active" : ""}${done ? " iss-done" : ""}${child ? " iss-child" : ""}`}>
        <button className="iss-head" aria-expanded={isActive} onClick={() => (isActive ? setActiveId(null) : selectMarker(m))}>
          {m.source === "revisions" ? (
            <span className={`rev-chip ${m.kind === "revision_add" ? "rev-a" : "rev-d"}`}>
              {m.kind === "revision_add" ? "A" : "D"}
            </span>
          ) : (
            <span className={`iss-dot sev-bg-${m.severity}`} />
          )}
          <span className="iss-title">{m.title}</span>
          {m.scope && m.scope !== "ALL" && <span className="iss-scope">{m.scope}</span>}
          <span className="iss-loc">
            line {m.line + 1}{m.page !== null ? ` · p${m.page + 1}` : ""}
          </span>
          {done && <span className="iss-check"><Icon name="check" /></span>}
          <span className="iss-chev"><Icon name={isActive ? "chevron-down" : "chevron-right"} /></span>
        </button>
        {!isActive && snippet && <div className="iss-snippet">{snippet}</div>}
        {kids.length > 0 && (
          <button
            className="link-button iss-kids"
            onClick={() => setExpandedParents((prev) => {
              const next = new Set(prev);
              if (next.has(m.id)) next.delete(m.id);
              else next.add(m.id);
              return next;
            })}
          >
            {expandedParents.has(m.id) ? "▾" : "▸"} {kids.length} related finding{kids.length > 1 ? "s" : ""} explained by this
          </button>
        )}
        {isActive && (
          <div className="iss-body">
            {m.detail && <p className="iss-why">{m.detail}</p>}
            {(m.expected || m.output) && (
              <FindingCompare expected={m.expected} output={m.output}
                              plain={m.kind === "content_reordered" || m.source !== "content"} notes={m.source === "content"} />
            )}
            <div className="iss-actions">
              {canApply && (
                <button onClick={handleApply} disabled={busy} title="Put the PDF's wording in the converted text">
                  Replace with PDF wording
                </button>
              )}
              {m.insertion && m.expected && m.source === "content" && (
                <button onClick={() => openInsert(m)} disabled={busy}>Insert missing text…</button>
              )}
              <button className="secondary" onClick={() => setEditing({ line: m.line, value: lines[m.line] ?? "" })}>
                Edit line {m.line + 1}
              </button>
              <button className="secondary" onClick={() => toggleReviewed(m.id, !done)}>
                {done ? "Undo reviewed" : "Mark reviewed"} <kbd>r</kbd>
              </button>
            </div>
            {canApply && (
              <details className="iss-replace">
                <summary>Change the replacement wording first</summary>
                <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} />
              </details>
            )}
          </div>
        )}
        {expandedParents.has(m.id) && kids.map((c) => renderIssueItem(c, true))}
      </div>
    );
  }

  const text_ruler = lines.length > 1 ? lines.length : 1;
  const visiblePanes = [panelOpen, showPdf && !!target.pdfPath, showText].filter(Boolean).length;
  const dataPanes = (showPdf && !!target.pdfPath ? 1 : 0) + (showText ? 1 : 0);
  const pdfPane = showPdf && !!target.pdfPath;
  const gridColumns = [
    panelOpen ? `${findingsPx}px` : null,
    panelOpen && dataPanes > 0 ? "16px" : null,
    pdfPane ? (showText ? `minmax(0, ${pdfFr}fr)` : "minmax(0, 1fr)") : null,
    pdfPane && showText ? "16px" : null,
    showText ? "minmax(0, 1fr)" : null,
  ].filter(Boolean).join(" ") || "1fr";

  function resetLayout() {
    setFindingsPx(DEFAULT_FINDINGS_PX);
    setPdfFr(DEFAULT_PDF_FR);
  }

  function setLayout(kind: "balanced" | "pdf" | "text") {
    setShowPdf(true);
    setShowText(true);
    if (kind === "balanced") resetLayout();
    else if (kind === "pdf") {
      setFindingsPx(300);
      setPdfFr(3);
    } else {
      setFindingsPx(340);
      setPdfFr(0.6);
    }
  }

  /** Drag the divider between two panes. "findings" resizes the findings column; "data" moves the PDF/text split. */
  function dragSplitter(e: React.PointerEvent<HTMLDivElement>, kind: "findings" | "data") {
    e.preventDefault();
    const bar = e.currentTarget;
    const prev = bar.previousElementSibling as HTMLElement | null;
    const next = bar.nextElementSibling as HTMLElement | null;
    if (!prev || !next) return;
    const startX = e.clientX;
    const w0 = prev.getBoundingClientRect().width;
    const w1 = next.getBoundingClientRect().width;
    document.body.classList.add("dragging-split");
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      if (kind === "findings") setFindingsPx(clamp(w0 + dx, 240, 720));
      else {
        const total = w0 + w1;
        const a = clamp(w0 + dx, 220, total - 220);
        setPdfFr(a / (total - a));
      }
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.classList.remove("dragging-split");
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function nudgeSplitter(e: React.KeyboardEvent, kind: "findings" | "data") {
    const dir = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : 0;
    if (!dir) return;
    e.preventDefault();
    if (kind === "findings") setFindingsPx((v) => clamp(v + dir * 24, 240, 720));
    else setPdfFr((v) => clamp(v + dir * 0.1, 0.4, 4));
  }
  // Findings alone fills the width instead of a 330px sliver.
  const columns = panelOpen && dataPanes === 0 ? "1fr" : gridColumns;

  // An audit that never reached the model (empty checklist) did not verify anything: do not present it as 100%.
  const auditUnverified = !!audit && audit.total > 0 &&
    (audit.verified === false || (audit.verified === undefined && audit.usage?.api_calls === 0));

  return (
    <div className="rw">
      <div className="rw-head">
        <button className="link-button" onClick={onBack}>← Back</button>
        <h2>{report.name}</h2>
        {target.stateCode && <span className="pill">{target.stateCode}</span>}
        {job?.display_type && <span className="pill pill-type">{job.display_type}</span>}
        {report.edited && <span className="pill pill-edit">edited</span>}
        {job && <span className={`status-badge status-${job.status}`}>{job.status}</span>}
        <span className="rw-spacer" />

        <button className="secondary" onClick={() => setFullPage((v) => !v)}
                title={fullPage ? "Show the header and menu again" : "Hide the header and menu so the panes fill the window"}>
          <Icon name={fullPage ? "compress" : "expand"} /> {fullPage ? "Exit full page" : "Full page"}
        </button>

        {/* Save → Approve → Submit: the same controls whether the file came from an upload or a pipeline session. */}
        {/* Status only — the buttons next to it are the actions, so nothing is shown twice. */}
        <ul className="flow">
          <li className="flow-done" title="Edits are saved to the working copy as you make them">
            <Icon name="check" /> Saved{report.edited ? " · edited" : ""}
          </li>
          {approval?.approved && (
            <li className="flow-done" title={`Approved ${approval.approved_at ?? ""}`}>
              <Icon name="check" /> Approved {approval.approved_at?.slice(11, 16)}
            </li>
          )}
          {approval?.stale && (
            <li className="flow-warn" title="The text changed after it was approved"><Icon name="warn" /> Edited since approval</li>
          )}
          {approval?.submitted && (
            <li className="flow-done" title={approval.submitted_to ?? undefined}>
              <Icon name="check" /> Submitted {approval.submitted_at?.slice(11, 16)}
            </li>
          )}
        </ul>
        <a
          className="btn-link"
          href={`/api/validation/download?key=${encodeURIComponent(report.key)}`}
          download={report.name}
          title="Download the converted text with every edit applied"
        >
          <Icon name="download" /> Download
        </a>
        <button
          onClick={handleApprove}
          disabled={busy || !!approval?.approved}
          title={approval?.approved ? "This exact text is already approved" : "Approve the current saved text"}
        >
          {approval?.approved ? "Approved" : approval?.stale ? "Re-approve" : "Approve"}
        </button>
        <button
          className="submit-btn"
          onClick={handleSubmit}
          disabled={busy || !approval?.approved || !submitCfg?.password_set}
          title={
            !approval?.approved
              ? "Approve the latest version first"
              : !submitCfg?.password_set
                ? "FTP_PASSWORD is not set in the project .env"
                : approval.submitted
                  ? "Already submitted — click to submit again"
                  : `Upload to ${submitCfg?.host}:${submitCfg?.remote_dir}`
          }
        >
          <Icon name="send" /> {approval?.submitted ? "Submit again" : "Submit to Keyed"}
        </button>
      </div>

      {(error || notice) && (
        <div className={`rw-toast ${error ? "rw-toast-error" : "rw-toast-ok"}`} role={error ? "alert" : "status"}>
          <span>{error ?? notice}</span>
          <button className="link-button" aria-label="Dismiss" onClick={() => { setError(null); setNotice(null); }}>×</button>
        </div>
      )}

      <nav className="rw-tabs">
        {([
          ["compare", "PDF ↔ Text Compare"],
          ["summary", "Summary"],
          ["log", "Activity Log"],
        ] as [View, string][]).map(([v, label]) => (
          <button key={v} className={view === v ? "tab active" : "tab"} onClick={() => setView(v)}>
            {label}
            {v === "compare" && flagged - counts.done > 0 && (
              <span className="tab-badge">{flagged - counts.done}</span>
            )}
          </button>
        ))}
      </nav>

      {view === "compare" && (
        <>
          <div className="rw-toolbar">
            <div className="seg" role="group" aria-label="Show or hide panes">
              {([
                ["Findings", panelOpen, () => setPanelOpen((v) => !v), true],
                ["PDF", showPdf, () => setShowPdf((v) => !v), !!target.pdfPath],
                ["Output text", showText, () => setShowText((v) => !v), true],
              ] as [string, boolean, () => void, boolean][]).map(([label, on, toggle, available]) => (
                <button
                  key={label}
                  className={on ? "seg-btn active" : "seg-btn"}
                  onClick={toggle}
                  disabled={!available || (on && visiblePanes === 1)}
                  title={on ? (visiblePanes === 1 ? "At least one pane must stay visible" : `Hide ${label}`) : `Show ${label}`}
                >
                  <Icon name={on ? "eye" : "eye-off"} /> {label}
                </button>
              ))}
            </div>
            <label className="chk"><input type="checkbox" checked={syncOn} onChange={(e) => setSyncOn(e.target.checked)} disabled={!(showPdf && showText)} /> Sync scroll</label>
            <label className="chk" title="Line numbers are only a reference for the findings (&quot;line 1006&quot;); they are not in the downloaded file">
              <input type="checkbox" checked={showLn} onChange={(e) => setShowLn(e.target.checked)} /> Line numbers
            </label>
            <span className="rw-sep" />
            <button className="secondary" onClick={() => setZoom((z) => Math.max(z - 20, 50))}>−</button>
            <span className="muted">{zoom}%</span>
            <button className="secondary" onClick={() => setZoom((z) => Math.min(z + 20, 300))}>+</button>
            <button className="secondary" onClick={() => setZoom(100)} title="Fit the page to the width of the PDF pane">Fit</button>
            <div className="seg" role="group" aria-label="Pane layout">
              <button className="seg-btn" onClick={() => setLayout("balanced")} title="Findings, PDF and text side by side">Balanced</button>
              <button className="seg-btn" onClick={() => setLayout("pdf")} title="Give most of the width to the PDF">PDF focus</button>
              <button className="seg-btn" onClick={() => setLayout("text")} title="Give most of the width to the converted text">Text focus</button>
            </div>
            <span className="rw-sep" />
            <button className="secondary" onClick={handleUndo} disabled={busy || report.history.length === 0}>
              Undo ({report.history.length})
            </button>
            <button className="link-button" onClick={handleReset} disabled={busy || report.history.length === 0}>
              Discard edits
            </button>
            <span className="rw-spacer" />
            {compare && (
              <span className="muted"
                title={`${Math.round((compare.mapped_words / Math.max(compare.pdf_words, 1)) * 100)}% of the PDF's words are linked to the converted text`}>
                Click a word or drag a box on the PDF to find it in the text · double-click a line to edit it
              </span>
            )}
          </div>

          <div
            className="rw-body"
            style={{ gridTemplateColumns: columns }}
          >
            {panelOpen && (
              <aside className="rw-issues">
                <div className="rw-issues-head">
                  <strong>Findings</strong>
                  <span className="muted">
                    {counts.done}/{counts.total} reviewed
                  </span>
                  <span className="rw-spacer" />
                  <button className="secondary sm" onClick={() => step(-1)} title="Previous (k)">▲</button>
                  <button className="secondary sm" onClick={() => step(1)} title="Next (j)">▼</button>
                  <button className="secondary sm" onClick={() => setPanelOpen(false)} title="Hide the findings panel"><Icon name="chevron-left" /></button>
                </div>
                <div className="rw-progress"><div style={{ width: `${counts.total ? (counts.done / counts.total) * 100 : 100}%` }} /></div>
                <div className="filter-row">
                  {(() => {
                    const n: Record<SourceFilter, number> = {
                      all: counts.total, paragraph: counts.paragraph, tags: counts.tags, content: counts.content,
                      rules: counts.rules, layout: counts.layout, revisions: counts.revisions,
                    };
                    const label: Record<SourceFilter, string> = {
                      all: `All ${counts.total}`, paragraph: `Paragraphs ${counts.paragraph}`, tags: `Tags ${counts.tags}`,
                      content: `Content ${counts.content}`, rules: `${target.stateCode ?? "State"} rules ${counts.rules}`,
                      layout: `Layout ${counts.layout}`, revisions: `Revisions ${counts.revisionsDone}/${counts.revisions}`,
                    };
                    return (["all", "content", "paragraph", "tags", "rules", "layout", "revisions"] as SourceFilter[])
                      .filter((v) => v === "all" || n[v] > 0 || v === sourceFilter)
                      .map((v) => (
                        <button key={v} className={sourceFilter === v ? "chip active" : "chip"} onClick={() => setSourceFilter(v)}>
                          {label[v]}
                        </button>
                      ));
                  })()}
                </div>
                <div className="filter-row">
                  {counts.error > 0 && counts.warning > 0 && (
                    <>
                      <button className={sevFilter === "all" ? "chip active" : "chip"} onClick={() => setSevFilter("all")}>Any severity</button>
                      <button className={sevFilter === "error" ? "chip active" : "chip"} onClick={() => setSevFilter("error")}>Errors {counts.error}</button>
                      <button className={sevFilter === "warning" ? "chip active" : "chip"} onClick={() => setSevFilter("warning")}>Warnings {counts.warning}</button>
                    </>
                  )}
                  <label className="chk"><input type="checkbox" checked={hideReviewed} onChange={(e) => setHideReviewed(e.target.checked)} /> Hide reviewed</label>
                </div>
                {(report.hidden_artifacts?.length ?? 0) > 0 && (
                  <details className="hidden-art">
                    <summary>
                      {report.hidden_artifacts!.length} source difference{report.hidden_artifacts!.length > 1 ? "s" : ""} ignored — page footers/timestamps
                    </summary>
                    <p className="muted">
                      The PDF extraction contains page furniture (timestamps, barcodes, page numbers) that the
                      conversion is supposed to drop, so these are not counted as problems.
                    </p>
                    <ul>
                      {report.hidden_artifacts!.map((h) => (
                        <li key={h.id}>
                          <b>{h.reason}</b>: <code>{h.expected.slice(0, 80)}</code>
                          {h.output ? <> → <code>{h.output.slice(0, 40)}</code></> : " → removed"}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                <div className="rw-issue-list">
                  {listed.length === 0 ? (
                    <div className="empty-state compact">
                      <Icon name="check" />
                      {markers.length === 0
                        ? "Nothing was flagged."
                        : hideReviewed && counts.done > 0 && counts.done >= counts.total
                          ? `All ${counts.total} findings reviewed.`
                          : "No findings match these filters."}
                      {hideReviewed && counts.done > 0 && (
                        <button className="link-button" onClick={() => setHideReviewed(false)}>Show reviewed</button>
                      )}
                    </div>
                  ) : (
                    listed.map((m) => renderIssueItem(m))
                  )}
                </div>
                <div className="rw-kbd muted">j/k next · prev &nbsp; r reviewed &nbsp; u undo &nbsp; Esc clear</div>
              </aside>
            )}

            {panelOpen && dataPanes > 0 && (
              <div className="splitter" role="separator" aria-orientation="vertical" tabIndex={0}
                   title="Drag to resize · double-click to reset" onPointerDown={(e) => dragSplitter(e, "findings")}
                   onDoubleClick={resetLayout} onKeyDown={(e) => nudgeSplitter(e, "findings")} />
            )}

            {showPdf && !!target.pdfPath && (
              <section className="rw-pane">
                <div className="rw-pane-title">Source PDF</div>
                <div className="pdf-scroll rw-scroll" ref={pdfRef} onScroll={onPdfScroll}>
                  {renderPdfPane()}
                </div>
              </section>
            )}

            {pdfPane && showText && (
              <div className="splitter" role="separator" aria-orientation="vertical" tabIndex={0}
                   title="Drag to resize · double-click to reset" onPointerDown={(e) => dragSplitter(e, "data")}
                   onDoubleClick={resetLayout} onKeyDown={(e) => nudgeSplitter(e, "data")} />
            )}

            {showText && (
            <section className={showLn ? "rw-pane" : "rw-pane hide-ln"}>
              <div className="rw-pane-title">Converted text</div>
              <div className="edit-toolbar" onMouseDown={(e) => e.preventDefault()}>
                <button className="tbtn tag-a" disabled={busy} onClick={() => runEdit("A")}
                  title="Wrap the selected text in [A>…<A] (addition)">[A&gt;</button>
                <button className="tbtn tag-d" disabled={busy} onClick={() => runEdit("D")}
                  title="Wrap the selected text in [D>…<D] (deletion)">[D&gt;</button>
                <button className="tbtn" disabled={busy} onClick={() => runEdit("untag")}
                  title="Remove the tag around the cursor (keeps the text)"><Icon name="x" /> Untag</button>
                <span className="tsep" />
                <button className="tbtn" disabled={busy} onClick={() => runEdit("split")}
                  title="Split the paragraph at the cursor. Inside a tag, each half keeps its own single tag."><Icon name="scissors" /> Split</button>
                <button className="tbtn" disabled={busy} onClick={() => runEdit("merge")}
                  title="Merge this paragraph into the one above. Touching tags of the same type become one tag.">⊕ Merge ↑</button>
                <span className="muted edit-hint">Select text or click in a paragraph, then use a tool · undo with Undo</span>
              </div>
              <div className="rw-text-wrap">
                <div className="text-scroll rw-scroll" ref={textRef} onScroll={onTextScroll} onMouseUp={onTextMouseUp}>
                  {renderTextPane()}
                </div>
                <div className="rw-ruler" aria-hidden={markers.length === 0}>
                  {markers.filter((m) => !m.parent && m.source !== "revisions").map((m) => (
                    <button
                      key={m.id}
                      className={`rtick sev-bg-${m.severity}${m.id === activeId ? " rtick-active" : ""}${reviewed.has(m.id) ? " rtick-done" : ""}`}
                      style={{ top: `${(m.line / text_ruler) * 100}%` }}
                      title={`${m.title} — line ${m.line + 1}`}
                      onClick={() => selectMarker(m)}
                    />
                  ))}
                </div>
              </div>
            </section>
            )}
          </div>
        </>
      )}

      {view === "summary" && (
        <div className="rw-summary">
          {job?.status === "done" && <JobMetrics job={job} audit={audit} />}
          <div className="score-row">
            <ScoreCard
              label="Content Accuracy"
              pct={report.content_accuracy_pct}
              sub={
                report.content_accuracy_raw_pct != null && report.content_accuracy_pct != null &&
                report.content_accuracy_pct - report.content_accuracy_raw_pct >= 0.05
                  ? `Raw text match ${report.content_accuracy_raw_pct.toFixed(2)}% — also counts page footers and moved text`
                  : undefined
              }
            />
            <ScoreCard
              label="Layout Accuracy"
              pct={report.layout_accuracy_pct}
              sub={
                report.layout_accuracy_raw_pct != null && report.layout_accuracy_pct != null &&
                report.layout_accuracy_pct - report.layout_accuracy_raw_pct >= 0.05
                  ? `Checker's raw score ${report.layout_accuracy_raw_pct.toFixed(1)}% — also counts short address/signature lines kept apart on purpose`
                  : undefined
              }
            />
            <ScoreCard
              label="Tag Accuracy"
              pct={auditUnverified ? null : audit?.tag_accuracy_pct ?? null}
              sub={audit
                ? (audit.total === 0 || auditUnverified
                    ? (audit.note ?? (audit.total === 0
                        ? "Nothing to audit: the checklist was empty, so the model was not called and nothing was charged."
                        : `Not verified: the model was not called, so the ${audit.total} tag(s) were not checked.`))
                    : `${audit.passes} pass · ${audit.warnings} warning · ${audit.errors} error · ${audit.total} checked`)
                : target.pdfPath ? undefined : "Needs the source PDF."}
              action={!audit && target.pdfPath ? (
                <div className="tile-actions">
                  <button className="tile-btn" onClick={handleAudit} disabled={auditRunning}>
                    {auditRunning ? "Auditing… (can take minutes)" : "Run tag audit"}
                  </button>
                  <span className="tile-warn" title="The tag audit uses the model, so it costs tokens. You will be asked to confirm before anything is sent.">
                    <Icon name="warn" /> Costs tokens
                  </span>
                </div>
              ) : undefined}
            />
            <div className={`score-card ${integrityIssues.length ? "score-bad" : "score-good"}`}>
              <span className="score-label">Tag Integrity</span>
              <span className="score-value">{integrityIssues.length ? `${integrityIssues.length} issue(s)` : "pass"}</span>
            </div>
            <div className="score-card score-info">
              <span className="score-label">Findings</span>
              {loadMsg ? (
                <>
                  <span className="score-value">…</span>
                  <span className="score-sub">{loadMsg}</span>
                </>
              ) : (
                <>
                  <span className="score-value">{counts.done}/{counts.total}</span>
                  <span className="score-sub">reviewed</span>
                </>
              )}
            </div>
          </div>

          {integrityIssues.length > 0 && (
            <div className="card">
              <strong>Tag integrity</strong>
              <ul className="integrity-list">
                {integrityIssues.map((msg, i) => <li key={i}>{msg}</li>)}
              </ul>
              <button className="secondary" onClick={() => setView("compare")}>Review them in the compare view →</button>
            </div>
          )}

          {catalog && (
            <details className="card rules-card">
              <summary>
                <strong>Rules applied{catalog.spec ? ` — ${catalog.spec.name} (${catalog.state})` : ""}</strong>{" "}
                <span className="muted">
                  {catalog.rules.length} checked · {catalog.rules.filter((r) => r.findings).length
                    ? `${catalog.rules.filter((r) => r.findings).length} with findings`
                    : "all passed"}
                </span>
              </summary>
              {catalog.spec && (
                <p className="muted">
                  Additions: <b>{catalog.spec.add.join(" + ") || "—"}</b> · Deletions:{" "}
                  <b>{catalog.spec.delete.join(" + ") || "—"}</b>
                  {catalog.spec.all_caps_condition ? ` · ALL CAPS only ${catalog.spec.all_caps_condition}` : ""}
                  {catalog.spec.tag_scope ? ` · tag scope: ${catalog.spec.tag_scope}` : ""}
                  <br />Remove: {catalog.spec.remove}
                </p>
              )}
              <table className="rules-table">
                <tbody>
                  {catalog.rules.map((r) => (
                    <tr key={r.id}>
                      <td className={r.findings ? "rt-bad" : "rt-ok"}>{r.findings ? r.findings : <Icon name="check" />}</td>
                      <td>
                        <b>{r.title}</b>
                        {r.scope !== "ALL" && <span className="iss-scope">{r.scope}</span>}
                        <div className="muted">{r.requirement}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {catalog.manual.length > 0 && (
                <details className="manual-checks">
                  <summary>
                    <strong>Reviewer checklist from the state spec</strong>{" "}
                    <span className="muted">({catalog.manual.length} — cannot be checked from text)</span>
                  </summary>
                  <ul className="notes-list">
                    {catalog.manual.map((m, i) => (
                      <li key={i}>
                        <span className="pill">
                          {/^\s*(do not|don't|never)\b/i.test(m.text) ? "do not tag" : m.kind === "add" ? "add" : "delete"}
                        </span>{" "}
                        {m.text.length > 260 ? `${m.text.slice(0, 260)}…` : m.text}
                      </li>
                    ))}
                  </ul>
                  <p className="muted">{catalog.formatting_note}</p>
                </details>
              )}
            </details>
          )}

          {extra}
        </div>
      )}

      {view === "log" && (
        <div className="card">
          {job ? <ActivityLog steps={job.steps_log} /> : <p className="muted">No activity log for pipeline sessions.</p>}
        </div>
      )}
    </div>
  );
}
