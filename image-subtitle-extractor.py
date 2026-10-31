#!/usr/bin/env python3
"""Write an .srt next to each video whose only subtitles are images.

Watches /videos, where the videos, or directories of them, are mounted. The
image track, PGS (Blu-ray) or VobSub (DVD), is extracted with PyAV (and
mkvextract for VobSub), read by subtile-ocr and cleaned by cleanit. README.md
says when an .srt is written, rewritten or left alone.
"""

import collections
import logging
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time

import av
import cleanit
import pyinotify

ROOT = "/videos"
# ISO 639-2, of the track to OCR and for Tesseract, whose data for it the image
# must have. The rules in CLEANIT_CONFIG are for English too.
LANGUAGE = "eng"
# Added to cleanit's own rules. Mount another file here to change them.
CLEANIT_CONFIG = "/usr/local/share/image-subtitle-extractor/cleanit.yml"
VIDEO_SUFFIXES = (".mkv", ".m4v", ".mp4")
# The captions minidlna serves with a video: <base>.srt, else <base>.smi, by
# exact name.
CAPTION_SUFFIXES = (".srt", ".smi")
SOURCE_XATTR = "user.image-subtitle-extractor.source"
# The image subtitle decoders subtile-ocr can read.
IMAGE_CODECS = {"pgssub", "dvdsub"}
UNKNOWN_LANGUAGES = {None, "", "und"}
TIMEOUT = 3600

# Tesseract reads | and / for a lone I, which cleanit turns back into I. Brace
# for parenthesis would keep cleanit from finding "(sighs)" and removing it.
OCR_BLACKLIST = "`_~{}"

# IN_MOVED_FROM and IN_MOVE_SELF are only for pyinotify, which needs them to
# keep the paths of watches on moved directories right.
MASK = (pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVED_TO | pyinotify.IN_MOVED_FROM
        | pyinotify.IN_CREATE | pyinotify.IN_DELETE | pyinotify.IN_MOVE_SELF)

log = logging.getLogger("image-subtitle-extractor")


class ConvertError(Exception):
    pass


class Skip(Exception):
    """The video needs no .srt, for the reason given."""


def is_video(path):
    return path.lower().endswith(VIDEO_SUFFIXES)


def sidecar(video):
    return os.path.splitext(video)[0] + ".srt"


def other_captions(video):
    """Whether minidlna has captions for the video that are not ours."""
    base = os.path.splitext(video)[0]
    return any(os.path.lexists(base + suffix) and our_stamp(base + suffix) is None
               for suffix in CAPTION_SUFFIXES)


def stamp(st):
    return f"{st.st_size}:{st.st_mtime_ns}"


def our_stamp(path):
    """The SOURCE_XATTR of path, or None if it is missing or not ours."""
    try:
        return os.getxattr(path, SOURCE_XATTR).decode()
    except OSError:
        return None


def videos_under(root):
    """Videos under root, sorted so a season is done in episode order."""
    for dirpath, dirnames, filenames in os.walk(
            root, onerror=lambda exc: log.warning("cannot list %s", exc.filename)):
        dirnames.sort()
        for name in sorted(filenames):
            if is_video(name):
                yield os.path.join(dirpath, name)


def image_track(video):
    """(index, codec) of the track to OCR. Raises Skip if there is none."""
    # Read while the container is open: a stream's fields are garbage after.
    try:
        with av.open(video) as container:
            streams = [Stream(s) for s in container.streams.subtitles]
    except av.FFmpegError as exc:
        raise ConvertError(f"cannot read: {exc}") from exc
    if any(s.text for s in streams):
        raise Skip("has text subtitles")

    tracks = [s for s in streams if s.codec in IMAGE_CODECS and not s.unwanted
              and s.language in {LANGUAGE} | UNKNOWN_LANGUAGES]
    if not tracks:
        raise Skip(f"no {LANGUAGE} PGS or VobSub subtitles")
    # Tagged with the language before untagged, else in their order.
    track = min(tracks, key=lambda s: s.language != LANGUAGE)
    return track.index, track.codec


class Stream:
    """What image_track needs to know of a subtitle stream."""

    def __init__(self, s):
        self.index = s.index
        self.codec = s.codec_context.name
        self.text = s.codec_context.codec.text_sub
        self.language = s.metadata.get("language")
        title = s.metadata.get("title", "").lower()
        self.unwanted = bool(
            s.disposition & (av.stream.Disposition.forced | av.stream.Disposition.comment)
            or "forced" in title or "commentary" in title)


def last_line(text):
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode:
        raise ConvertError(f"{os.path.basename(cmd[0])} exited {r.returncode}: "
                           f"{last_line(r.stderr or r.stdout)}")


def extract(video, index, dest):
    """Copy the video's stream to dest, in the format its suffix names."""
    try:
        with av.open(video) as src, av.open(dest, "w") as dst:
            stream = src.streams[index]
            copy = dst.add_stream_from_template(stream)
            for packet in src.demux(stream):
                # The empty packet at the end, which flushes decoders.
                if packet.dts is None:
                    continue
                packet.stream = copy
                dst.mux(packet)
    except av.FFmpegError as exc:
        raise ConvertError(f"cannot extract stream {index}: {exc}") from exc


