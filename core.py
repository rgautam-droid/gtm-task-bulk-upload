"""Core logic for FieldAssist V3 TaskManagement bulk upload (UI-agnostic).

Mirrors the Colab uploader: read codes -> build payload from template ->
batch -> concurrent POST with retry/back-off -> collect failures.
"""
from __future__ import annotations

import copy
import io
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

DEFAULT_URL = "https://colpal-external-api.fieldassist.io/api/V3/TaskManagement/CreateUpdateTasks"

# Template fields and their types
INT_FIELDS = ["productHierarchyLevel", "calculationMeasure", "taskLevelHierarchy"]
NUM_FIELDS = ["totalTarget", "achievedTarget"]
LIST_FIELDS = ["taskLevelHierarchyERPIDs"]
STR_FIELDS = ["taskFocusAreaName"]

# Column-name aliases (normalised: lowercase, alphanumerics only)
CODE_ALIASES = {
    "custcode", "customercode", "taskentityerpid", "entityerpid", "outletcode",
    "outleterpid", "retailercode", "storecode", "erpid", "code",
}
OVERRIDE_ALIASES = {
    "totaltarget": "totalTarget", "target": "totalTarget",
    "achievedtarget": "achievedTarget", "achieved": "achievedTarget",
    "taskfocusareaname": "taskFocusAreaName", "focusarea": "taskFocusAreaName",
    "focusareaname": "taskFocusAreaName",
    "tasklevelhierarchyerpids": "taskLevelHierarchyERPIDs", "hierarchyerpids": "taskLevelHierarchyERPIDs",
    "erpids": "taskLevelHierarchyERPIDs",
    "producthierarchylevel": "productHierarchyLevel",
    "calculationmeasure": "calculationMeasure",
    "tasklevelhierarchy": "taskLevelHierarchy",
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _clean_code(v) -> str:
    s = "" if v is None else str(v).strip().strip('"').strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    if re.fullmatch(r"\d+\.0", s):  # Excel numeric 12345.0 -> 12345
        s = s[:-2]
    return s


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def split_ids(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,;|]", str(v or "")) if x.strip()]


def coerce_field(fieldname: str, v):
    if fieldname in INT_FIELDS:
        return int(float(v))
    if fieldname in NUM_FIELDS:
        return _num(v)
    if fieldname in LIST_FIELDS:
        return split_ids(v)
    return str(v).strip()


# ----------------------------------------------------------------- input ---
@dataclass
class ParsedInput:
    rows: list[dict]                      # [{"code": str, "overrides": {field: value}}]
    header_used: bool
    code_column: str | None
    override_columns: dict[str, str]      # source col -> payload field
    total_rows: int = 0
    blanks: int = 0
    duplicates: int = 0
    bad_values: list[str] = field(default_factory=list)


def load_table(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(io.BytesIO(data), header=None, dtype=str)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        sep = next((d for d in ("\t", ";", "|", ",") if d in first), ",")
        df = pd.read_csv(io.StringIO(text), header=None, dtype=str, keep_default_na=False,
                         skip_blank_lines=False, sep=sep, engine="python", quotechar='"')
    return df.fillna("")


def detect_header(df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    return any(_norm(v) in CODE_ALIASES or _norm(v) in OVERRIDE_ALIASES for v in df.iloc[0].tolist())


def parse_table(df: pd.DataFrame, header: bool | None = None, code_col: str | None = None) -> ParsedInput:
    """header=None -> auto-detect. code_col: header name to use as code column."""
    if header is None:
        header = detect_header(df)

    override_cols: dict[str, str] = {}
    if header and not df.empty:
        cols = [str(c).strip() for c in df.iloc[0].tolist()]
        body = df.iloc[1:].reset_index(drop=True)
        body.columns = range(len(cols))
        if code_col is None or code_col not in cols:
            code_col = next((c for c in cols if _norm(c) in CODE_ALIASES), cols[0])
        code_idx = cols.index(code_col)
        for i, c in enumerate(cols):
            if i != code_idx and _norm(c) in OVERRIDE_ALIASES:
                override_cols[c] = OVERRIDE_ALIASES[_norm(c)]
        col_idx = {c: i for i, c in enumerate(cols)}
    else:
        body, code_idx, code_col, col_idx = df, 0, None, {}

    out = ParsedInput(rows=[], header_used=bool(header), code_column=code_col, override_columns=override_cols)
    seen: set = set()
    for rec in body.itertuples(index=False):
        rec = list(rec)
        out.total_rows += 1
        code = _clean_code(rec[code_idx] if code_idx < len(rec) else "")
        if not code:
            out.blanks += 1
            continue
        ov = {}
        for src, fld in override_cols.items():
            raw = _clean_code(rec[col_idx[src]])
            if raw == "":
                continue
            try:
                ov[fld] = coerce_field(fld, raw)
            except (ValueError, TypeError):
                out.bad_values.append(f"{code}: {src}='{raw}'")
        key = (code, ov.get("taskFocusAreaName"))
        if key in seen:
            out.duplicates += 1
            continue
        seen.add(key)
        out.rows.append({"code": code, "overrides": ov})
    return out


def parse_pasted(text: str) -> ParsedInput:
    lines = [ln for ln in text.splitlines()]
    df = pd.DataFrame({0: [re.split(r"[,\t;]", ln)[0] for ln in lines]}, dtype=str)
    return parse_table(df, header=None)


# --------------------------------------------------------------- payload ---
def validate_template(t: dict) -> list[str]:
    errs = []
    if not str(t.get("taskFocusAreaName", "")).strip():
        errs.append("taskFocusAreaName is empty")
    if not t.get("taskLevelHierarchyERPIDs"):
        errs.append("taskLevelHierarchyERPIDs is empty")
    for f in INT_FIELDS + NUM_FIELDS:
        if not isinstance(t.get(f), (int, float)):
            errs.append(f"{f} must be numeric")
    return errs


def build_item(template: dict, row: dict) -> dict:
    item = copy.deepcopy(template)
    item.update(row.get("overrides") or {})
    item["taskEntityERPId"] = row["code"]
    return item


def chunk(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]


# ---------------------------------------------------------------- upload ---
def body_retryable(text: str) -> bool:
    try:
        j = json.loads(text)
    except Exception:
        return False
    if not isinstance(j, dict):
        return False
    if "error occurred while saving data" in str(j.get("message", "")).lower():
        return True
    cnt = j.get("responseStatusCount") or {}
    total = cnt.get("total") if isinstance(cnt, dict) else None
    return j.get("responseStatus") in (0, "0") and total in (0, "0", None)


_INVALID_IDS_RE = re.compile(r"Invalid\s+TaskLevelHierarchyERPIDs\s*:\s*([A-Za-z0-9_\-, ]+)", re.I)


def summarise_response(text: str) -> dict:
    """Best-effort extraction of message / counts / record-level failures / invalid hierarchy IDs."""
    out = {"message": "", "counts": {}, "record_failures": [], "invalid_hierarchy_ids": []}
    try:
        j = json.loads(text)
    except Exception:
        out["message"] = (text or "")[:300]
        return out
    if not isinstance(j, dict):
        return out
    out["message"] = str(j.get("message", ""))[:300]
    cnt = j.get("responseStatusCount")
    if isinstance(cnt, dict):
        out["counts"] = cnt
    bad_ids: set[str] = set()
    for k, v in j.items():  # e.g. responseList / ResponseList
        if isinstance(v, list) and "list" in k.lower():
            for it in v:
                if not isinstance(it, dict):
                    continue
                low = {kk.lower(): vv for kk, vv in it.items()}
                st = str(low.get("responsestatus", low.get("status", ""))).lower()
                failed = low.get("success") is False or any(w in st for w in ("fail", "error", "invalid"))
                if not failed:
                    continue
                ent = {kk.lower(): vv for kk, vv in (low.get("entities") or {}).items()} \
                    if isinstance(low.get("entities"), dict) else {}
                rid = next((low[x] for x in ("erpid", "taskentityerpid", "entityerpid", "id") if low.get(x)),
                           ent.get("outleterpid") or ent.get("taskentityerpid") or "")
                msg = str(low.get("message", ""))
                for m in _INVALID_IDS_RE.finditer(msg):
                    bad_ids.update(x.strip() for x in m.group(1).split(",") if x.strip())
                out["record_failures"].append({"code": str(rid), "status": st or "failed", "message": msg[:300]})
    out["invalid_hierarchy_ids"] = sorted(bad_ids)
    return out


@dataclass
class UploadConfig:
    url: str = DEFAULT_URL
    username: str = "Internal"
    password: str = ""
    wrap_key: str | None = None
    batch_size: int = 1000
    workers: int = 8
    timeout: int = 60
    retries: int = 3
    backoff: float = 1.5
    jitter: float = 0.25


class Uploader:
    def __init__(self, cfg: UploadConfig):
        self.cfg = cfg
        self._tl = threading.local()
        self.cancel = threading.Event()

    def _session(self):
        s = getattr(self._tl, "s", None)
        if s is None:
            s = requests.Session()
            s.auth = HTTPBasicAuth(self.cfg.username, self.cfg.password)
            s.headers.update({"User-Agent": "gtm-task-bulk-upload/1.0", "Content-Type": "application/json"})
            ad = HTTPAdapter(pool_connections=self.cfg.workers, pool_maxsize=self.cfg.workers * 2, max_retries=0)
            s.mount("https://", ad)
            s.mount("http://", ad)
            self._tl.s = s
        return s

    def _sleep(self, tries):
        time.sleep((self.cfg.backoff ** tries) + random.uniform(0, self.cfg.jitter))

    def post(self, payload: list[dict]):
        body = payload if not self.cfg.wrap_key else {self.cfg.wrap_key: payload}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        tries = 0
        while True:
            try:
                r = self._session().post(self.cfg.url, data=data, timeout=self.cfg.timeout)
                if r.status_code == 200 and body_retryable(r.text):
                    tries += 1
                    if tries > self.cfg.retries:
                        return "RETRY_EXHAUSTED", r.text, tries
                    self._sleep(tries)
                    continue
                if r.status_code >= 500:
                    tries += 1
                    if tries > self.cfg.retries:
                        return r.status_code, r.text, tries
                    self._sleep(tries)
                    continue
                return r.status_code, r.text, tries + 1
            except Exception as e:  # network / timeout
                tries += 1
                if tries > self.cfg.retries:
                    return "ERROR", str(e), tries
                self._sleep(tries)

    def _work(self, idx: int, rows: list[dict], template: dict):
        t0 = time.time()
        if self.cancel.is_set():
            return {"batch": idx, "rows": rows, "status": "SKIPPED", "http": "", "attempts": 0,
                    "seconds": 0.0, "response": "Cancelled before send"}
        payload = [build_item(template, r) for r in rows]
        http, text, attempts = self.post(payload)
        failed = http in ("ERROR", "RETRY_EXHAUSTED") or (isinstance(http, int) and http >= 400)
        return {"batch": idx, "rows": rows, "status": "FAILED" if failed else "OK", "http": http,
                "attempts": attempts, "seconds": round(time.time() - t0, 2), "response": text or ""}

    def run(self, rows: list[dict], template: dict):
        """Generator yielding one result dict per batch as they complete."""
        batches = chunk(rows, self.cfg.batch_size)
        ex = ThreadPoolExecutor(max_workers=self.cfg.workers)
        try:
            futs = [ex.submit(self._work, i + 1, b, template) for i, b in enumerate(batches)]
            for f in as_completed(futs):
                yield f.result()
        finally:
            # on stop/interrupt: drop queued batches, let in-flight ones finish
            self.cancel.set()
            ex.shutdown(wait=True, cancel_futures=True)
