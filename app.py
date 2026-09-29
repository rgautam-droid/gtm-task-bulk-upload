"""GTM Task Bulk Upload — Streamlit UI for FieldAssist V3 CreateUpdateTasks.

Run:  streamlit run app.py
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

import core

APP_DIR = Path(__file__).resolve().parent
PRESETS_FILE = APP_DIR / "presets.json"
RUNS_DIR = APP_DIR / "runs"

DEFAULT_PRESETS = {
    "CMF Red 150": {
        "taskFocusAreaName": "CMF Red 150", "productHierarchyLevel": 0, "calculationMeasure": 2,
        "taskLevelHierarchy": 5, "taskLevelHierarchyERPIDs": ["E65", "E53", "ZV9"],
        "totalTarget": 89, "achievedTarget": 0,
    },
    "Brilliant Star": {
        "taskFocusAreaName": "Brilliant Star", "productHierarchyLevel": 0, "calculationMeasure": 2,
        "taskLevelHierarchy": 5, "taskLevelHierarchyERPIDs": ["FO5"],
        "totalTarget": 89, "achievedTarget": 0,
    },
}
BASE_KEYS = ["taskFocusAreaName", "productHierarchyLevel", "calculationMeasure", "taskLevelHierarchy",
             "taskLevelHierarchyERPIDs", "totalTarget", "achievedTarget"]

st.set_page_config(page_title="GTM Task Bulk Upload", page_icon="📤", layout="wide")


def _secret(name: str) -> str:
    """Read from Streamlit secrets (cloud) or env var (local); '' if absent."""
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "")


# Optional app-level passcode gate (set APP_PASSCODE in Streamlit Cloud secrets)
_APP_PASSCODE = _secret("APP_PASSCODE")
if _APP_PASSCODE and not st.session_state.get("_authed"):
    st.title("🔒 GTM Task Bulk Upload")
    code = st.text_input("App passcode", type="password")
    if code:
        if code == _APP_PASSCODE:
            st.session_state["_authed"] = True
            st.rerun()
        st.error("Wrong passcode")
    st.stop()


# ------------------------------------------------------------- presets ---
def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            st.warning("presets.json is invalid — using built-in presets.")
    return dict(DEFAULT_PRESETS)


def save_presets(p: dict):
    PRESETS_FILE.write_text(json.dumps(p, indent=2), encoding="utf-8")


def apply_preset(t: dict):
    ss = st.session_state
    ss.t_focus = t.get("taskFocusAreaName", "")
    ss.t_erp = ", ".join(core.split_ids(t.get("taskLevelHierarchyERPIDs", [])))
    ss.t_phl = int(t.get("productHierarchyLevel", 0))
    ss.t_cm = int(t.get("calculationMeasure", 2))
    ss.t_tlh = int(t.get("taskLevelHierarchy", 5))
    ss.t_total = float(t.get("totalTarget", 0))
    ss.t_ach = float(t.get("achievedTarget", 0))
    extra = {k: v for k, v in t.items() if k not in BASE_KEYS}
    ss.t_extra = json.dumps(extra, indent=2) if extra else "{}"


def on_preset_change():
    name = st.session_state.preset_sel
    if name in st.session_state.presets:
        apply_preset(st.session_state.presets[name])


ss = st.session_state
if "presets" not in ss:
    ss.presets = load_presets()
    first = next(iter(ss.presets))
    ss.preset_sel = first
    apply_preset(ss.presets[first])
ss.setdefault("last_run", None)


def current_template() -> tuple[dict, list[str]]:
    errs = []
    t = {
        "taskFocusAreaName": ss.t_focus.strip(),
        "productHierarchyLevel": int(ss.t_phl),
        "calculationMeasure": int(ss.t_cm),
        "taskLevelHierarchy": int(ss.t_tlh),
        "taskLevelHierarchyERPIDs": core.split_ids(ss.t_erp),
        "totalTarget": core._num(ss.t_total),
        "achievedTarget": core._num(ss.t_ach),
    }
    try:
        extra = json.loads(ss.t_extra or "{}")
        if not isinstance(extra, dict):
            raise ValueError
        t.update(extra)
    except Exception:
        errs.append("Extra fields must be a JSON object, e.g. {\"key\": \"value\"}")
    return t, errs + core.validate_template(t)


# ------------------------------------------------------------- sidebar ---
with st.sidebar:
    st.header("🔌 Connection")
    api_url = st.text_input("API URL", core.DEFAULT_URL)
    api_user = st.text_input("Username", "Internal")
    api_pass = st.text_input("Password", _secret("FA_API_PASSWORD"), type="password",
                             help="Kept in memory only. Tip: set env var FA_API_PASSWORD to prefill.")
    wrap_key = st.text_input("Wrap payload key (optional)", "",
                             help="Leave blank to send a raw JSON array. Set e.g. 'data' to send {\"data\": [...]}")
    st.header("⚙️ Performance")
    batch_size = st.number_input("Batch size", 1, 5000, 1000, step=100)
    workers = st.number_input("Parallel workers", 1, 32, 8)
    timeout = st.number_input("Request timeout (s)", 5, 600, 60)
    retries = st.number_input("Max retries / batch", 0, 10, 3)
    backoff = st.number_input("Back-off factor", 1.0, 5.0, 1.5, step=0.1)
    st.caption("Smaller batches = finer failure isolation (a failed batch fails all its codes).")

host = urlparse(api_url).netloc or api_url
st.title("📤 GTM Task Bulk Upload")
st.caption(f"POST `{api_url}`")

tab_t, tab_i, tab_u = st.tabs(["① Task template", "② Input codes", "③ Upload & results"])

# ------------------------------------------------------------ template ---
with tab_t:
    c1, c2 = st.columns([3, 2])
    with c1:
        st.selectbox("Preset", list(ss.presets.keys()), key="preset_sel", on_change=on_preset_change)
        a, b = st.columns(2)
        a.text_input("taskFocusAreaName", key="t_focus")
        b.text_input("taskLevelHierarchyERPIDs (comma-separated)", key="t_erp")
        a, b, c = st.columns(3)
        a.number_input("productHierarchyLevel", step=1, key="t_phl")
        b.number_input("calculationMeasure", step=1, key="t_cm")
        c.number_input("taskLevelHierarchy", step=1, key="t_tlh")
        a, b = st.columns(2)
        a.number_input("totalTarget", min_value=0.0, step=1.0, format="%g", key="t_total")
        b.number_input("achievedTarget", min_value=0.0, step=1.0, format="%g", key="t_ach")
        with st.expander("Extra payload fields (JSON, optional)"):
            st.text_area("Merged into every task", key="t_extra", height=120,
                         help='Any additional API fields, e.g. {"startDate": "2026-10-01"}')

        st.markdown("**Save as preset**")
        a, b, c = st.columns([3, 1, 1])
        new_name = a.text_input("Preset name", value=ss.t_focus, label_visibility="collapsed")
        tpl, _ = current_template()
        if b.button("💾 Save", width="stretch"):
            ss.presets[new_name.strip() or tpl["taskFocusAreaName"]] = tpl
            save_presets(ss.presets)
            st.success(f"Saved preset '{new_name}'")
        if c.button("🗑 Delete", width="stretch", disabled=len(ss.presets) <= 1):
            ss.presets.pop(ss.preset_sel, None)
            save_presets(ss.presets)
            del ss["preset_sel"]
            st.rerun()
    with c2:
        tpl, tpl_errs = current_template()
        st.markdown("**Payload per task**")
        st.code(json.dumps({**tpl, "taskEntityERPId": "<CUST_CODE>"}, indent=2), language="json")
        for e in tpl_errs:
            st.error(e)
        st.caption("Per-row columns in the input file (totalTarget, taskFocusAreaName, "
                   "taskLevelHierarchyERPIDs, …) override these values.")

# --------------------------------------------------------------- input ---
@st.cache_data(show_spinner=False)
def _parse_file(data: bytes, name: str, header_mode: str, code_col: str | None):
    df = core.load_table(data, name)
    hdr = None if header_mode == "Auto" else header_mode == "Yes"
    return df, core.parse_table(df, header=hdr, code_col=code_col)


parsed: core.ParsedInput | None = None
with tab_i:
    src = st.radio("Source", ["Upload file", "Paste codes"], horizontal=True)
    if src == "Upload file":
        up = st.file_uploader("CSV / Excel — first column CUST_CODE (header optional)",
                              type=["csv", "txt", "xlsx", "xls"])
        a, b = st.columns(2)
        header_mode = a.selectbox("First row is header?", ["Auto", "Yes", "No"])
        if up is not None:
            data = up.getvalue()
            df_raw, parsed = _parse_file(data, up.name, header_mode, None)
            if parsed.header_used:
                hdrs = [str(x).strip() for x in df_raw.iloc[0].tolist()]
                cc = b.selectbox("Code column", hdrs, index=hdrs.index(parsed.code_column)
                                 if parsed.code_column in hdrs else 0)
                if cc != parsed.code_column:
                    _, parsed = _parse_file(data, up.name, header_mode, cc)
    else:
        txt = st.text_area("One CUST_CODE per line", height=220)
        if txt.strip():
            parsed = core.parse_pasted(txt)

    if parsed:
        m = st.columns(5)
        m[0].metric("Rows read", f"{parsed.total_rows:,}")
        m[1].metric("Unique tasks", f"{len(parsed.rows):,}")
        m[2].metric("Blank skipped", f"{parsed.blanks:,}")
        m[3].metric("Duplicates removed", f"{parsed.duplicates:,}")
        m[4].metric("Batches", f"{math.ceil(len(parsed.rows) / batch_size) if parsed.rows else 0:,}")
        if parsed.header_used:
            st.info(f"Header detected · code column **{parsed.code_column}**"
                    + (f" · per-row overrides: {', '.join(f'`{k}`→{v}' for k, v in parsed.override_columns.items())}"
                       if parsed.override_columns else " · no override columns"))
        if parsed.bad_values:
            st.warning(f"{len(parsed.bad_values)} unparseable override value(s) ignored (template used): "
                       + "; ".join(parsed.bad_values[:10]))
        tpl, _ = current_template()
        prev = [core.build_item(tpl, r) for r in parsed.rows[:25]]
        if prev:
            pdf = pd.DataFrame(prev)
            pdf["taskLevelHierarchyERPIDs"] = pdf["taskLevelHierarchyERPIDs"].map(lambda v: ", ".join(v))
            st.markdown("**Payload preview (first 25)**")
            st.dataframe(pdf, width="stretch", hide_index=True)


# ----------------------------------------------------------- execution ---
def failed_frame(rows: list[dict], reasons: dict) -> pd.DataFrame:
    recs = []
    for r in rows:
        d = {"CUST_CODE": r["code"]}
        for k, v in (r.get("overrides") or {}).items():
            d[k] = ", ".join(v) if isinstance(v, list) else v
        d["reason"] = reasons.get(id(r), "")
        recs.append(d)
    return pd.DataFrame(recs)


def build_artifacts(run: dict) -> dict:
    """Return {name: bytes} for downloads + save to runs/ folder."""
    batches = pd.DataFrame([{k: v for k, v in b.items() if k != "rows"} for b in run["batches"]])
    fdf = run["failed_df"]
    recdf = pd.DataFrame(run["record_failures"])
    summary = pd.DataFrame([
        ("Started", run["started"]), ("Finished", run["finished"]), ("API URL", run["url"]),
        ("Focus area", run["template"].get("taskFocusAreaName")), ("Template", json.dumps(run["template"])),
        ("Tasks", run["total"]), ("OK", run["ok_codes"]), ("Failed / not confirmed", len(fdf)),
        ("Interrupted", run["interrupted"]),
    ], columns=["Field", "Value"])
    xbuf = io.BytesIO()
    with pd.ExcelWriter(xbuf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        if not batches.empty:
            b2 = batches.copy()
            b2["response"] = b2["response"].str.slice(0, 32000)
            b2.to_excel(xw, sheet_name="Batches", index=False)
        (fdf if not fdf.empty else pd.DataFrame({"CUST_CODE": []})).to_excel(xw, sheet_name="Failed", index=False)
        if not recdf.empty:
            recdf.to_excel(xw, sheet_name="Record issues", index=False)
    fbuf = io.BytesIO()
    if not fdf.empty:
        with pd.ExcelWriter(fbuf, engine="openpyxl") as xw:
            fdf.to_excel(xw, sheet_name="Failed", index=False)
    arts = {
        "run_report.xlsx": xbuf.getvalue(),
        "failed_cust_codes.csv": fdf.drop(columns=["reason"], errors="ignore").to_csv(index=False).encode("utf-8")
        if not fdf.empty else b"",
        "failed_cust_codes.xlsx": fbuf.getvalue(),
        "upload_log.txt": "\n".join(run["log"]).encode("utf-8"),
    }
    try:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(run["template"].get("taskFocusAreaName", "run")))[:40]
        kind = "retry" if run.get("label", "").startswith("Retry") else "upload"
        base = RUNS_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}_{kind}"
        d, i = base, 1
        while d.exists():
            i += 1
            d = base.with_name(f"{base.name}_{i}")
        d.mkdir(parents=True)
        for n, bts in arts.items():
            if bts:
                (d / n).write_bytes(bts)
        run["saved_to"] = str(d)
    except Exception as e:
        run["saved_to"] = f"(could not save: {e})"
    return arts


def execute(rows: list[dict], template: dict, label: str):
    cfg = core.UploadConfig(url=api_url.strip(), username=api_user, password=api_pass,
                            wrap_key=wrap_key.strip() or None, batch_size=int(batch_size),
                            workers=int(workers), timeout=int(timeout), retries=int(retries),
                            backoff=float(backoff))
    uploader = core.Uploader(cfg)
    n_batches = math.ceil(len(rows) / cfg.batch_size)
    log: list[str] = []

    def L(msg, lvl="INFO"):
        log.append(f"{datetime.now():%Y-%m-%d %H:%M:%S} {lvl:<5} {msg}")

    L(f"{label}: {len(rows)} tasks · {n_batches} batches · batch={cfg.batch_size} · workers={cfg.workers} · "
      f"retries={cfg.retries} · url={cfg.url}")
    L(f"Template: {json.dumps(template, ensure_ascii=False)}")

    st.button("⏹ Stop (queued batches are skipped)", key="stop_btn")
    prog = st.progress(0.0, text=f"0 / {n_batches} batches")
    mc = st.columns(4)
    ph = [c.empty() for c in mc]
    log_ph = st.empty()

    results, rec_fail = [], []
    bad_ids: set = set()
    ok_codes = fail_codes = 0
    t0 = time.time()
    started = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    interrupted = True
    gen = uploader.run(rows, template)
    try:
        for res in gen:
            summ = core.summarise_response(res["response"])
            res.update({"codes": len(res["rows"]), "message": summ["message"],
                        "counts": json.dumps(summ["counts"]) if summ["counts"] else "",
                        "record_failures": len(summ["record_failures"])})
            rec_fail += [{"batch": res["batch"], **x} for x in summ["record_failures"]]
            bad_ids.update(summ["invalid_hierarchy_ids"])
            results.append(res)
            if res["status"] == "OK":
                ok_codes += res["codes"]
                L(f"Batch {res['batch']}/{n_batches} OK · http={res['http']} · {res['codes']} codes · "
                  f"attempts={res['attempts']} · {res['seconds']}s · {summ['counts'] or summ['message'][:120]}")
            else:
                fail_codes += res["codes"]
                L(f"Batch {res['batch']}/{n_batches} FAILED · http={res['http']} · attempts={res['attempts']} · "
                  f"resp={str(res['response'])[:250]}", "WARN")
            done = len(results)
            prog.progress(done / n_batches, text=f"{done} / {n_batches} batches · {time.time() - t0:.0f}s")
            ph[0].metric("Batches done", f"{done}/{n_batches}")
            ph[1].metric("Codes OK", f"{ok_codes:,}")
            ph[2].metric("Codes failed", f"{fail_codes:,}")
            ph[3].metric("Elapsed", f"{time.time() - t0:.0f}s")
            log_ph.code("\n".join(log[-200:]), language="log")
        interrupted = False
    finally:
        uploader.cancel.set()
        gen.close()
        done_ids = {id(r) for b in results for r in b["rows"]}
        reasons = {}
        failed_rows = []
        for b in results:
            if b["status"] != "OK":
                for r in b["rows"]:
                    reasons[id(r)] = f"batch {b['batch']}: {b['status']} http={b['http']}"
                    failed_rows.append(r)
        pending = [r for r in rows if id(r) not in done_ids]
        for r in pending:
            reasons[id(r)] = "NOT_CONFIRMED (run stopped)"
        failed_rows += pending
        if interrupted:
            L(f"Run stopped · {len(pending)} tasks not confirmed", "WARN")
        L(f"Done in {time.time() - t0:.1f}s · OK={ok_codes} · failed/unconfirmed={len(failed_rows)}")
        run = {"label": label, "started": started, "finished": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "url": cfg.url,
               "template": template, "total": len(rows), "ok_codes": ok_codes, "batches":
               sorted(results, key=lambda b: b["batch"]), "failed_rows": failed_rows,
               "failed_df": failed_frame(failed_rows, reasons), "record_failures": rec_fail, "invalid_hierarchy_ids": sorted(bad_ids),
               "log": log, "interrupted": interrupted, "seconds": round(time.time() - t0, 1)}
        run["artifacts"] = build_artifacts(run)
        ss.last_run = run
    st.rerun()


# -------------------------------------------------------------- upload ---
with tab_u:
    tpl, tpl_errs = current_template()
    rows = parsed.rows if parsed else []
    a, b, c, d = st.columns(4)
    a.metric("Tasks ready", f"{len(rows):,}")
    b.metric("Focus area", tpl.get("taskFocusAreaName") or "—")
    c.metric("Target / ERP IDs", f"{tpl['totalTarget']} · {', '.join(tpl['taskLevelHierarchyERPIDs'])}")
    d.metric("Host", host)

    limit = st.number_input("Send only the first N tasks (0 = all) — use 1–5 for a smoke test", 0, None, 0)
    send_rows = rows[:limit] if limit else rows

    blockers = list(tpl_errs)
    if not rows:
        blockers.append("No input codes — add them in ② Input codes.")
    if not api_pass:
        blockers.append("Enter the API password in the sidebar.")
    for x in blockers:
        st.warning(x)

    confirm = st.checkbox(f"I confirm pushing **{len(send_rows):,}** task(s) "
                          f"for **{tpl.get('taskFocusAreaName')}** to **{host}**")
    b1, b2, b3 = st.columns(3)
    if b1.button("🧪 Dry run (build payload, no POST)", width="stretch", disabled=not rows or bool(tpl_errs)):
        payload = [core.build_item(tpl, r) for r in send_rows]
        body = payload if not wrap_key.strip() else {wrap_key.strip(): payload}
        n_b = math.ceil(len(payload) / int(batch_size))
        st.info(f"Dry run OK · {len(payload):,} tasks → {n_b:,} batch(es) of ≤{int(batch_size)} · nothing sent.")
        st.download_button("⬇ Download full payload JSON", json.dumps(body, indent=2, ensure_ascii=False),
                           file_name=f"payload_{re.sub(r'[^A-Za-z0-9]+', '_', tpl['taskFocusAreaName'])}.json",
                           mime="application/json")
    start = b2.button("🚀 Start upload", type="primary", width="stretch",
                      disabled=bool(blockers) or not confirm)
    lr = ss.last_run
    retry = False
    if lr and lr["failed_rows"]:
        retry = b3.button(f"🔁 Retry {len(lr['failed_rows']):,} failed (current template)", width="stretch",
                          disabled=not api_pass)

    if start:
        execute(send_rows, tpl, "Upload")
    elif retry:
        execute(lr["failed_rows"], tpl, "Retry failed")

    # ------------------------------------------------------ results ---
    if lr:
        st.divider()
        st.subheader("Last run")
        bad = lr.get("invalid_hierarchy_ids") or []
        if bad:
            cur = core.split_ids(ss.t_erp)
            still = [x for x in bad if x in cur]
            st.error(f"API rejected **{len(bad)} TaskLevelHierarchyERPID(s)** as invalid: `{', '.join(bad)}` — "
                     "every task carrying them failed. Check these codes in the Colpal hierarchy master.")
            if still:
                def _drop_bad(ids=tuple(still)):
                    ss.t_erp = ", ".join(x for x in core.split_ids(ss.t_erp) if x not in ids)
                st.button(f"🧹 Remove {len(still)} invalid ID(s) from template", on_click=_drop_bad)
            else:
                st.success("Invalid IDs are already removed from the current template → hit Retry (current template).")
        n_fail = len(lr["failed_df"])
        if lr["interrupted"]:
            st.warning(f"Run was stopped. {n_fail:,} task(s) failed or not confirmed.")
        elif n_fail:
            st.error(f"{n_fail:,} of {lr['total']:,} task(s) failed. Download the failed codes or hit Retry.")
        else:
            st.success(f"🎉 All {lr['total']:,} task(s) uploaded successfully.")
        m = st.columns(5)
        m[0].metric("Tasks", f"{lr['total']:,}")
        m[1].metric("OK", f"{lr['ok_codes']:,}")
        m[2].metric("Failed", f"{n_fail:,}")
        m[3].metric("Batches", f"{len(lr['batches']):,}")
        m[4].metric("Duration", f"{lr['seconds']}s")
        st.caption(f"{lr['template'].get('taskFocusAreaName')} · {lr['started']} → {lr['finished']} · "
                   f"saved to `{lr.get('saved_to', '')}`")

        arts = lr["artifacts"]
        d = st.columns(4)
        d[0].download_button("⬇ Run report (.xlsx)", arts["run_report.xlsx"], "run_report.xlsx",
                             width="stretch")
        d[1].download_button("⬇ Failed codes (.csv)", arts["failed_cust_codes.csv"], "failed_cust_codes.csv",
                             disabled=not n_fail, width="stretch")
        d[2].download_button("⬇ Failed codes (.xlsx)", arts["failed_cust_codes.xlsx"] or b"",
                             "failed_cust_codes.xlsx", disabled=not n_fail, width="stretch")
        d[3].download_button("⬇ Log (.txt)", arts["upload_log.txt"], "upload_log.txt", width="stretch")

        if lr["record_failures"]:
            st.markdown(f"**Record-level issues reported by API ({len(lr['record_failures']):,})** "
                        "— batch was accepted, but these records were flagged")
            st.dataframe(pd.DataFrame(lr["record_failures"]), width="stretch", hide_index=True)
        if lr["batches"]:
            st.markdown("**Batches**")
            bdf = pd.DataFrame([{k: b[k] for k in ("batch", "status", "http", "codes", "attempts", "seconds",
                                                    "counts", "message", "record_failures")}
                                for b in lr["batches"]])
            bdf["http"] = bdf["http"].astype(str)
            st.dataframe(bdf, width="stretch", hide_index=True)
            with st.expander("Raw API responses"):
                for b in lr["batches"]:
                    st.markdown(f"Batch {b['batch']} · {b['status']} · http {b['http']}")
                    st.code(str(b["response"])[:3000] or "(empty)", language="json")
        with st.expander("Log"):
            st.code("\n".join(lr["log"][-500:]), language="log")
