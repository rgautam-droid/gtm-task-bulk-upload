# GTM Task Bulk Upload (local UI)

Streamlit UI over the Colab uploader for `POST /api/V3/TaskManagement/CreateUpdateTasks`.

## Run (Windows)
Double-click `run.bat`. First run creates `.venv` and installs deps; the browser opens at http://localhost:8501.
Needs Python 3.10+ (`py --version`). Optional: set env var `FA_API_PASSWORD` to prefill the password.

## Flow
1. **① Task template**: pick or edit a preset (focus area, ERP IDs, targets, hierarchy). Save new presets to `presets.json`.
2. **② Input codes**: CSV/XLSX (first column CUST_CODE, header optional) or paste codes.
   Optional per-row override columns (need a header): `totalTarget`, `achievedTarget`, `taskFocusAreaName`,
   `taskLevelHierarchyERPIDs` (use `;` between IDs), `productHierarchyLevel`, `calculationMeasure`, `taskLevelHierarchy`.
3. **③ Upload & results**: smoke test with "first N", dry run (builds JSON, no POST), confirm, then Start.
   Retry failed with one click. Every run is saved to `runs/<timestamp>_<focus>_<upload|retry>/`
   (run_report.xlsx, failed_cust_codes.csv/.xlsx, upload_log.txt).

Retry and back-off rules match the Colab script: 5xx, exceptions, and 200 responses with a retryable body are retried; 4xx fails immediately.
A failed batch marks all of its codes as failed, so use a smaller batch size to isolate bad codes.

## Deploy to Streamlit Community Cloud
1. Create a GitHub repo (e.g. `gtm-task-bulk-upload`) and upload all files in this folder, including `.streamlit/`.
   Leave out `runs/`, `.venv/`, and `test/`.
2. Go to https://share.streamlit.io, click **Create app**, pick the repo, branch `main`, main file `app.py`.
3. Under **Advanced settings**:
   - Python version: **3.12**
   - Secrets (optional): `APP_PASSCODE = "choose-a-passcode"`. With this set, the app asks for the passcode before it opens.
   - Do **not** add `FA_API_PASSWORD` for a public app. Anyone with the link could push tasks.
4. Deploy. You get a `https://<name>.streamlit.app` URL.

Cloud notes: presets you save and the `runs/` folder are lost when the app restarts, so use the download buttons
and add permanent presets to `presets.json` in the repo. If the Colpal API only accepts requests from certain IP
addresses, calls from Streamlit Cloud will fail. Test with "first N = 2".
