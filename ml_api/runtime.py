"""Мост к существующим CLI-модулям без копирования формул прогноза.

CLI используют импорты из каталога scripts. Добавляем только этот известный
каталог один раз; пути и Python-код никогда не берутся из HTTP-запросов.
"""
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from train_iek_model import FEATURES, CANDIDATES, attach_history, predict_model, read_csv  # noqa: E402
from explainable_demand import predict_components  # noqa: E402
