# StreamDeck - FFmpeg Stream Manager

Aplicacao web simples em Flask para gerenciar transmissoes via FFmpeg. Permite criar streams com playlist/loop, escolher encoder (CPU, NVENC, VideoToolbox) e acompanhar status e logs pelo dashboard.

## Requisitos

- Python 3.10+
- FFmpeg instalado e no PATH

## Como rodar

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Dashboard: http://localhost:5050

## API basica

- GET /api/system
- GET /api/streams
- POST /api/streams
- POST /api/streams/<stream_id>/start
- POST /api/streams/<stream_id>/stop
- POST /api/streams/<stream_id>/restart
- DELETE /api/streams/<stream_id>
- GET /api/streams/<stream_id>/logs
- GET /api/files
- POST /api/files/upload
- DELETE /api/files/<filename>

## Uploads

Os videos enviados ficam em uploads/. Por padrao esta pasta nao deve ir para o git.

## Observacoes

- Encoders detectados automaticamente via `ffmpeg -encoders`.
- O modo `loop` reinicia automaticamente quando a playlist termina.
