"""Copy recordings from a Tapo hub's (or camera's) microSD card to a NAS folder.

Usage:
  python tapo_archiver.py test   check login, list devices and recordings, download the newest clip
  python tapo_archiver.py once   download all missing recordings of the last DAYS_BACK days, then exit
  python tapo_archiver.py run    like "once" at start-up and then every day at RUN_AT_HOUR (local time)

Works with Tapo hubs (e.g. H200 with a battery doorbell D230) and with cameras that record to their
own microSD card. It talks to the device in the LAN with pytapo; no RTSP, no ONVIF, no cloud.
Files that already exist are skipped, recordings still in progress are picked up by the next run.
"""

import asyncio
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta

from pytapo import Tapo
from pytapo.media_stream.downloader import Downloader

HOSTS = [h.strip() for h in os.environ.get("TAPO_HOSTS", "").split(",") if h.strip()]
OUT = os.environ.get("OUTPUT_DIR", "/recordings")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "89"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "90"))  # 0 = never delete anything
if KEEP_DAYS > 0:
    # never look further back than we keep, otherwise deleted days would come back from the SD card
    DAYS_BACK = min(DAYS_BACK, KEEP_DAYS - 1)
RUN_AT_HOUR = int(os.environ.get("RUN_AT_HOUR", "4"))
# "download" is fast but the H200 (firmware 1.7.7) aborts it; "playback" works there
METHOD = os.environ.get("METHOD", "playback")
WINDOW_SIZE = os.environ.get("WINDOW_SIZE", "50")
PASSWORD_FILE = os.environ.get("PASSWORD_FILE", "/run/secrets/tapo_password")

TMP = os.path.join(OUT, ".partial") + "/"   # unfinished files; only complete clips go into the day folders


def log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + msg, flush=True)


def password():
    with open(PASSWORD_FILE, encoding="utf-8") as f:
        pw = f.read().strip()
    if not pw:
        raise SystemExit("password file is empty: " + PASSWORD_FILE)
    return pw


def safe(text):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text)).strip("-") or "camera"


def connect():
    """Return a list of (name, Tapo) for every camera; for a hub, one entry per paired child device.

    Must run outside of an asyncio event loop: pytapo is blocking and starts event loops of its own.
    """
    pw = password()
    cams = []
    for host in HOSTS:
        try:
            # Tapo devices accept the user "admin" with the password of the Tapo (cloud) account
            dev = Tapo(host, "admin", pw, pw, printWarnInformation=False)
            info = dev.getBasicInfo().get("device_info", {}).get("basic_info", {})
            model = info.get("device_model", "camera")
            log(f"{host}: {model} '{info.get('device_alias', '')}' firmware {info.get('sw_version', '?')}")
        except Exception as err:
            log(f"{host}: login failed: {err}")
            continue
        try:
            children = dev.getChildDevices()
        except Exception:
            children = None
        # depending on the firmware a plain list or an object holding the list
        if isinstance(children, dict):
            children = (children.get("child_device_list")
                        or next((v for v in children.values() if isinstance(v, list)), None))
        if children and not all(isinstance(c, dict) for c in children):
            log(f"{host}: unknown format of the child list: {type(children[0]).__name__}")
            children = None
        if not children:
            log(f"{host}: no hub children, treated as a single camera")
            cams.append((safe(model), dev))
            continue
        for child in children:
            # on the H200 device_name is model and hardware version, e.g. "D230 1.20"
            name = (child.get("device_name") or child.get("model") or "camera").split()[0]
            log(f"{host}: child {child.get('device_name', '?')}")
            cams.append((safe(name), Tapo(host, "admin", pw, pw, childID=child["device_id"],
                                          printWarnInformation=False)))
    return cams


def recordings_of(cam, day):
    result = cam.getRecordings(day.strftime("%Y%m%d"))
    items = []
    for rec in result or []:
        for key in rec:
            items.append((int(rec[key]["startTime"]), int(rec[key]["endTime"])))
    return sorted(set(items))


