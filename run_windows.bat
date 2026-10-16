@echo off
REM Starts the app with Waitress (a real multi-user WSGI server) instead of
REM Flask's single-connection dev server, bound to all network interfaces
REM so other PCs on the same network/VPN can reach it.
REM
REM Run this from the project folder, with venv already created
REM (python -m venv venv && venv\Scripts\activate && pip install -r requirements.txt)
REM and ADMIN_PASSWORD / SECRET_KEY set however you prefer (this .env file,
REM or set them as real environment variables before running this).

if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do set %%A=%%B
)

venv\Scripts\waitress-serve --host=0.0.0.0 --port=8080 --threads=8 wsgi:app
