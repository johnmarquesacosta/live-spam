"""
StreamDeck - Web Server (Flask)
Rodar com: python app.py
Dashboard em: http://localhost:5000
"""

import os
import sys
import atexit
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from stream_manager import StreamManager, StreamStatus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates"),
)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10 GB upload max

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
manager = StreamManager(upload_dir=UPLOAD_DIR)

# Graceful shutdown — stop all FFmpeg processes
atexit.register(manager.shutdown_all)

ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts", ".m4v"}


def allowed_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTENSIONS


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return send_from_directory(app.template_folder, "dashboard.html")


# ─── System ───────────────────────────────────────────────────────────────────

@app.route("/api/system", methods=["GET"])
def system_info():
    try:
        return jsonify(manager.get_system_info())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Streams ──────────────────────────────────────────────────────────────────

@app.route("/api/streams", methods=["GET"])
def list_streams():
    return jsonify(manager.list_streams())


@app.route("/api/streams", methods=["POST"])
def create_stream():
    try:
        data = request.json
        required = ["name", "rtmp_url", "stream_key", "files"]
        for field in required:
            if field not in data:
                return jsonify({"error": f"Campo obrigatório: {field}"}), 400

        # Resolve caminhos dos arquivos
        resolved_files = []
        for fn in data["files"]:
            fp = os.path.join(UPLOAD_DIR, fn)
            if not os.path.exists(fp):
                return jsonify({"error": f"Arquivo não encontrado: {fn}"}), 400
            resolved_files.append(fp)

        stream = manager.create_stream(
            name=data["name"],
            rtmp_url=data["rtmp_url"],
            stream_key=data["stream_key"],
            files=resolved_files,
            mode=data.get("mode", "loop"),
            encoder=data.get("encoder", "cpu"),
            quality=data.get("quality", "medium"),
            resolution=data.get("resolution", "1280x720"),
            fps=int(data.get("fps", 30)),
        )

        # Auto-start se solicitado
        if data.get("auto_start", False):
            stream.start()

        return jsonify(stream.to_dict()), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/streams/<stream_id>", methods=["GET"])
def get_stream(stream_id):
    stream = manager.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream não encontrado"}), 404
    return jsonify(stream.to_dict())


@app.route("/api/streams/<stream_id>/start", methods=["POST"])
def start_stream(stream_id):
    stream = manager.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream não encontrado"}), 404
    ok, msg = stream.start()
    return jsonify({"ok": ok, "message": msg, "status": stream.status})


@app.route("/api/streams/<stream_id>/stop", methods=["POST"])
def stop_stream(stream_id):
    stream = manager.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream não encontrado"}), 404
    ok, msg = stream.stop()
    return jsonify({"ok": ok, "message": msg, "status": stream.status})


@app.route("/api/streams/<stream_id>/restart", methods=["POST"])
def restart_stream(stream_id):
    stream = manager.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream não encontrado"}), 404
    ok, msg = stream.restart()
    return jsonify({"ok": ok, "message": msg, "status": stream.status})


@app.route("/api/streams/<stream_id>", methods=["DELETE"])
def delete_stream(stream_id):
    ok = manager.delete_stream(stream_id)
    if not ok:
        return jsonify({"error": "Stream não encontrado"}), 404
    return jsonify({"ok": True})


@app.route("/api/streams/<stream_id>/logs", methods=["GET"])
def stream_logs(stream_id):
    stream = manager.get_stream(stream_id)
    if not stream:
        return jsonify({"error": "Stream não encontrado"}), 404
    return jsonify({"logs": stream.logs})


# ─── Arquivos ─────────────────────────────────────────────────────────────────

@app.route("/api/files", methods=["GET"])
def list_files():
    return jsonify(manager._list_files())


@app.route("/api/files/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Nome de arquivo inválido"}), 400
    if not allowed_file(f.filename):
        return jsonify({"error": f"Extensão não permitida. Use: {ALLOWED_EXTENSIONS}"}), 400

    filename = secure_filename(f.filename)
    dest = os.path.join(UPLOAD_DIR, filename)
    f.save(dest)
    size = os.path.getsize(dest)
    return jsonify({"name": filename, "size_mb": round(size / 1024 / 1024, 2)}), 201


@app.route("/api/files/<filename>", methods=["DELETE"])
def delete_file(filename):
    fp = os.path.join(UPLOAD_DIR, secure_filename(filename))
    if not os.path.exists(fp):
        return jsonify({"error": "Arquivo não encontrado"}), 404
    os.remove(fp)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("=" * 60)
    print("  StreamDeck - Gerenciador de Transmissões FFmpeg")
    print("  Dashboard: http://localhost:5050")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)