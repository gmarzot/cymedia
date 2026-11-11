# cython: language_level=3
"""Binding for the cymedia graph engine."""
import logging

from cpython.bytes cimport PyBytes_FromStringAndSize
from cpython.pycapsule cimport PyCapsule_GetPointer, PyCapsule_IsValid, PyCapsule_New
from libc.stdint cimport int32_t, int64_t, uint8_t, uint32_t, uint64_t
from libc.stdlib cimport free, malloc
from libc.string cimport memset

from cymedia._types import Output, Packet, StreamInfo

log = logging.getLogger("cymedia")


cdef extern from "cymedia_sink.h" nogil:
    enum:
        CYMEDIA_KEY
        CYMEDIA_GROUP_START
        CYMEDIA_EV_ERROR
        CYMEDIA_EV_GROUP_ABANDONED
        CYMEDIA_EV_INPUT_DROPPED
        CYMEDIA_EV_END
    const char *CYMEDIA_SINK_CAPSULE
    ctypedef struct cymedia_meta:
        uint64_t group
        uint64_t obj "object"
        int64_t pts_us
        uint32_t flags
        uint32_t branch
    ctypedef struct cymedia_sink:
        uint32_t abi
        uint32_t size


cdef extern from "engine.h" nogil:
    ctypedef struct cym_graph:
        pass
    ctypedef struct cym_branch_cfg:
        const char *filter
        const char *encoder
        const char *options
        int strip_filler
    ctypedef struct cym_graph_cfg:
        const char *decoder
        const char *hwaccel
        const char *device
        const uint8_t *extradata
        int extradata_size
        int fps_num
        int fps_den
        int extra_hw_frames
        int input_depth
        int input_max_ms
        int queue_depth
        int realtime
        int nbranches
        const cym_branch_cfg *branches
        cymedia_sink sink
    ctypedef struct cym_branch_stats:
        uint64_t frames_in
        uint64_t frames_out
        uint64_t frames_dropped
        uint64_t groups_abandoned
        uint64_t errors
    ctypedef struct cym_graph_stats:
        uint64_t packets_in
        uint64_t packets_dropped
        uint64_t frames_decoded
        uint64_t decode_errors

    cym_graph *cym_graph_create(const cym_graph_cfg *cfg, char *err, size_t errlen)
    int cym_graph_push(cym_graph *g, const uint8_t *data, size_t size, uint64_t group,
                       uint64_t obj, int64_t pts_us, int key, int block)
    int cym_graph_flush(cym_graph *g, int timeout_ms)
    void cym_graph_destroy(cym_graph *g)
    void cym_graph_get_stats(cym_graph *g, cym_graph_stats *out)
    void cym_branch_get_stats(cym_graph *g, int branch, cym_branch_stats *out)
    int cym_branch_delays(cym_graph *g, int branch, int64_t *out, int cap)
    int cym_have_encoder(const char *name)
    int cym_have_decoder(const char *name)
    int cym_have_filter(const char *name)
    int cym_hw_available(const char *type, const char *device)


cdef extern from "source.h" nogil:
    enum:
        CYM_SOURCE_VIDEO
        CYM_SOURCE_AUDIO
    ctypedef struct cym_source:
        pass
    ctypedef struct cym_stream_info:
        int present
        char codec[32]
        const uint8_t *extradata
        int extradata_size
        int length_prefixed
        int width
        int height
        int fps_num
        int fps_den
        int sample_rate
        int channels
        int64_t bit_rate
    ctypedef struct cym_source_stats:
        uint64_t video_packets
        uint64_t audio_packets
        uint64_t before_key
        uint64_t graph_dropped
        uint64_t bytes
    cym_source *cym_source_open(const char *url, const char *options, int timeout_ms, char *err,
                                size_t errlen)
    void cym_source_info(const cym_source *s, int stream, cym_stream_info *out)
    int cym_source_start(cym_source *s, cym_graph *graph, cymedia_sink sink, int passthrough,
                         int pace)
    void cym_source_get_stats(cym_source *s, cym_source_stats *out)
    void cym_source_close(cym_source *s)
    int cym_have_protocol(const char *name)
    int cym_have_format(const char *name)


