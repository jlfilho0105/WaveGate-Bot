@echo off
echo === WaveGate Bot Setup ===

REM 1. Criar venv Python 3.14
py -3.14 -m venv .venv
if errorlevel 1 (
    echo Tentando python generico...
    python -m venv .venv
)

REM 2. Instalar dependencias
.venv\Scripts\pip install -r requirements.txt

REM 3. Criar pastas necessarias
if not exist logs mkdir logs
if not exist data\markov_cache mkdir data\markov_cache
if not exist backtest\results mkdir backtest\results

REM 4. Copiar .env.example
if not exist .env (
    copy .env.example .env
    echo Edite .env com suas credenciais!
)

echo.
echo Setup concluido!
echo Edite .env e config.yaml, depois execute: .venv\Scripts\python main.py
pause
