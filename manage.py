#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    from django.core.management.commands.runserver import Command as RunServer

    # Puerto del sistema: «manage.py runserver» sin puerto usa 8508 (o MES_PORT) en vez del 8000 de Django.
    RunServer.default_port = os.environ.get("MES_PORT", "8508")

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