cdef extern from "qsink.h" nogil:
    cdef struct cym_qitem:
        cym_qitem *next
        int is_event
        cymedia_meta meta
        const uint8_t *data
        size_t size
        int32_t code
        uint32_t branch
        uint64_t group
        char msg[256]
    ctypedef struct cym_qsink:
        pass
    cym_qsink *cym_qsink_create()
    void cym_qsink_destroy(cym_qsink *q)
    cymedia_sink cym_qsink_sink(cym_qsink *q)
    int cym_qsink_fd(cym_qsink *q)
    cym_qitem *cym_qsink_take(cym_qsink *q, int timeout_ms)
    void cym_qitem_free(cym_qitem *it)
    cymedia_sink *cym_counting_sink_new()
    uint64_t cym_counting_sink_frames(const cymedia_sink *s)
    void cym_counting_sink_free(cymedia_sink *s)


cdef extern from "libavcodec/avcodec.h" nogil:
    unsigned avcodec_version()
    const char *avcodec_license()
    const char *avcodec_configuration()


cdef extern from "libavutil/avutil.h" nogil:
    const char *av_version_info()


cdef extern from "libavutil/log.h" nogil:
    void av_log_set_level(int level)


cdef bytes _b(s):
    return None if s is None else str(s).encode()


cdef bytes _opts(options):
    return "\n".join(f"{k}={v}" for k, v in dict(options or {}).items()).encode()


cdef int _ms(timeout):
    return -1 if timeout is None else max(0, int(timeout * 1000))


