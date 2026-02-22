# mysecond

Chess opening research tool. Generates annotated PGN importable into ChessBase.

## Quick start

```bash
bash scripts/setup_macos.sh
bash scripts/run_demo.sh
```

## Usage

```
mysecond --fen "<FEN>" --side white|black [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--fen` | required | Starting position in FEN notation |
| `--side` | required | Side to research (`white` or `black`) |
| `--plies` | 12 | Line length in half-moves |
| `--beam` | 30 | Beam width (candidate lines kept per ply) |
| `--depths` | `16,20,24` | Comma-separated evaluation depths |
| `--time-ms` | 150 | Per-position time budget (ms, combined with depth) |
| `--out` | `ideas.pgn` | Output PGN file |
| `--workers` | 4 | Parallel engine workers |

## Example

```bash
mysecond \
  --fen "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1" \
  --side black \
  --plies 10 \
  --beam 20 \
  --depths 16,20,24 \
  --time-ms 150 \
  --out ideas.pgn
```

## Engine

Stockfish is detected automatically via `which stockfish`. Override with:

```bash
export MYSECOND_STOCKFISH_PATH=/path/to/stockfish
```

## Output format

Each candidate line is exported as a separate PGN game. The last move of each
line carries a comment with:

- `[%eval]` annotation (ChessBase-compatible)
- per-depth evaluations
- stability metric (cp stddev across depths)
- master game count from Lichess explorer
- final computed score

## Scoring

```
score = eval_cp − 5 × stability + 50 × rarity_bonus
```

- **eval_cp** — average centipawn eval across depths (perspective-corrected)
- **stability** — standard deviation of cp across depths (lower = more stable)
- **rarity_bonus** — `1 / (1 + log(1 + master_count))`; rewards surprise value

## Architecture

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Click entry-point, orchestration |
| `engine.py` | Stockfish UCI wrapper |
| `explorer.py` | Lichess masters API (all network calls here) |
| `cache.py` | SQLite response cache |
| `search.py` | Beam search + parallel root expansion |
| `score.py` | Composite scoring |
| `export.py` | PGN export |
| `models.py` | Shared data classes |

## Tests

```bash
pip install -e ".[test]"
pytest tests/
```
