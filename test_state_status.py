import time
from tetrio_state_source import TetrioStateSource

source = TetrioStateSource("config.json")

try:
    source.start()
    print("PID:", source.proc.pid if source.proc else None, flush=True)

    for index in range(240):
        status = source.get_status()
        proc = source.proc
        poll = None if proc is None else proc.poll()

        print(
            index,
            "poll=", poll,
            "browser=", status.get("browser_status"),
            "game=", status.get("game_state"),
            "detail=", repr(status.get("detail")),
            "current=", status.get("current"),
            "queue=", status.get("queue"),
            flush=True,
        )

        time.sleep(0.5)
finally:
    source.close()
