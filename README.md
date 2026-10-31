# image-subtitle-extractor

Watches `/videos` for videos (`.mkv`, `.m4v`, `.mp4`) and writes an `.srt`
next to each one whose only subtitles are images, PGS (Blu-ray) or VobSub
(DVD), by OCR.

Some players cannot draw image subtitles, LG's webOS among them, and DLNA
servers like minidlna do not transcode them. minidlna does serve a
`<video>.srt` beside the video, though, and the TV shows that.

The track is extracted with PyAV (and mkvextract for VobSub), read by
[subtile-ocr](https://github.com/gwen-lg/subtile-ocr) with Tesseract, and
cleaned by [cleanit](https://github.com/ratoaq2/cleanit) with its own rules
plus those in [`cleanit.yml`](cleanit.yml), for how OCR misreads a lone
capital I.

## Behaviour

- A video is converted as soon as it is written or renamed into place, e.g.
  when transmission drops the `.part` suffix of a finished file. That happens
  per file, so the first episode of a season pack gets its subtitles while the
  rest is still downloading. New and moved-in directories are scanned, and so
  is everything at startup and after the kernel's inotify queue overflowed.
- One video at a time, first come first served. A video that comes up again
  while it waits keeps its place, so one written over and over cannot hold up
  the others.
- An `.srt` it wrote carries the `user.image-subtitle-extractor.source` xattr.
  Only those are ever rewritten (when the video changes) or removed (when the
  changed video needs none). A video with captions of its own that minidlna
  would serve, `<video>.srt` or `<video>.smi`, is skipped and those stay as
  they are. Renaming or deleting a video leaves its `.srt` behind; a renamed
  video gets a new one. Delete one of ours to have it written again.
- A video with text subtitles already, without an English image track, or
  whose OCR failed, is skipped until it changes or the container restarts.

## Setup

```yaml
services:
  image-subtitle-extractor:
    image: ghcr.io/snoack/image-subtitle-extractor:latest
    restart: unless-stopped
    # The owner of the videos, so the .srt files get the same.
    user: "1000:1000"
    volumes:
      - /srv/downloads:/videos
      # More directories go below it.
      - /srv/movies:/videos/movies
```

The filesystem must support user xattrs. Only English subtitles are read. To
change the cleanit rules, mount your own file over
`/usr/local/share/image-subtitle-extractor/cleanit.yml`.
