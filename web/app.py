"""
واجهة ويب محلية لتشغيل pdfcrush.py (بدون تعديله).

التشغيل:  python app.py   ثم افتح  http://127.0.0.1:8000
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import json
import queue
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
SCRIPT = Path(os.environ.get("PDFCRUSH_SCRIPT", BASE.parent / "pdfcrush.py")).resolve()
JOBS_DIR = Path(os.environ.get("PDFCRUSH_JOBS", BASE / "jobs")).resolve()

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "18000"))
MAX_UPLOAD = 60 * 1024 * 1024        # أقصى حجم رفع (بايت)
JOB_TIMEOUT = 20 * 60                # أقصى مدة معالجة (ثوان)
MAX_PENDING = 10                     # أقصى عدد مهام تنتظر/تعمل
KEEP_SECONDS = 24 * 3600             # مدة الاحتفاظ بالمهام القديمة
WORKERS = max(1, (os.cpu_count() or 2) - 1)   # خيار -j الموجود أصلاً في السكربت

ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,32}$")

MSG_WARN = ("تعذر الوصول إلى حجم 499KB بعد استنفاد محاولات الضغط. "
            "تم تجهيز أفضل نتيجة متاحة، ويمكنك تحميل الملف ومراجعة حجمه وجودته.")
MSG_ERR = "تعذر ضغط الملف. تأكد أنه ملف PDF سليم وغير محمي بكلمة مرور، ثم حاول مجدداً."
MSG_TIMEOUT = "استغرقت المعالجة وقتاً أطول من المسموح. جرّب ملفاً أصغر أو أقل صفحات."
MSG_RESTART = "أُعيد تشغيل الخدمة أثناء المعالجة. حاول رفع الملف مجدداً."
MSG_BUSY = "الخدمة مشغولة حالياً. حاول بعد قليل."
MSG_NOT_PDF = "الملف المرفوع ليس ملف PDF صالحاً."
MSG_TOO_BIG = "حجم الملف أكبر من الحد المسموح (60 ميغابايت)."

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD + 1024 * 1024

_lock = threading.Lock()
_q: "queue.Queue[str]" = queue.Queue()
_active = 0   # مهام في الانتظار أو قيد المعالجة


# ------------------------------------------------------------------ helpers
def job_dir(job_id: str) -> Path:
    if not ID_RE.match(job_id or ""):
        raise ValueError("bad id")
    p = (JOBS_DIR / job_id).resolve()
    if p.parent != JOBS_DIR:
        raise ValueError("bad path")
    return p


def read_status(job_id: str):
    try:
        with open(job_dir(job_id) / "status.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_status(job_id: str, **changes):
    d = job_dir(job_id)
    path = d / "status.json"
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.update(changes)
    tmp = d / "status.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, path)       # كتابة ذرية


def safe_download_name(original: str) -> str:
    stem = Path(original.replace("\\", "/")).stem
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", stem).strip(" .")[:100]
    return (stem or "document") + "_small.pdf"


def looks_like_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"%PDF-" in f.read(1024)
    except OSError:
        return False


def kill_tree(proc: subprocess.Popen):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ------------------------------------------------------------------ worker
def run_job(job_id: str):
    d = job_dir(job_id)
    inp, out = d / "input.pdf", d / "output.pdf"
    write_status(job_id, status="compressing")

    cmd = [sys.executable, str(SCRIPT), str(inp), "-o", str(out), "-j", str(WORKERS)]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.BELOW_NORMAL_PRIORITY_CLASS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

    code, timed_out, output = None, False, ""
    try:
        proc = subprocess.Popen(cmd, cwd=str(d), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", **kwargs)
        try:
            output, _ = proc.communicate(timeout=JOB_TIMEOUT)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
            output, _ = proc.communicate()
    except Exception as e:
        output = f"failed to start: {e!r}"
    try:
        (d / "run.log").write_text(output or "", encoding="utf-8")
    except OSError:
        pass

    write_status(job_id, status="finalizing")
    out_ok = out.is_file() and out.stat().st_size > 0 and looks_like_pdf(out)

    if timed_out:
        write_status(job_id, status="error", message=MSG_TIMEOUT)
    elif code == 0 and out_ok:
        write_status(job_id, status="done", out_size=out.stat().st_size)
    elif code == 2 and out_ok:
        write_status(job_id, status="done_warning", message=MSG_WARN,
                     out_size=out.stat().st_size)
    else:
        write_status(job_id, status="error", message=MSG_ERR)
        if out.exists() and not out_ok:
            out.unlink(missing_ok=True)
    inp.unlink(missing_ok=True)      # الأصل لم يعد مطلوباً


def worker_loop():
    global _active
    while True:
        job_id = _q.get()
        try:
            run_job(job_id)
        except Exception:
            try:
                write_status(job_id, status="error", message=MSG_ERR)
            except Exception:
                pass
        finally:
            with _lock:
                _active -= 1
            _q.task_done()


def cleanup_loop():
    while True:
        try:
            now = time.time()
            for p in JOBS_DIR.iterdir():
                if p.is_dir() and ID_RE.match(p.name) and now - p.stat().st_mtime > KEEP_SECONDS:
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        time.sleep(600)


def recover_on_start():
    """مهام كانت جارية عند الإيقاف تُعلَّم فاشلة بدل أن تبقى معلّقة."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    for p in JOBS_DIR.iterdir():
        if p.is_dir() and ID_RE.match(p.name):
            st = read_status(p.name)
            if st and st.get("status") in ("queued", "compressing", "finalizing"):
                write_status(p.name, status="error", message=MSG_RESTART)


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.post("/api/jobs")
def create_job():
    global _active
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error=MSG_NOT_PDF), 400

    with _lock:
        if _active >= MAX_PENDING:
            return jsonify(error=MSG_BUSY), 429
        _active += 1

    job_id = secrets.token_urlsafe(16)
    try:
        d = job_dir(job_id)
        d.mkdir(parents=True)
        inp = d / "input.pdf"
        f.save(inp)
        if inp.stat().st_size > MAX_UPLOAD:
            raise ValueError("too big")
        if not looks_like_pdf(inp):
            shutil.rmtree(d, ignore_errors=True)
            with _lock:
                _active -= 1
            return jsonify(error=MSG_NOT_PDF), 400
        write_status(job_id, status="queued", name=safe_download_name(f.filename),
                     orig_size=inp.stat().st_size, created=time.time())
    except Exception:
        with _lock:
            _active -= 1
        try:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
        except Exception:
            pass
        return jsonify(error=MSG_TOO_BIG), 400

    _q.put(job_id)
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    try:
        st = read_status(job_id)
    except ValueError:
        st = None
    if not st:
        return jsonify(status="error", message="لم يتم العثور على المهمة."), 404
    return jsonify({k: st.get(k) for k in ("status", "message", "orig_size", "out_size")})


@app.get("/api/jobs/<job_id>/download")
def download(job_id):
    try:
        st = read_status(job_id)
        out = job_dir(job_id) / "output.pdf"
    except ValueError:
        return jsonify(error="غير موجود"), 404
    if not st or st.get("status") not in ("done", "done_warning") or not out.is_file():
        return jsonify(error="الملف غير متاح."), 404
    return send_file(out, mimetype="application/pdf", as_attachment=True,
                     download_name=st.get("name") or "document_small.pdf")


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=MSG_TOO_BIG), 413


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def main():
    if not SCRIPT.is_file():
        sys.exit(f"pdfcrush.py not found at {SCRIPT}")
    recover_on_start()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"http://{HOST}:{PORT}")
    try:
        from waitress import serve
        serve(app, host=HOST, port=PORT, threads=8,
              max_request_body_size=MAX_UPLOAD + 2 * 1024 * 1024)
    except ImportError:
        app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
