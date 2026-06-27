"""
Testes de monitoramento — WaveGate Bot
Verifica: WIN / LOSS / TIMEOUT no MonitorAgent + integração com PortfolioAgent
"""

import sys, os, pathlib, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from agents.signal_agent import TradeSignal
from agents.monitor_agent import MonitorAgent
from agents.portfolio_agent import PortfolioAgent

PASS = "[OK]"
FAIL = "[FALHOU]"

results = []

# ── Factory de sinal de teste ────────────────────────────────────────────────

def make_signal(entry=2500.0, target_pct=0.006, stop_pct=0.003, leverage=3, direction="LONG"):
    if direction == "SHORT":
        target = entry * (1 - target_pct)
        stop = entry * (1 + stop_pct)
    else:
        target = entry * (1 + target_pct)
        stop = entry * (1 - stop_pct)
    return TradeSignal(
        symbol             = "ETHUSDT",
        direction          = direction,
        entry_price        = entry,
        target_price       = target,
        stop_price         = stop,
        leverage           = leverage,
        risk_reward        = target_pct / stop_pct,
        conditions_met     = ["markov_gate", "wt_cross_up", "above_ema55", "ema_aligned"],
        position_size_usdt = 100.0,
    )

def make_env(max_dur=120):
    config = {
        "initial_equity_usdt": 10_000.0,
        "monitor_update_pct": 25,
        "max_duration_min": max_dur,
    }
    portfolio = PortfolioAgent(config)
    # Isola cada teste: arquivo temporario + reset de estado
    portfolio._state_file = pathlib.Path(tempfile.mktemp(suffix=".json"))
    portfolio.equity = 10_000.0
    portfolio.open_positions = {}
    portfolio.closed_trades  = []
    monitor = MonitorAgent(config, portfolio=portfolio)
    return monitor, portfolio

# ── Teste 1: WIN ─────────────────────────────────────────────────────────────

def test_win():
    monitor, portfolio = make_env()
    fired = []
    monitor.on_event("on_target", lambda s, *_: fired.append("WIN"))

    signal = make_signal(entry=2500.0)
    portfolio.open_position(signal)
    monitor.start_monitoring(signal)

    # Preço abaixo do alvo — nao deve fechar
    monitor.update_price("ETHUSDT", 2510.0)
    assert len(fired) == 0, "Disparou WIN antes do alvo"

    # Preço atinge alvo (2500 * 1.006 = 2515.0)
    monitor.update_price("ETHUSDT", signal.target_price)
    assert fired == ["WIN"], f"WIN nao disparou: {fired}"
    assert "ETHUSDT" not in monitor.active_symbols(), "Monitoramento nao encerrou apos WIN"

    eq_expected = 10_000.0 + 100.0 * 3 * 0.006  # margem * lev * target_pct
    assert abs(portfolio.equity - eq_expected) < 0.01, \
        f"Equity incorreta: {portfolio.equity:.4f} != {eq_expected:.4f}"

    results.append((PASS, "WIN: preco atinge alvo -> callback disparado + equity correta"))

# ── Teste 2: LOSS ────────────────────────────────────────────────────────────

def test_loss():
    monitor, portfolio = make_env()
    fired = []
    monitor.on_event("on_stop", lambda s, *_: fired.append("LOSS"))

    signal = make_signal(entry=2500.0)
    portfolio.open_position(signal)
    monitor.start_monitoring(signal)

    # Preço acima do stop (2492.5) — nao deve fechar
    monitor.update_price("ETHUSDT", 2493.0)
    assert len(fired) == 0, "Disparou LOSS antes do stop"

    # Preço atinge stop (2500 * 0.997 = 2492.5)
    monitor.update_price("ETHUSDT", signal.stop_price)
    assert fired == ["LOSS"], f"LOSS nao disparou: {fired}"
    assert "ETHUSDT" not in monitor.active_symbols(), "Monitoramento nao encerrou apos LOSS"

    eq_expected = 10_000.0 - 100.0 * 3 * 0.003
    assert abs(portfolio.equity - eq_expected) < 0.01, \
        f"Equity incorreta: {portfolio.equity:.4f} != {eq_expected:.4f}"

    results.append((PASS, "LOSS: preco atinge stop -> callback disparado + equity correta"))

# ── Teste 3: TIMEOUT ─────────────────────────────────────────────────────────

def test_timeout():
    monitor, portfolio = make_env(max_dur=0)  # timeout imediato
    fired = []
    monitor.on_event("on_timeout", lambda s, *args: fired.append("TIMEOUT"))

    signal = make_signal(entry=2500.0)
    portfolio.open_position(signal)
    monitor.start_monitoring(signal)

    # Qualquer update ja deve disparar timeout (max_dur=0)
    monitor.update_price("ETHUSDT", 2503.0)
    assert fired == ["TIMEOUT"], f"TIMEOUT nao disparou: {fired}"
    assert "ETHUSDT" not in monitor.active_symbols(), "Monitoramento nao encerrou apos TIMEOUT"

    results.append((PASS, "TIMEOUT: duracao expirada -> callback disparado"))

