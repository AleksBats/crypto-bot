"""
signal_stats/ — signal performance tracking / paper trading statistics.

⚠️  NAMING: this package was originally called `statistics/`, which collides
with the Python standard library module of the same name. Because
run_live.py puts the project root at the FRONT of sys.path
(`sys.path.insert(0, ...)`), that old name shadowed the stdlib module for
the entire process — which silently broke `statistics.mean()` in the
repo's older `price.py` and `volume.py` monitor stubs.

It was renamed to `signal_stats/` before ever being deployed, specifically
so nothing in this repo (or any dependency) can accidentally get this
package when it asks for stdlib `statistics`. Do not rename it back, and
do not add any other module here whose name collides with a stdlib module.
See DECISIONS.md #12.

Layout:
    signal_store.py       all SQL (Neon Postgres via asyncpg)
    signal_tracker.py     record signals + resolve WIN/LOSS/OPEN, RSI, combos
    performance.py        pure aggregation (win rate, R, MFE/MAE, Profit Factor)
    reports.py            builds the /today /week /month /stats Telegram text
    telegram_commands.py  long-polls Telegram for those commands
"""
