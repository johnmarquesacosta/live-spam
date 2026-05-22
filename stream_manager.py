"""
StreamDeck - FFmpeg Stream Manager
Supports: NVIDIA NVENC, Apple Silicon VideoToolbox, CPU fallback
"""

import subprocess
import threading
import time
import uuid
import os
import json
import platform
import shutil
import logging
import signal
import atexit
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("StreamDeck")


class EncoderType(str, Enum):
    NVENC = "nvenc"           # NVIDIA GPU (RTX 5070 Ti)
    VIDEOTOOLBOX = "videotoolbox"  # Apple Silicon
    CPU = "cpu"               # libx264 fallback


class StreamMode(str, Enum):
    LOOP = "loop"             # Um arquivo em loop infinito
    PLAYLIST = "playlist"     # Playlist sequencial
    ONCE = "once"             # Transmitir uma vez e parar


class StreamStatus(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


def detect_available_encoders() -> List[str]:
    """Detecta quais encoders estão disponíveis no sistema."""
    available = []
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders", "-v", "quiet"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout
        if "h264_nvenc" in output:
            available.append(EncoderType.NVENC)
        if "h264_videotoolbox" in output:
            available.append(EncoderType.VIDEOTOOLBOX)
        available.append(EncoderType.CPU)  # sempre disponível
    except Exception as e:
        logger.warning(f"Falha ao detectar encoders: {e}")
        available.append(EncoderType.CPU)
    return available


def get_encoder_flags(encoder: EncoderType, quality: str = "medium") -> List[str]:
    """Retorna as flags FFmpeg para o encoder escolhido."""
    quality_map = {
        "low":    {"nvenc": "p1", "cpu_crf": "28", "vb": "2500k"},
        "medium": {"nvenc": "p4", "cpu_crf": "23", "vb": "4500k"},
        "high":   {"nvenc": "p7", "cpu_crf": "18", "vb": "8000k"},
    }
    q = quality_map.get(quality, quality_map["medium"])

    if encoder == EncoderType.NVENC:
        return [
            "-c:v", "h264_nvenc",
            "-preset", q["nvenc"],
            "-b:v", q["vb"],
            "-maxrate", q["vb"],
            "-bufsize", str(int(q["vb"].replace("k","")) * 2) + "k",
            "-gpu", "0",
            "-rc", "cbr",
        ]
    elif encoder == EncoderType.VIDEOTOOLBOX:
        return [
            "-c:v", "h264_videotoolbox",
            "-b:v", q["vb"],
            "-maxrate", q["vb"],
            "-bufsize", str(int(q["vb"].replace("k","")) * 2) + "k",
            "-realtime", "1",
        ]
    else:  # CPU
        return [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", q["cpu_crf"],
            "-maxrate", q["vb"],
            "-bufsize", str(int(q["vb"].replace("k","")) * 2) + "k",
            "-tune", "zerolatency",
        ]


class Stream:
    # Max repetitions for playlist loop concat files — avoids multi-GB text files
    MAX_LOOP_REPEATS = 500

    def __init__(
        self,
        stream_id: str,
        name: str,
        rtmp_url: str,
        stream_key: str,
        files: List[str],
        mode: StreamMode,
        encoder: EncoderType,
        quality: str = "medium",
        resolution: str = "1280x720",
        fps: int = 30,
    ):
        self.id = stream_id
        self.name = name
        self.rtmp_url = rtmp_url
        self.stream_key = stream_key
        self.files = files
        self.mode = mode
        self.encoder = encoder
        self.quality = quality
        self.resolution = resolution
        self.fps = fps

        self.status = StreamStatus.IDLE
        self.process: Optional[subprocess.Popen] = None
        self.thread: Optional[threading.Thread] = None
        self.started_at: Optional[datetime] = None
        self.stopped_at: Optional[datetime] = None
        self.error_msg: Optional[str] = None
        self.logs: List[str] = []
        self.current_file_index: int = 0
        self._stop_event = threading.Event()
        self._concat_file: Optional[str] = None

    def _log(self, msg: str):
        entry = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]
        logger.info(f"[{self.name}] {msg}")

    def _cleanup_concat_file(self):
        """Remove arquivo concat temporário se existir."""
        if self._concat_file and os.path.exists(self._concat_file):
            try:
                os.remove(self._concat_file)
                self._log(f"Arquivo temporário removido: {self._concat_file}")
            except OSError as e:
                self._log(f"Aviso: não foi possível remover temp file: {e}")
            finally:
                self._concat_file = None

    def _build_input_flags(self) -> List[str]:
        """Constrói a parte de input do comando FFmpeg."""
        if not self.files:
            raise ValueError("Nenhum arquivo fornecido")

        if self.mode == StreamMode.LOOP and len(self.files) == 1:
            # Loop de arquivo único — usa -stream_loop nativo do FFmpeg
            return ["-stream_loop", "-1", "-re", "-i", self.files[0]]

        elif self.mode in (StreamMode.PLAYLIST, StreamMode.LOOP):
            # Cria concat list temporária
            self._concat_file = os.path.join(
                os.path.dirname(self.files[0]) if self.files else "/tmp",
                f".playlist_{self.id}.txt"
            )
            with open(self._concat_file, "w") as f:
                files_to_write = self.files
                if self.mode == StreamMode.LOOP:
                    # Repete a playlist um número razoável de vezes
                    # Se acabar, o _run loop vai re-lançar o processo
                    files_to_write = self.files * self.MAX_LOOP_REPEATS
                for fp in files_to_write:
                    f.write(f"file '{fp}'\n")
            return ["-re", "-f", "concat", "-safe", "0", "-i", self._concat_file]

        else:  # ONCE
            return ["-re", "-i", self.files[0]]

    def _build_command(self) -> List[str]:
        """Monta o comando FFmpeg completo."""
        destination = f"{self.rtmp_url}/{self.stream_key}"
        input_flags = self._build_input_flags()
        encoder_flags = get_encoder_flags(self.encoder, self.quality)

        w, h = self.resolution.split("x")
        scale_filter = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"

        cmd = (
            ["ffmpeg", "-y", "-loglevel", "warning"]
            + input_flags
            + ["-vf", scale_filter]
            + encoder_flags
            + [
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
                "-pix_fmt", "yuv420p",
                "-r", str(self.fps),
                "-g", str(self.fps * 2),
                "-f", "flv",
                destination,
            ]
        )
        return cmd

    def _run(self):
        """Thread principal de streaming — com re-launch automático para modo loop."""
        self.status = StreamStatus.STARTING

        while not self._stop_event.is_set():
            try:
                cmd = self._build_command()
                self._log(f"Iniciando: {' '.join(cmd[:8])}...")
                self._log(f"Encoder: {self.encoder} | Qualidade: {self.quality} | Resolução: {self.resolution}")

                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self.status = StreamStatus.RUNNING
                if not self.started_at:
                    self.started_at = datetime.now()
                self._log("Transmissão iniciada ✓")

                # Lê output do FFmpeg
                for line in self.process.stdout:
                    line = line.strip()
                    if line and not self._stop_event.is_set():
                        self._log(line)

                self.process.wait()

                if self._stop_event.is_set():
                    self.status = StreamStatus.STOPPED
                    self._log("Transmissão encerrada pelo usuário.")
                    break
                elif self.process.returncode != 0:
                    self.status = StreamStatus.ERROR
                    self.error_msg = f"FFmpeg saiu com código {self.process.returncode}"
                    self._log(f"ERRO: {self.error_msg}")
                    break
                else:
                    # Processo terminou normalmente
                    if self.mode == StreamMode.LOOP:
                        # Re-launch: playlist concat acabou, reinicia
                        self._log("Playlist loop concluída, reiniciando automaticamente...")
                        self._cleanup_concat_file()
                        continue
                    else:
                        self.status = StreamStatus.STOPPED
                        self._log("Transmissão finalizada normalmente.")
                        break

            except Exception as e:
                self.status = StreamStatus.ERROR
                self.error_msg = str(e)
                self._log(f"EXCEÇÃO: {e}")
                break
            finally:
                self._cleanup_concat_file()

        self.stopped_at = datetime.now()

    def start(self):
        if self.status in (StreamStatus.RUNNING, StreamStatus.STARTING):
            return False, "Stream já está em execução"
        self._stop_event.clear()
        self.error_msg = None
        self.started_at = None
        self.stopped_at = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return True, "Stream iniciado"

    def stop(self):
        self._stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.status = StreamStatus.STOPPED
        self._cleanup_concat_file()
        return True, "Stream encerrado"

    def restart(self):
        """Para e reinicia o stream."""
        self.stop()
        # Aguarda a thread anterior terminar
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        return self.start()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "rtmp_url": self.rtmp_url,
            "stream_key": "***" + self.stream_key[-4:] if len(self.stream_key) > 4 else "****",
            "files": [os.path.basename(f) for f in self.files],
            "mode": self.mode,
            "encoder": self.encoder,
            "quality": self.quality,
            "resolution": self.resolution,
            "fps": self.fps,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
            "error_msg": self.error_msg,
            "logs": self.logs[-50:],
            "uptime_seconds": (
                int((datetime.now() - self.started_at).total_seconds())
                if self.started_at and self.status == StreamStatus.RUNNING
                else None
            ),
        }


