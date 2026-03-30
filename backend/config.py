from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    recordings_dir: Path = Path("recordings")
    storage_threshold_pct: float = 85.0  # trigger cleanup above this %
    storage_max_age_days: int = 7
    mjpeg_quality: int = 85  # JPEG quality for MJPEG stream (1-100)
    stream_fps: int = 20
    snapshot_fps: int = 1  # default still-capture FPS
    host_yolo_model: str = "yolov8n.pt"  # default host-side model
    poe_subnet: str = "169.254.0.0/16"
    cors_origins: list[str] = ["*"]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