def ocr(video, index, codec, rules):
    """The cleaned .srt of the video's track. Raises Skip if it has no text."""
    with tempfile.TemporaryDirectory(prefix="image-subtitle-extractor-") as tmp:
        track = os.path.join(tmp, "track")
        if codec == "pgssub":
            extract(video, index, track + ".sup")
            ocr_input = track + ".sup"
        else:
            # FFmpeg cannot write idx/sub, so the track goes through Matroska.
            extract(video, index, track + ".mkv")
            run(["mkvextract", "-q", track + ".mkv", "tracks", f"0:{track}.sub"])
            ocr_input = track + ".idx"
        # cleanit applies the rules of the language the file name says.
        srt = f"{track}.{LANGUAGE}.srt"
        run(["subtile-ocr", "-l", LANGUAGE,
             "-c", f"tessedit_char_blacklist={OCR_BLACKLIST}",
             "-o", srt, ocr_input])
        try:
            subtitle = cleanit.Subtitle(srt)
            if subtitle.clean(rules):
                subtitle.save()
        except Exception as exc:
            raise ConvertError(f"cleanit: {exc}") from exc
        with open(srt, "rb") as f:
            data = f.read()
    if b" --> " not in data:
        raise Skip("OCR found no text")
    return data


class Work:
    """Videos to look at, first come first served, each once."""

    def __init__(self):
        self.videos = collections.OrderedDict()
        # Starts out set, for the scan at startup.
        self.rescan = True

    def add(self, videos):
        for video in videos:
            self.videos.setdefault(video, None)

    def pop(self):
        return self.videos.popitem(last=False)[0]


class Handler(pyinotify.ProcessEvent):

    def my_init(self, work):
        self.work = work

    def process_IN_CLOSE_WRITE(self, event):
        if is_video(event.pathname):
            self.work.add([event.pathname])

    def process_IN_CREATE(self, event):
        # Files written into a new directory before pyinotify watched it.
        if event.dir:
            self.work.add(videos_under(event.pathname))

    def process_IN_MOVED_TO(self, event):
        if event.dir:
            self.work.add(videos_under(event.pathname))
        elif is_video(event.pathname):
            self.work.add([event.pathname])

    def process_IN_DELETE(self, event):
        if not event.dir and event.pathname.lower().endswith(CAPTION_SUFFIXES):
            # Deleting captions asks for ours again. Looking at the other videos
            # in the directory too is cheap, as they are up to date.
            d = os.path.dirname(event.pathname)
            try:
                names = sorted(os.listdir(d))
            except OSError:
                return
            self.work.add(os.path.join(d, n) for n in names if is_video(n))

    def process_IN_Q_OVERFLOW(self, event):
        log.warning("inotify queue overflowed, rescanning")
        self.work.rescan = True


def remove_sidecar(video):
    srt = sidecar(video)
    if our_stamp(srt) is None:
        return
    try:
        os.unlink(srt)
        log.info("removed %s", srt)
    except OSError as exc:
        log.warning("cannot remove %s: %s", srt, exc)


def write_sidecar(video, st, data):
    srt = sidecar(video)
    # Not .srt, so minidlna ignores it until the rename.
    tmp = os.path.join(os.path.dirname(srt), "." + os.path.basename(srt) + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.setxattr(tmp, SOURCE_XATTR, stamp(st).encode())
    # Some may have arrived with the torrent while the OCR ran.
    if other_captions(video):
        os.unlink(tmp)
        return False
    os.replace(tmp, srt)
    return True


def process(video, rules):
    try:
        st = os.stat(video)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        return

    if other_captions(video):
        return
    srt = sidecar(video)
    if our_stamp(srt) == stamp(st):
        return

    started = time.monotonic()
    try:
        data = ocr(video, *image_track(video), rules)
        # If the video changed during the OCR, the .srt is stamped with the old
        # size and mtime, and the queued close_write writes it again.
        written = write_sidecar(video, st, data)
    except Skip as skip:
        remove_sidecar(video)
        log.info("skipping %s: %s", video, skip)
    except (ConvertError, OSError, subprocess.TimeoutExpired) as exc:
        log.error("ERROR: %s: %s", video, exc)
    else:
        if written:
            log.info("wrote %s (%d cues, %ds)", srt, data.count(b" --> "),
                     time.monotonic() - started)


def watch(wm):
    """(Re)add recursive watches. Adding an existing watch is harmless."""
    for p, wd in wm.add_watch(ROOT, MASK, rec=True, auto_add=True,
                              quiet=True).items():
        if wd < 0:
            log.warning("cannot watch %s", p)


def full_scan(work):
    videos = list(videos_under(ROOT))
    work.add(videos)
    log.info("scanned %d videos", len(videos))


def main():
    # cleanit would quietly fall back to its own rules without the file. Loaded
    # once, so a broken one stops the service at startup.
    if not os.path.isfile(CLEANIT_CONFIG):
        sys.exit(f"no such file: {CLEANIT_CONFIG}")
    rules = cleanit.Config.from_path(CLEANIT_CONFIG).select_rules(tags={"default"})

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
    # As a container's PID 1 SIGTERM would be ignored. Exiting from Python
    # rather than dying of it also kills a running OCR tool (subprocess.run
    # does on any exception) and removes its scratch directory.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    work = Work()
    wm = pyinotify.WatchManager()
    notifier = pyinotify.Notifier(wm, Handler(work=work))

    while True:
        # Block only when there is nothing to do; else just take what is there.
        if notifier.check_events(timeout=0 if work.videos or work.rescan else None):
            try:
                notifier.read_events()
            except UnicodeDecodeError:
                # pyinotify cannot decode a non-UTF-8 name, and the events read
                # with it are lost.
                log.warning("undecodable file name, rescanning")
                work.rescan = True
            notifier.process_events()
        if work.rescan:
            work.rescan = False
            # Watch first, so nothing changes unseen between the scan and the
            # watch.
            watch(wm)
            full_scan(work)
        if work.videos:
            process(work.pop(), rules)


if __name__ == "__main__":
    main()