cdef class Graph:
    """Decodes one source once and feeds every branch.

    Each branch is a libavfilter chain ending in an encoder, run on its own thread. Outputs carry
    the source's group, object and timestamp, and every branch starts a keyframe at each source
    group start. With ``realtime`` a full queue drops: input resumes at the next group start, and
    a branch that falls behind abandons the rest of the group. Without it, queues block. A packet
    that has waited ``input_max_ms`` to be decoded is dropped with the rest of its group (0: no
    time bound), from when every encoder has opened.
    """

    cdef cym_graph *_g
    cdef cym_qsink *_q
    cdef object _capsule
    cdef object _source
    cdef readonly int nbranches
    cdef readonly bint realtime

    def __cinit__(self, branches, *, decoder="h264", hwaccel=None, device=None, extradata=None,
                  fps=(60, 1), extra_hw_frames=8, input_depth=240, input_max_ms=500, queue_depth=4,
                  realtime=True, sink=None):
        cdef cym_graph_cfg cfg
        cdef cym_branch_cfg *bc = NULL
        cdef cym_graph *g = NULL
        cdef char err[256]
        cdef const uint8_t[::1] xd
        cdef Py_ssize_t i
        self._g = NULL
        self._q = NULL
        branches = list(branches)
        if not branches:
            raise ValueError("a graph needs at least one branch")
        if sink is not None and not PyCapsule_IsValid(sink, CYMEDIA_SINK_CAPSULE):
            raise TypeError("sink must be a 'cymedia.sink' capsule")
        memset(&cfg, 0, sizeof(cfg))
        memset(err, 0, sizeof(err))
        keep = []
        bc = <cym_branch_cfg *>malloc(len(branches) * sizeof(cym_branch_cfg))
        if bc == NULL:
            raise MemoryError()
        try:
            for i in range(len(branches)):
                br = branches[i]
                f, e, o = _b(br.filter), _b(br.encoder), _opts(br.options)
                keep += [f, e, o]
                bc[i].filter = f
                bc[i].encoder = e
                bc[i].options = o
                bc[i].strip_filler = bool(br.strip_filler)
            d, h, dv = _b(decoder), _b(hwaccel), _b(device)
            keep += [d, h, dv]
            cfg.decoder = d
            cfg.hwaccel = NULL if h is None else <const char *>h
            cfg.device = NULL if dv is None else <const char *>dv
            if extradata:
                xd = bytes(extradata)
                cfg.extradata = &xd[0]
                cfg.extradata_size = xd.shape[0]
            cfg.fps_num, cfg.fps_den = int(fps[0]), int(fps[1])
            cfg.extra_hw_frames = extra_hw_frames
            cfg.input_depth = input_depth
            cfg.input_max_ms = input_max_ms
            cfg.queue_depth = queue_depth
            cfg.realtime = bool(realtime)
            cfg.nbranches = len(branches)
            cfg.branches = bc
            if sink is None:
                self._q = cym_qsink_create()
                if self._q == NULL:
                    raise MemoryError()
                cfg.sink = cym_qsink_sink(self._q)
            else:
                cfg.sink = (<cymedia_sink *>PyCapsule_GetPointer(sink, CYMEDIA_SINK_CAPSULE))[0]
                self._capsule = sink
            with nogil:
                g = cym_graph_create(&cfg, err, sizeof(err))
        finally:
            free(bc)
        if g == NULL:
            if self._q != NULL:
                cym_qsink_destroy(self._q)
                self._q = NULL
            raise ValueError(err.decode(errors="replace"))
        self._g = g
        self.nbranches = len(branches)
        self.realtime = bool(realtime)

    def push(self, data, uint64_t group, uint64_t obj, int64_t pts_us, bint key, block=None):
        """Queues one encoded source frame; returns False when it was dropped."""
        cdef const uint8_t[::1] view = data
        cdef int blk = (not self.realtime) if block is None else bool(block)
        cdef int r
        if self._g == NULL:
            raise RuntimeError("graph is closed")
        if view.shape[0] == 0:
            raise ValueError("empty frame")
        with nogil:
            r = cym_graph_push(self._g, &view[0], view.shape[0], group, obj, pts_us, key, blk)
        if r < 0:
            raise RuntimeError("graph is flushing or failed")
        return r == 0

    def read(self, timeout=None):
        """Returns the outputs queued so far, waiting up to ``timeout`` seconds for the first."""
        cdef cym_qitem *head
        cdef cym_qitem *it
        cdef int ms = _ms(timeout)
        if self._q == NULL:
            raise RuntimeError("outputs go to the sink passed at construction")
        with nogil:
            head = cym_qsink_take(self._q, ms)
        out = []
        it = head
        try:
            while it != NULL:
                if it.is_event:
                    _log_event(it.branch, it.code, it.group, it.msg)
                else:
                    out.append(Output(
                        it.meta.branch, it.meta.group, it.meta.obj, it.meta.pts_us,
                        bool(it.meta.flags & CYMEDIA_KEY), bool(it.meta.flags & CYMEDIA_GROUP_START),
                        PyBytes_FromStringAndSize(<const char *>it.data, it.size)))
                it = it.next
        finally:
            cym_qitem_free(head)
        return out

    def fileno(self):
        """A descriptor that is readable while outputs are queued."""
        if self._q == NULL:
            raise RuntimeError("outputs go to the sink passed at construction")
        return cym_qsink_fd(self._q)

    def flush(self, timeout=None):
        """Ends input and waits for every branch to emit its last frame; False on timeout."""
        cdef int ms = _ms(timeout)
        cdef int r
        if self._g == NULL:
            raise RuntimeError("graph is closed")
        with nogil:
            r = cym_graph_flush(self._g, ms)
        return r == 0

    def stats(self):
        cdef cym_graph_stats gs
        cdef cym_branch_stats bs
        if self._g == NULL:
            raise RuntimeError("graph is closed")
        cym_graph_get_stats(self._g, &gs)
        branches = []
        for i in range(self.nbranches):
            cym_branch_get_stats(self._g, i, &bs)
            branches.append(dict(frames_in=bs.frames_in, frames_out=bs.frames_out,
                                 frames_dropped=bs.frames_dropped,
                                 groups_abandoned=bs.groups_abandoned, errors=bs.errors))
        return dict(packets_in=gs.packets_in, packets_dropped=gs.packets_dropped,
                    frames_decoded=gs.frames_decoded, decode_errors=gs.decode_errors,
                    branches=branches)

    def delays(self, int branch):
        """The most recent push-to-output delays of a branch, in nanoseconds, oldest first."""
        cdef int cap = 8192
        cdef int n
        cdef int64_t *buf
        if self._g == NULL:
            raise RuntimeError("graph is closed")
        buf = <int64_t *>malloc(cap * sizeof(int64_t))
        if buf == NULL:
            raise MemoryError()
        try:
            n = cym_branch_delays(self._g, branch, buf, cap)
            return [buf[i] for i in range(n)]
        finally:
            free(buf)

    def close(self):
        if self._source is not None:
            self._source.close()
            self._source = None
        cdef cym_graph *g = self._g
        self._g = NULL
        if g != NULL:
            with nogil:
                cym_graph_destroy(g)
        if self._q != NULL:
            cym_qsink_destroy(self._q)
            self._q = NULL

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __dealloc__(self):
        self.close()


