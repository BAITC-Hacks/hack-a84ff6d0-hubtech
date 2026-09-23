"""Обновить файл контракта: python -m ml_api.export_openapi."""
import json
from pathlib import Path

from ml_api.app import create_app


def main():
    path = Path(__file__).with_name("openapi.json")
    schema = create_app().openapi()  # Lifespan не запускается; артефакты не нужны.
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