async def fetch(cam, name, start, end, correction):
    day_dir = os.path.join(OUT, datetime.fromtimestamp(start).strftime("%Y-%m-%d")) + "/"
    file_name = datetime.fromtimestamp(start).strftime("%H-%M-%S") + f"_{name}.mp4"
    final = day_dir + file_name
    if os.path.isfile(final):
        return "Skipping", final
    downloader = Downloader(cam, start, end, correction, TMP, None, True, WINDOW_SIZE,
                            fileName=file_name, progressInterval=30, method=METHOD)
    last = None
    async for status in downloader.download():
        last = status["currentAction"]
    part = TMP + file_name
    if last not in ("Skipping", "Recording in progress", "Giving up") and os.path.isfile(part):
        os.makedirs(day_dir, exist_ok=True)
        os.replace(part, final)
    return last, final


async def run_once(cams, test=False):
    # pytapo calls are blocking, so they run in the executor
    stats = {"new": 0, "existing": 0, "in_progress": 0, "errors": 0}
    loop = asyncio.get_running_loop()
    today = datetime.now()
    for name, cam in cams:
        correction = await loop.run_in_executor(None, cam.getTimeCorrection)
        all_recs = []
        for back in range(DAYS_BACK + 1):
            day = today - timedelta(days=back)
            try:
                recs = await loop.run_in_executor(None, recordings_of, cam, day)
            except Exception as err:
                log(f"{name}: cannot list recordings of {day:%Y-%m-%d}: {err}")
                stats["errors"] += 1
                continue
            if test and recs:
                log(f"{name}: {day:%Y-%m-%d}: {len(recs)} recording(s)")
            all_recs += recs
        all_recs = sorted(set(all_recs))
        log(f"{name}: {len(all_recs)} recordings in the last {DAYS_BACK + 1} days")
        if test:
            # only the newest finished recording
            all_recs = [r for r in all_recs if r[1] < time.time() - correction - 120][-1:]
        for start, end in all_recs:
            try:
                action, path = await fetch(cam, name, start, end, correction)
            except Exception as err:
                log(f"{name}: error at {datetime.fromtimestamp(start):%Y-%m-%d %H:%M:%S}: {err}")
                stats["errors"] += 1
                continue
            if action == "Skipping":
                stats["existing"] += 1
            elif action == "Recording in progress":
                stats["in_progress"] += 1
            elif action == "Giving up":
                log(f"{name}: gave up on {path} (try METHOD=playback)")
                stats["errors"] += 1
            else:
                stats["new"] += 1
                size = os.path.getsize(path) if os.path.isfile(path) else 0
                log(f"{name}: saved {path} ({size / 1e6:.1f} MB)")
    return stats


def cleanup():
    if KEEP_DAYS <= 0:
        return
    limit = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for entry in sorted(os.listdir(OUT)):
        if len(entry) == 10 and entry[4] == "-" and entry < limit:
            shutil.rmtree(os.path.join(OUT, entry), ignore_errors=True)
            log(f"removed old day folder {entry}")


def write_status(text):
    with open(os.path.join(OUT, "LAST-RUN.txt"), "w", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n")


def job(test=False):
    shutil.rmtree(TMP, ignore_errors=True)   # leftovers of an interrupted run
    os.makedirs(TMP, exist_ok=True)
    try:
        cams = connect()
        if not cams:
            raise RuntimeError("no device reachable")
        stats = asyncio.run(run_once(cams, test))
        text = (f"new {stats['new']}, already there {stats['existing']}, "
                f"still recording {stats['in_progress']}, errors {stats['errors']}")
        log("done: " + text)
        if not test:
            cleanup()
            write_status(("OK " if stats["errors"] == 0 else "PARTIAL ") + text)
        return stats["errors"] == 0
    except Exception as err:
        traceback.print_exc()
        if not test:
            write_status("ERROR " + str(err))
        return False


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if not HOSTS:
        raise SystemExit("TAPO_HOSTS is not set")
    os.makedirs(OUT, exist_ok=True)
    if mode == "test":
        sys.exit(0 if job(test=True) else 1)
    if mode == "once":
        sys.exit(0 if job() else 1)
    log(f"started, running now and every day at {RUN_AT_HOUR}:00")
    while True:
        job()
        now = datetime.now()
        nxt = now.replace(hour=RUN_AT_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        log(f"next run {nxt:%Y-%m-%d %H:%M}")
        time.sleep((nxt - now).total_seconds())


if __name__ == "__main__":
    main()