_STREAMS = ("video", "audio")


cdef object _stream_info(cym_source *s, int stream):
    cdef cym_stream_info i
    cym_source_info(s, stream, &i)
    if not i.present:
        return None
    xd = (PyBytes_FromStringAndSize(<const char *>i.extradata, i.extradata_size)
          if i.extradata_size else None)
    return StreamInfo(codec=i.codec.decode(), extradata=xd,
                      length_prefixed=bool(i.length_prefixed), width=i.width, height=i.height,
                      fps=(i.fps_num, i.fps_den) if i.fps_num > 0 else None,
                      sample_rate=i.sample_rate, channels=i.channels, bit_rate=i.bit_rate)


def _is_file(url):
    url = str(url)
    return "://" not in url or url.startswith("file:")


cdef class Source:
    """Reads an input with FFmpeg's libavformat on a native thread: a file, or a live SRT, RTMP,
    RTSP, HLS or UDP stream. ``options`` are libavformat options, e.g. ``{"listen": "1"}`` to be
    an RTMP server; ``timeout`` bounds the open, in seconds.

    The best video stream and the best audio stream are read. Video groups start at key frames,
    and every audio frame is a group of its own. Timestamps are microseconds from the first packet.
    H.264 comes out length-prefixed, with an avcC as ``video.extradata``.
    """

    cdef cym_source *_s
    cdef cym_qsink *_q
    cdef readonly object url
    cdef readonly object video
    cdef readonly object audio
    cdef readonly bint ended
    cdef readonly object error

    def __cinit__(self, url, options=None, *, timeout=None):
        cdef bytes u = _b(url)
        cdef bytes o = _opts(options)
        cdef const char *up = u
        cdef const char *op = o
        cdef int ms = _ms(timeout)
        cdef char err[256]
        cdef cym_source *s
        self._s = NULL
        self._q = NULL
        memset(err, 0, sizeof(err))
        with nogil:
            s = cym_source_open(up, op, ms, err, sizeof(err))
        if s == NULL:
            raise OSError(err.decode(errors="replace"))
        self._s = s
        self.url = url
        self.video = _stream_info(s, CYM_SOURCE_VIDEO)
        self.audio = _stream_info(s, CYM_SOURCE_AUDIO)

    def start(self, Graph graph=None, *, passthrough=True, pace=None):
        """Starts reading. ``graph`` decodes the video natively, from the first key frame, and then
        takes no push() from Python; closing it closes this source. read() returns the audio and,
        with ``passthrough``, the video. ``pace`` holds each packet to its timestamp; by default
        files are paced and network inputs are not."""
        cdef cymedia_sink sink
        cdef cym_graph *g = NULL
        cdef int pc = _is_file(self.url) if pace is None else bool(pace)
        if self._s == NULL:
            raise RuntimeError("source is closed")
        if self._q != NULL:
            raise RuntimeError("source already started")
        if graph is not None:
            if graph._g == NULL:
                raise RuntimeError("graph is closed")
            if graph._source is not None:
                raise RuntimeError("graph already has a source")
            g = graph._g
        self._q = cym_qsink_create()
        if self._q == NULL:
            raise MemoryError()
        sink = cym_qsink_sink(self._q)
        if cym_source_start(self._s, g, sink, bool(passthrough), pc) != 0:
            raise RuntimeError("source did not start")
        if graph is not None:
            graph._source = self

    def read(self, timeout=None):
        """Returns the packets read so far, waiting up to ``timeout`` seconds for the first.
        ``ended`` is set once the input ends or fails; ``error`` says why it failed."""
        cdef cym_qitem *head
        cdef cym_qitem *it
        cdef int ms = _ms(timeout)
        if self._q == NULL:
            raise RuntimeError("source is not started")
        with nogil:
            head = cym_qsink_take(self._q, ms)
        out = []
        it = head
        try:
            while it != NULL:
                if it.is_event:
                    text = it.msg.decode(errors="replace")
                    if it.code == CYMEDIA_EV_END:
                        self.ended = True
                        log.info("source %s: %s", self.url, text)
                    elif it.code == CYMEDIA_EV_ERROR:
                        self.ended = True
                        self.error = text
                        log.error("source %s: %s", self.url, text)
                else:
                    out.append(Packet(
                        _STREAMS[it.meta.branch], it.meta.group, it.meta.obj, it.meta.pts_us,
                        bool(it.meta.flags & CYMEDIA_KEY),
                        PyBytes_FromStringAndSize(<const char *>it.data, it.size)))
                it = it.next
        finally:
            cym_qitem_free(head)
        return out

    def fileno(self):
        """A descriptor that is readable while packets are queued."""
        if self._q == NULL:
            raise RuntimeError("source is not started")
        return cym_qsink_fd(self._q)

    def stats(self):
        cdef cym_source_stats st
        if self._s == NULL:
            raise RuntimeError("source is closed")
        cym_source_get_stats(self._s, &st)
        return dict(video_packets=st.video_packets, audio_packets=st.audio_packets,
                    before_key=st.before_key, graph_dropped=st.graph_dropped, bytes=st.bytes)

    def close(self):
        """Stops reading, interrupting a blocked read."""
        cdef cym_source *s = self._s
        self._s = NULL
        if s != NULL:
            with nogil:
                cym_source_close(s)
        if self._q != NULL:
            cym_qsink_destroy(self._q)
            self._q = NULL

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __dealloc__(self):
        self.close()


