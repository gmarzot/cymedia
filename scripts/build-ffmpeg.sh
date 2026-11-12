#!/usr/bin/env bash
# Builds an FFmpeg with what cymedia's profiles need, into its own prefix.
# LGPL by default; --gpl adds libx264 and makes the whole build GPLv3.
set -euo pipefail

FFMPEG_VERSION=9.0.2
FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e
AOM_VERSION=3.15.3
AOM_SHA256=12c01dc731ed7b5a999e28120b45bf58a002ba3435a4ba2cfbc1e084a27c9c04
OPENSSL_VERSION=3.5.9
OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
SRT_COMMIT=899348d8318eb9a3c5a5b6ec43c4a1114288773a          # v1.5.7
NV_HEADERS_COMMIT=a807fd4b35442891306b7ba23a300581302fa76d  # n13.1.15.0
X264_COMMIT=b35605ace3ddf7c1a5d67a2eb553f034aef41d55        # stable

prefix=$HOME/.local/cymedia-ffmpeg
gpl=0
nvidia=1

usage() {
    cat <<USAGE
usage: $0 [--prefix DIR] [--gpl] [--no-nvidia]

  --prefix DIR  install here (default $prefix)
  --gpl         add libx264 for software H.264; the build becomes GPLv3
  --no-nvidia   on Linux, leave out NVENC, NVDEC and scale_cuda

Needs curl, git, make, cmake, perl, pkg-config and C and C++ compilers; nasm on
x86_64; clang on Linux for scale_cuda.
  Ubuntu: sudo apt-get install build-essential cmake git nasm clang pkg-config curl perl
  macOS:  xcode-select --install && brew install cmake pkg-config
USAGE
}

while [ $# -gt 0 ]; do
    case $1 in
        --prefix) prefix=$2; shift 2 ;;
        --gpl) gpl=1; shift ;;
        --no-nvidia) nvidia=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

os=$(uname -s)
[ "$os" = Linux ] || nvidia=0
mkdir -p "$prefix"
prefix=$(cd "$prefix" && pwd)
jobs=$(getconf _NPROCESSORS_ONLN)

tools=(curl git make cmake perl pkg-config cc c++ shasum)
[ "$(uname -m)" != x86_64 ] || tools+=(nasm)
[ "$nvidia" = 0 ] || tools+=(clang)
for t in "${tools[@]}"; do
    command -v "$t" >/dev/null || { echo "missing: $t" >&2; usage >&2; exit 1; }
done

work=$(mktemp -d "${TMPDIR:-/tmp}/cymedia-ffmpeg.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
export PKG_CONFIG_PATH=$prefix/lib/pkgconfig

fetch() {  # url sha256
    local file=${1##*/}
    curl -fsSL --retry 3 -o "$file" "$1"
    echo "$2  $file" | shasum -a 256 -c - >/dev/null
    tar xf "$file"
}

fetch_git() {  # url commit dir
    git init -q "$3"
    git -C "$3" fetch -q --depth 1 "$1" "$2"
    git -C "$3" -c advice.detachedHead=false checkout -q FETCH_HEAD
}

echo "== libaom $AOM_VERSION"
fetch "https://storage.googleapis.com/aom-releases/libaom-$AOM_VERSION.tar.gz" "$AOM_SHA256"
cmake -S "libaom-$AOM_VERSION" -B aom-build -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=OFF \
    -DENABLE_DOCS=0 -DENABLE_EXAMPLES=0 -DENABLE_TESTS=0 -DENABLE_TOOLS=0 >/dev/null
cmake --build aom-build -j "$jobs" >/dev/null
cmake --install aom-build >/dev/null

# Static and PIC, linked into FFmpeg's shared libraries, which export only their own symbols.
echo "== OpenSSL $OPENSSL_VERSION"
fetch "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz" \
    "$OPENSSL_SHA256"
# --openssldir: verify peers against the system's CA certificates.
(cd "openssl-$OPENSSL_VERSION" && ./Configure --prefix="$prefix" --libdir=lib --openssldir=/etc/ssl \
    no-shared no-tests no-docs -fPIC >/dev/null && make -j "$jobs" build_sw >/dev/null \
    && make install_sw >/dev/null)

echo "== libsrt"
fetch_git https://github.com/Haivision/srt.git "$SRT_COMMIT" srt
cmake -S srt -B srt-build -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$prefix" -DCMAKE_INSTALL_LIBDIR=lib \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DENABLE_SHARED=OFF -DENABLE_STATIC=ON \
    -DENABLE_APPS=OFF -DOPENSSL_ROOT_DIR="$prefix" -DOPENSSL_USE_STATIC_LIBS=ON >/dev/null
cmake --build srt-build -j "$jobs" >/dev/null
cmake --install srt-build >/dev/null

config=(--prefix="$prefix" --enable-shared --disable-static --enable-rpath --disable-doc
        --disable-autodetect --pkg-config-flags=--static --enable-libaom
        --enable-openssl --enable-libsrt)

if [ "$gpl" = 1 ]; then
    echo "== x264"
    fetch_git https://code.videolan.org/videolan/x264.git "$X264_COMMIT" x264
    (cd x264 && ./configure --prefix="$prefix" --enable-static --enable-pic --disable-cli \
        >/dev/null && make -j "$jobs" >/dev/null && make install >/dev/null)
    config+=(--enable-gpl --enable-version3 --enable-libx264)  # GPL takes OpenSSL 3 only as v3
fi

if [ "$nvidia" = 1 ]; then
    echo "== nv-codec-headers"
    fetch_git https://github.com/FFmpeg/nv-codec-headers.git "$NV_HEADERS_COMMIT" nv-headers
    make -C nv-headers PREFIX="$prefix" install >/dev/null
    config+=(--enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid --enable-cuda-llvm)
fi

[ "$os" != Darwin ] || config+=(--enable-videotoolbox)

echo "== FFmpeg $FFMPEG_VERSION"
fetch "https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VERSION.tar.xz" "$FFMPEG_SHA256"
(cd "ffmpeg-$FFMPEG_VERSION" && ./configure "${config[@]}" >/dev/null \
    && make -j "$jobs" >/dev/null && make install >/dev/null)

cat > "$prefix/env.sh" <<ENV
export PATH="$prefix/bin:\$PATH"
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig\${PKG_CONFIG_PATH:+:\$PKG_CONFIG_PATH}"
ENV

cat <<DONE

Built $("$prefix/bin/ffmpeg" -hide_banner -version | head -1 | cut -d' ' -f1-3) in $prefix

Install cymedia against it:
    source $prefix/env.sh
    pip install --no-cache-dir --no-binary cymedia cymedia
DONE
