import time

from tetrio_state_source import TetrioStateSource

source = TetrioStateSource("config.json")

try:
    source.start()
    print("PID:", source.proc.pid if source.proc else None, flush=True)

    for index in range(120):
        proc = source.proc
        poll = None if proc is None else proc.poll()

        print(
            index,
            "poll=", poll,
            "connected=", source.browser_connected,
            "ready=", source.last_ready,
            "error=", repr(source.last_error),
            "log=", repr(source.last_log_line),
            flush=True,
        )

        if proc is None or poll is not None:
            break

        time.sleep(0.5)
finally:
    source.close()
