FROM debian:trixie-slim

# subtile-ocr is only in testing, but its dependencies are all in trixie, so
# just that package comes from testing, pinned below everything else so apt
# never prefers testing on its own. If a later build of it needed newer
# libraries, apt would take those from testing too; the check fails the build
# instead, before the image turns into a mix of releases.
#
# Tesseract's English data only, for the script's LANGUAGE. Not tesseract-ocr,
# the CLI, which subtile-ocr does not use.
RUN set -eux; \
    echo 'deb http://deb.debian.org/debian testing main' \
        > /etc/apt/sources.list.d/testing.list; \
    printf 'Package: *\nPin: release a=testing\nPin-Priority: 100\n' \
        > /etc/apt/preferences.d/testing; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        mkvtoolnix \
        python3 \
        python3-pip \
        python3-pyinotify \
        subtile-ocr/testing \
        tesseract-ocr-eng; \
    from_testing=$(apt list --installed 2>/dev/null | grep /testing | cut -d/ -f1); \
    if [ "$from_testing" != subtile-ocr ]; then \
        echo "installed from testing: $from_testing" >&2; exit 1; \
    fi; \
    rm /etc/apt/sources.list.d/testing.list /etc/apt/preferences.d/testing; \
    rm -rf /var/lib/apt/lists/*

# cleanit is not packaged. PyAV is, but Debian's needs libavdevice, which
# brings SDL, Mesa and LLVM: about 450 MB of the image. The wheel has its own
# FFmpeg, with the formats and codecs needed here.
#
# Each is kept below its next release with API changes; the rest, and their
# unpinned dependencies, arrive with the rebuilds. pip installs into
# /usr/local, beside Debian's modules rather than over them, and none of the
# dependencies is a Debian package here. Nothing runs apt after this, which is
# what --break-system-packages warns about.
RUN pip install --break-system-packages --no-cache-dir --root-user-action=ignore \
        "av~=19.0" \
        "cleanit~=0.5.1"

COPY cleanit.yml /usr/local/share/image-subtitle-extractor/
COPY --chmod=755 image-subtitle-extractor.py /usr/local/bin/image-subtitle-extractor

# OCR keeps every core busy for half a minute per episode; whatever else the
# host runs comes first.
ENTRYPOINT ["nice", "-n", "10", "image-subtitle-extractor"]