def have_protocol(name):
    """True when the linked libavformat reads this protocol, e.g. "srt" or "rtmp"."""
    return bool(cym_have_protocol(_b(name)))


def have_format(name):
    """True when the linked libavformat has this input format, e.g. "rtsp" or "hls"."""
    return bool(cym_have_format(_b(name)))


def _log_event(branch, code, group, msg):
    text = msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg)
    where = "graph" if branch == 0xFFFFFFFF else f"branch {branch}"
    if code == CYMEDIA_EV_ERROR:
        log.error("%s: %s", where, text)
    elif code == CYMEDIA_EV_GROUP_ABANDONED:
        log.warning("%s: abandoned group %d: %s", where, group, text)
    elif code == CYMEDIA_EV_INPUT_DROPPED:
        log.warning("input dropped in group %d: %s", group, text)
    else:
        log.debug("%s: %s", where, text)


def have_encoder(name):
    return bool(cym_have_encoder(_b(name)))


def have_decoder(name):
    return bool(cym_have_decoder(_b(name)))


def have_filter(name):
    return bool(cym_have_filter(_b(name)))


def hw_available(type, device=None):
    """True when a hardware device of this type (e.g. "cuda") opens."""
    cdef bytes t = _b(type)
    cdef bytes dv = _b(device)
    cdef const char *tp = t
    cdef const char *dp = NULL if dv is None else <const char *>dv
    cdef int r
    with nogil:
        r = cym_hw_available(tp, dp)
    return bool(r)


def ffmpeg_info():
    """The linked FFmpeg's version, license and configure line."""
    v = avcodec_version()
    return dict(version=av_version_info().decode(), license=avcodec_license().decode(),
                libavcodec=f"{v >> 16}.{(v >> 8) & 0xff}.{v & 0xff}",
                configuration=avcodec_configuration().decode())


def set_ffmpeg_log_level(int level):
    """Sets FFmpeg's global log level (AV_LOG_* values: 16 error, 24 warning, 32 info)."""
    av_log_set_level(level)


cdef void _free_counting(object cap) noexcept:
    cym_counting_sink_free(<cymedia_sink *>PyCapsule_GetPointer(cap, CYMEDIA_SINK_CAPSULE))


def _counting_sink():
    """A native sink that counts and releases frames, for testing the capsule path."""
    cdef cymedia_sink *s = cym_counting_sink_new()
    if s == NULL:
        raise MemoryError()
    return PyCapsule_New(s, CYMEDIA_SINK_CAPSULE, _free_counting)


def _counting_sink_frames(cap):
    if not PyCapsule_IsValid(cap, CYMEDIA_SINK_CAPSULE):
        raise TypeError("not a 'cymedia.sink' capsule")
    return cym_counting_sink_frames(<cymedia_sink *>PyCapsule_GetPointer(cap, CYMEDIA_SINK_CAPSULE))