class StreamManager:
    def __init__(self, upload_dir: str = "./uploads"):
        self.streams: Dict[str, Stream] = {}
        self.upload_dir = upload_dir
        os.makedirs(upload_dir, exist_ok=True)
        self.available_encoders = detect_available_encoders()
        logger.info(f"Encoders disponíveis: {self.available_encoders}")

    def create_stream(
        self,
        name: str,
        rtmp_url: str,
        stream_key: str,
        files: List[str],
        mode: str = "loop",
        encoder: str = "cpu",
        quality: str = "medium",
        resolution: str = "1280x720",
        fps: int = 30,
    ) -> Stream:
        stream_id = str(uuid.uuid4())[:8]
        stream = Stream(
            stream_id=stream_id,
            name=name,
            rtmp_url=rtmp_url,
            stream_key=stream_key,
            files=files,
            mode=StreamMode(mode),
            encoder=EncoderType(encoder),
            quality=quality,
            resolution=resolution,
            fps=fps,
        )
        self.streams[stream_id] = stream
        return stream

    def get_stream(self, stream_id: str) -> Optional[Stream]:
        return self.streams.get(stream_id)

    def delete_stream(self, stream_id: str) -> bool:
        stream = self.streams.get(stream_id)
        if not stream:
            return False
        if stream.status == StreamStatus.RUNNING:
            stream.stop()
        del self.streams[stream_id]
        return True

    def list_streams(self) -> List[Dict]:
        return [s.to_dict() for s in self.streams.values()]

    def shutdown_all(self):
        """Encerra todos os streams ativos — chamado no shutdown do servidor."""
        active = [s for s in self.streams.values() if s.status in (StreamStatus.RUNNING, StreamStatus.STARTING)]
        if active:
            logger.info(f"Encerrando {len(active)} stream(s) ativo(s)...")
            for stream in active:
                try:
                    stream.stop()
                except Exception as e:
                    logger.error(f"Erro ao encerrar stream {stream.name}: {e}")
            logger.info("Todos os streams foram encerrados.")

    def get_system_info(self) -> Dict:
        ffmpeg_ok = shutil.which("ffmpeg") is not None
        return {
            "platform": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "ffmpeg_available": ffmpeg_ok,
            "available_encoders": self.available_encoders,
            "upload_dir": os.path.abspath(self.upload_dir),
            "uploaded_files": self._list_files(),
        }

    def _list_files(self) -> List[Dict]:
        files = []
        if not os.path.isdir(self.upload_dir):
            return files
        for fn in os.listdir(self.upload_dir):
            fp = os.path.join(self.upload_dir, fn)
            if os.path.isfile(fp):
                size = os.path.getsize(fp)
                files.append({
                    "name": fn,
                    "path": fp,
                    "size_mb": round(size / 1024 / 1024, 2),
                })
        return files