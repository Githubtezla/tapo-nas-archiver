# tapo-nas-archiver

Copy the recordings of your Tapo cameras and doorbells to your own NAS, including models that cannot save to a NAS at all.

Tapo devices store their recordings on a microSD card or in the paid Tapo Care cloud. Saving to a NAS or NVR is only possible through RTSP/ONVIF, and battery-powered models like the **D230 doorbell** or the **H200 hub** do not offer RTSP at all. This project takes a different route: it downloads the recordings that are already on the microSD card, the same way the Tapo app plays them back, and stores them as MP4 files on your NAS. It runs as a small Docker container, talks to the device only inside your LAN and needs no cloud, no Home Assistant and no open ports.

It is built on [pytapo](https://github.com/JurajNyiri/pytapo), which does the actual work of talking to the device.

## How it works

1. The container logs in to your hub or camera in the LAN with the password of your Tapo account.
2. For a hub it finds the paired cameras and doorbells, for a camera it uses the camera itself.
3. It lists the recordings on the microSD card day by day and downloads every clip that is not on the NAS yet.
4. It does this once at start-up and then every night. Day folders older than `KEEP_DAYS` are deleted on the NAS.

With a hub like the H200 the microSD card sits in the hub, which runs on mains power. The download reads from the hub, so the battery doorbell itself does not have to wake up for it.

## Tested devices

| Device | Firmware | Result |
| --- | --- | --- |
| Hub H200 with doorbell D230 (kit D230S1) | hub 1.7.7, doorbell hardware 1.20 | Works with `METHOD=playback` (the default). `METHOD=download` is aborted by the hub. |
| Camera C200 | 1.4.6 | Login and listing work; downloading recordings from its own card has not been tested yet. |

Other Tapo devices that can play back their SD card recordings in the Tapo app will probably work as well. Reports in the issues are welcome, with model, firmware and which `METHOD` worked.

## Requirements

- A NAS or any Linux machine with Docker and Docker Compose (tested on a UGREEN NAS with UGOS Pro).
- The Tapo device in the same LAN as the NAS, with a microSD card and recording switched on in the Tapo app.
- The IP address of the device (Tapo app, device settings, device info). Give it a fixed address in your router, otherwise the container cannot find it after a change.
- The password of your Tapo account. Two-factor authentication on the account does not matter, because the login happens locally on the device.

## Quick start

```bash
git clone https://github.com/Githubtezla/tapo-nas-archiver.git
cd tapo-nas-archiver
```

1. Create the file `secrets/tapo_password` with one line: the password of your Tapo account.
2. Open `docker-compose.yaml` and set `TAPO_HOSTS` to the IP of your hub or camera, and `TZ` to your time zone.
3. Build the image and run a test. It lists the devices and recordings and downloads the newest clip:

```bash
docker compose build
docker compose run --rm archiver python /app/tapo_archiver.py test
```

4. If the test saved a clip in `recordings/`, start the service:

```bash
docker compose up -d
docker compose logs -f archiver
```

The first run downloads everything that is on the card and can take many hours (see below). After that, each nightly run only fetches the new clips.

### NAS web interfaces

Some NAS web interfaces only show Docker projects that were created there. On UGREEN UGOS, a project started only with `docker compose up -d` does not appear in the Docker app. Copy the folder to the NAS, then create the project in the Docker app (Project, Create), pick the folder and choose "import this configuration". Synology Container Manager has a similar dialog (Project, Create, use an existing `docker-compose.yaml`); this has not been tested here. Building and testing on the command line before that is fine.

## Output

```
recordings/
├── 2026-10-26/
│   ├── 07-19-53_D230.mp4
│   └── ...
├── LAST-RUN.txt      e.g. "2026-10-27 04:52:10 OK new 23, already there 1568, still recording 0, errors 0"
└── .partial/         unfinished downloads; only complete files are moved into the day folders
```

File and folder names use local time (`TZ`). The clips are MP4 files with H.264 video and AAC audio; the D230 records 2560 × 1920 at about 2 to 8 MB per clip of 14 to 25 seconds.

## Configuration

All settings are environment variables in `docker-compose.yaml`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TAPO_HOSTS` | none | IP of the hub or camera; several devices separated by commas |
| `TZ` | `Europe/Berlin` in the example | Time zone for file names and the nightly run |
| `DAYS_BACK` | `89` | How many days back each run looks on the card. Always kept below `KEEP_DAYS`, otherwise deleted days would be downloaded again. |
| `KEEP_DAYS` | `90` | Day folders older than this are deleted on the NAS; `0` keeps everything |
| `RUN_AT_HOUR` | `4` | Hour of the nightly run (local time) |
| `METHOD` | `playback` | `playback` works on the H200; `download` is faster on devices that support it |
| `WINDOW_SIZE` | `50` | pytapo stream window; lower values are slower but more stable |
| `PASSWORD_FILE` | `/run/secrets/tapo_password` | Where the container reads the password |

## Speed and battery

`playback` streams each clip roughly in real time plus a few seconds, about 10 to 30 seconds per clip. A full card with 1,600 clips takes about half a day on the first run; a normal night with 20 to 50 new clips takes 10 to 30 minutes.

The device handles only one playback at a time. While the archiver downloads, playing recordings in the Tapo app may fail, and a second archiver instance gets `HTTP 503`.

With a hub the doorbell should not be woken by the download, because the card is in the hub. This was observed but not measured over a long time; keep an eye on the battery level during the first days. A battery camera that records to its own card may have to wake up for each download.

## Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `login failed` | Usually a wrong password: it has to be the password of your Tapo account, not a camera account. If your Tapo app offers a "Third-Party Compatibility" switch, enabling it may help (not needed with the tested H200). |
| `gave up on ... (try METHOD=playback)` | The device aborted the fast download. Set `METHOD: "playback"`. |
| `HTTP request returned 503` | The device is busy with another playback (Tapo app or a second instance). The clip is retried on the next run. |
| `ModuleNotFoundError: No module named 'aiofiles'` | pytapo needs `aiofiles` but does not declare it; it is already in `requirements.txt`, rebuild the image. |
| `Cannot run the event loop while another loop is running` | Only relevant if you change the code: pytapo is blocking and must not be called from inside a running asyncio loop. |
| `Permission denied` on start | The files were created by a different user. The `fixperms` service fixes this on every start; start the whole project, not only the `archiver` service. |

## Security

- No port is published. The container only connects to your Tapo device in the LAN (TCP 443 and 8800).
- It runs as an unprivileged user with a read-only file system, no Linux capabilities and `no-new-privileges`.
- The password file is only readable by that user. It is the password of your whole Tapo account, so treat the NAS folder accordingly. `secrets/` is excluded from git.
- The local API used by pytapo is not officially documented by TP-Link. A firmware update can change or break it.

## Credits and license

- [pytapo](https://github.com/JurajNyiri/pytapo) by JurajNyiri (MIT License) does the communication with the Tapo devices.
- This project is not affiliated with or endorsed by TP-Link. Tapo is a trademark of TP-Link.
- Released under the [MIT License](LICENSE).
