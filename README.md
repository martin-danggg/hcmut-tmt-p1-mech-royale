# HCMUT TMT – Mech Royale bot

Builders Buildathon 2026, track **Builders – Mech Royale**. Team HCMUT TMT.

- `bot.py` – the bot (entry point), `mechfight.py` – protocol wrapper from the kit.
- `check/` – the kit's conformance check, `arena/v2/` – the map (the scorer mounts its own copy at `/arena`).
- `docs/protocol.md` and `mechfight.schema.json` – the protocol spec and schema from the kit.

## Local check

```bash
python3 check/check.py -- python3 -u bot.py
docker build -t hcmut-tmt-mech .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:size=64m -v "$PWD/arena:/arena:ro" hcmut-tmt-mech
```
