"""Administrative commands; passwords never appear in command arguments."""
from __future__ import annotations

import argparse
import getpass
import os

from fastapi import HTTPException

from app.repositories import create_user
from app.storage import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление сервисом автозаказов")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Применить миграции базы данных")
    admin = subparsers.add_parser("create-admin", help="Создать администратора")
    admin.add_argument("--username", help="Имя пользователя; иначе запросить интерактивно")
    admin.add_argument("--password-env", help="Имя переменной окружения с паролем для автоматизированной установки")
    args = parser.parse_args()
    migrate()
    if args.command == "migrate":
        print("Миграции применены.")
        return
    username = args.username or input("Имя администратора: ").strip()
    if args.password_env:
        password = os.environ.pop(args.password_env, "")
        if not password:
            parser.error("Указанная переменная пароля пуста.")
    else:
        password = getpass.getpass("Пароль (не менее 12 символов): ")
        if password != getpass.getpass("Повторите пароль: "):
            parser.error("Пароли не совпадают.")
    try:
        user = create_user(username, password, "admin")
    except HTTPException as error:
        parser.error(str(error.detail))
    print(f"Администратор {user['username']} создан. Пароль не сохраняется в открытом виде.")


if __name__ == "__main__":
    main()
