"""Local web bridge for the stylegrid studio.

Run from the repository root:
    python app/server.py
Then open http://127.0.0.1:8765/ (the UI also works as a standalone file).

No network service is contacted. Uploaded images and generated outputs stay in out/web/.
"""
from __future__ import annotations

import json
import secrets
import sys
import zipfile
from email.parser import BytesParser
from email.policy import default
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
OUT = ROOT / "out" / "web"
MAX_UPLOAD = 20 * 1024 * 1024

sys.path.insert(0, str(ROOT))
import gridkit as G  # noqa: E402
import mixposter as MP  # noqa: E402
import workflow  # noqa: E402

PRESET_PROFILES = {
    "深海蓝调": "照片保真",
    "颗粒纪实": "印刷",
    "银盐夜色": "织品",
    "炽热信号": "抽象",
    "自定义": "全都要",
}
MIX_DIRECTIONS = {
    "潮汐冷光": "lgt_mosaic_glitch",
    "月蚀单色": "print_cross",
    "霓虹余温": "neon_foil",
    "沙丘低饱和": "leak_riso",
}


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    # Serve from the repository so /app/index.html can resolve ../samples and /out/web.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[stylegrid] " + fmt % args)

    def send_json(self, status: int, payload: object):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/app/")
            self.end_headers()
            return
        if path == "/api/catalog":
            self.send_json(200, {
                "styles": [{"key": key, "name": G.NAMES.get(key, key)} for key in sorted(G.STYLES)],
                "mixes": sorted(MP.MIXES),
                "profiles": sorted(workflow.PROFILES),
                "formats": ["V", "S", "H"],
                "local_only": True,
            })
            return
        if path == "/api/recent":
            jobs = []
            if OUT.is_dir():
                dirs = [d for d in OUT.iterdir() if d.is_dir()]
                for job_dir in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)[:6]:
                    assets = {f.name: f"/out/web/{job_dir.name}/{f.name}" for f in sorted(job_dir.glob("*.png"))}
                    cover = assets.get("01_九宫格.png") or (next(iter(assets.values())) if assets else None)
                    if cover:
                        jobs.append({
                            "job": job_dir.name,
                            "cover": cover,
                            "mtime": job_dir.stat().st_mtime,
                            "assets": assets,
                        })
            self.send_json(200, {"jobs": jobs})
            return
        if path == "/api/download":
            job = parse_qs(urlparse(self.path).query).get("job", [""])[0]
            if not job or not job.isalnum() or len(job) > 32:
                self.send_json(400, {"error": "无效作品编号"})
                return
            job_dir = (OUT / job).resolve()
            if job_dir.parent != OUT.resolve() or not job_dir.is_dir():
                self.send_json(404, {"error": "作品不存在"})
                return
            archive = job_dir / f"stylegrid_{job}.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for file in sorted(job_dir.glob("*.png")):
                    bundle.write(file, file.name)
                report = job_dir / "报告.md"
                if report.exists():
                    bundle.write(report, report.name)
            body = archive.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename=stylegrid_{job}.zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/generate":
            self.send_json(404, {"error": "not found"})
            return
        try:
            result = self.generate_request()
            self.send_json(200, result)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:  # keep server alive and expose a useful local error
            self.send_json(500, {"error": f"生成失败：{type(exc).__name__}: {exc}"})
    def generate_request(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("请以 multipart/form-data 上传图片")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("上传长度无效") from exc
        if length > MAX_UPLOAD + 2 * 1024 * 1024:
            raise ValueError("上传请求过大")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + raw
        )
        fields = {}
        upload_name = ""
        payload = b""
        for part in message.iter_parts():
            disposition = dict(part.get_params(header="content-disposition", failobj=[]))
            name = disposition.get("name")
            if name == "file":
                upload_name = disposition.get("filename", "")
                payload = part.get_payload(decode=True) or b""
            elif name:
                fields[name] = part.get_content()
        if not upload_name:
            raise ValueError("缺少图片文件")
        if len(payload) > MAX_UPLOAD:
            raise ValueError("图片超过 20 MB")
        suffix = Path(upload_name).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("仅支持 JPG、PNG、WEBP")
        preset = fields.get("preset", "深海蓝调")
        profile_name = PRESET_PROFILES.get(preset, "照片保真")
        profile = workflow.PROFILES[profile_name]
        formats = list(dict.fromkeys(f for f in fields.get("formats", "V,S,H").split(",") if f in {"V", "S", "H"}))
        if not formats:
            raise ValueError("至少选择一种输出格式")
        try:
            density = max(1, min(3, int(fields.get("density", "2"))))
        except ValueError:
            density = 2
        tile = {1: 280, 2: 300, 3: 320}[density]
        title = fields.get("title", "作品")[:40]
        sub = fields.get("sub", "")[:60]
        selected_mix = MIX_DIRECTIONS.get(fields.get("blend", ""))
        mixes = [x for x in ([selected_mix] if selected_mix else []) + profile.get("mixes", "").split(",") if x]
        mixes = list(dict.fromkeys(mixes))[:8]
        custom_styles = [s for s in fields.get("styles", "").split(",") if s in G.STYLES][:8]
        styles = custom_styles or profile.get("styles", "auto").split(",")
        job = secrets.token_hex(5)
        job_dir = OUT / job
        job_dir.mkdir(parents=True, exist_ok=True)
        src = job_dir / ("source" + suffix)
        src.write_bytes(payload)
        report = workflow.run(src, job_dir, styles=styles, mixes=mixes,
                              posters=formats, aspect="1:1", size=900, tile=tile,
                              title=title, sub=sub, poster_style=None, verify=True)
        files = report.get("files", [])
        failed_steps = [name for name, status in report.get("steps", []) if status != "ok"]
        if failed_steps or not files:
            detail = "、".join(failed_steps) if failed_steps else "无产物"
            raise RuntimeError(f"工作流未完成：{detail}")
        base = f"/out/web/{job}/"
        return {
            "job": job,
            "local_only": True,
            "files": files,
            "assets": {name: base + name for name in files},
            "download": f"/api/download?job={job}",
            "output_count": len(files),
            "qa_count": len(report.get("scores", {})),
            "scores": report.get("scores", {}),
            "plate": report.get("plate", {}),
            "styles": report.get("styles", []),
            "mixes": report.get("mixes", []),
        }

def main():
    # Windows 上 SO_REUSEADDR 允许其他进程劫持同端口;端口可换,默认 8765。
    port = 8765
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            raise SystemExit("用法: python app/server.py [--port 8765]")
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"stylegrid studio → http://127.0.0.1:{port}/ (local only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
