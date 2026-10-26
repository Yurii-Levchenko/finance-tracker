"""Шаблон WSGI-файлу для PythonAnywhere. Секретів тут немає й не має бути.

Скопіюйте вміст у WSGI-файл вебзастосунку (вкладка Web → WSGI configuration file)
і замініть USERNAME на своє імʼя користувача PythonAnywhere.
Паролі й ключ підпису лежать у ~/.tracker.env (див. README), app.py читає його сам.
"""

import os
import sys

PROJECT = "/home/USERNAME/finance-tracker"
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

from app import application  # noqa: E402,F401