# ── Teste 4: progresso parcial ───────────────────────────────────────────────

def test_progress():
    monitor, portfolio = make_env()
    progress_updates = []
    monitor.on_event("on_progress", lambda s, pct, px: progress_updates.append(pct))

    signal = make_signal(entry=2500.0)
    portfolio.open_position(signal)
    monitor.start_monitoring(signal)

    # Avanca 30% do caminho ate o alvo
    mid = signal.entry_price + (signal.target_price - signal.entry_price) * 0.30
    monitor.update_price("ETHUSDT", mid)
    assert len(progress_updates) == 1, f"Progresso nao disparou: {progress_updates}"
    assert progress_updates[0] >= 25, f"Progresso < 25%: {progress_updates[0]}"

    results.append((PASS, f"PROGRESSO: update disparado em {progress_updates[0]:.0f}%"))

# ── Teste 5: sem trade aberto ────────────────────────────────────────────────

def test_no_watch():
    monitor, portfolio = make_env()
    fired = []
    monitor.on_event("on_target", lambda s, *_: fired.append("WIN"))

    # Update sem posição monitorada — deve ignorar silenciosamente
    monitor.update_price("ETHUSDT", 99999.0)
    assert len(fired) == 0, "Disparou evento sem posicao monitorada"

    results.append((PASS, "SEM POSICAO: update ignorado corretamente"))

# ── Teste 6: dois pares simultaneos ─────────────────────────────────────────

def test_two_symbols():
    monitor, portfolio = make_env()
    closed = []
    monitor.on_event("on_target", lambda s, *_: closed.append(s.symbol + "_WIN"))
    monitor.on_event("on_stop",   lambda s, *_: closed.append(s.symbol + "_LOSS"))

    sig_eth = make_signal(entry=2500.0)
    sig_sol = TradeSignal(
        symbol="SOLUSDT", direction="LONG",
        entry_price=150.0, target_price=150.9, stop_price=149.55,
        leverage=3, risk_reward=2.0,
        conditions_met=["markov_gate","wt_cross_up"],
        position_size_usdt=100.0,
    )

    portfolio.open_position(sig_eth)
    portfolio.open_position(sig_sol)
    monitor.start_monitoring(sig_eth)
    monitor.start_monitoring(sig_sol)

    assert monitor.active_count == 2, f"Esperava 2 ativos, tem {monitor.active_count}"

    monitor.update_price("ETHUSDT", sig_eth.target_price)  # ETH WIN
    monitor.update_price("SOLUSDT", sig_sol.stop_price)    # SOL LOSS

    assert "ETHUSDT_WIN" in closed, f"ETH WIN nao disparou: {closed}"
    assert "SOLUSDT_LOSS" in closed, f"SOL LOSS nao disparou: {closed}"
    assert monitor.active_count == 0, f"Ainda ha posicoes ativas: {monitor.active_count}"

    results.append((PASS, "2 PARES: ETH WIN + SOL LOSS simultaneos"))


def test_short_win_loss():
    monitor, portfolio = make_env()
    closed = []
    monitor.on_event("on_target", lambda s, *_: closed.append("SHORT_WIN"))

    signal = make_signal(entry=100.0, direction="SHORT")
    portfolio.open_position(signal)
    monitor.start_monitoring(signal)

    monitor.update_price("ETHUSDT", 99.7)
    assert len(closed) == 0, f"SHORT fechou antes do alvo: {closed}"

    monitor.update_price("ETHUSDT", signal.target_price)
    assert closed == ["SHORT_WIN"], f"SHORT WIN nao disparou: {closed}"

    eq_expected = 10_000.0 + 100.0 * 3 * 0.006
    assert abs(portfolio.equity - eq_expected) < 0.01, \
        f"Equity short incorreta: {portfolio.equity:.4f} != {eq_expected:.4f}"

    results.append((PASS, "SHORT: preco cai ate o alvo -> WIN + equity correta"))

# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("WIN",                test_win),
        ("LOSS",               test_loss),
        ("TIMEOUT",            test_timeout),
        ("PROGRESSO",          test_progress),
        ("SEM POSICAO",        test_no_watch),
        ("2 PARES SIMULTANEOS",test_two_symbols),
        ("SHORT WIN",          test_short_win_loss),
    ]

    print("\n" + "="*60)
    print("  WaveGate Bot — Testes de Monitoramento")
    print("="*60)

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            results.append((FAIL, f"{name}: {e}"))
            failed += 1

    print()
    for status, msg in results:
        print(f"  {status}  {msg}")

    print()
    print(f"  Resultado: {passed}/{len(tests)} passaram", end="")
    if failed:
        print(f" | {failed} FALHARAM")
    else:
        print(" — todos OK")
    print("="*60)
