@echo off
REM Starts the app with Waitress (a real multi-user WSGI server) instead of
REM Flask's single-connection dev server, bound to all network interfaces
REM so other PCs on the same network/VPN can reach it.
REM
REM Run this from the project folder, with venv already created
REM (python -m venv venv && venv\Scripts\activate && python -m pip install -r requirements.txt)
REM and a .env file with ADMIN_PASSWORD / SECRET_KEY / etc set (see README.md)
REM -- config.py loads .env itself on startup, so there's nothing to set up here.

venv\Scripts\waitress-serve --host=0.0.0.0 --port=8080 --threads=8 wsgi:app
