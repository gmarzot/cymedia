#define _POSIX_C_SOURCE 200809L
#include "source.h"

#include <limits.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libavformat/avformat.h>
#include <libavutil/dict.h>
#include <libavutil/error.h>
#include <libavutil/mathematics.h>

struct cym_source {
    AVFormatContext *fmt;
    int idx[2];                 /* stream index per CYM_SOURCE_*, -1 when absent */
    cym_stream_info info[2];
    uint8_t *xd[2];
    int annexb;                 /* H.264 in Annex B, converted to length prefixes */
    int64_t deadline_ns;        /* bounds the open; 0 for none */
    int closing;
    int started;
    pthread_t thread;

    cym_graph *graph;
    cymedia_sink sink;
    int passthrough, pace;

    /* reader thread */
    uint8_t *scratch;
    size_t scratch_cap;
    int64_t origin_us, t0_ns;
    int have_group, graph_ok;
    uint64_t group, object, audio_n;

    pthread_mutex_t mu;
    cym_source_stats st;
};

typedef struct srcbuf {
    cymedia_buf buf;
    uint8_t data[];
} srcbuf;

#define SSTAT(s, field, delta)              \
    do {                                    \
        pthread_mutex_lock(&(s)->mu);       \
        (s)->st.field += (delta);           \
        pthread_mutex_unlock(&(s)->mu);     \
    } while (0)

static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static int interrupted(void *opaque)
{
    cym_source *s = opaque;
    if (__atomic_load_n(&s->closing, __ATOMIC_ACQUIRE))
        return 1;
    return s->deadline_ns && now_ns() > s->deadline_ns;
}

static void averr(char *err, size_t errlen, const char *what, int r)
{
    char msg[AV_ERROR_MAX_STRING_SIZE];
    av_strerror(r, msg, sizeof msg);
    snprintf(err, errlen, "%s: %s", what, msg);
}

/* The next NAL unit of an Annex B buffer from *pos, without trailing zero bytes; 0 at the end. */
static int next_nal(const uint8_t *d, size_t n, size_t *pos, const uint8_t **nal, size_t *len)
{
    size_t i = *pos;
    while (i + 3 <= n && !(d[i] == 0 && d[i + 1] == 0 && d[i + 2] == 1))
        i++;
    if (i + 3 > n)
        return 0;
    size_t a = i + 3, b = a;
    while (b + 3 <= n && !(d[b] == 0 && d[b + 1] == 0 && d[b + 2] == 1))
        b++;
    if (b + 3 > n)
        b = n;
    *pos = b;
    while (b > a && d[b - 1] == 0)
        b--;
    *nal = d + a;
    *len = b - a;
    return 1;
}

/* A minimal avcC (one SPS, one PPS) from Annex B parameter sets. */
static int build_avcc(const uint8_t *d, size_t n, uint8_t **out, int *outn)
{
    const uint8_t *sps = NULL, *pps = NULL, *nal;
    size_t sl = 0, pl = 0, len, pos = 0;
    while (next_nal(d, n, &pos, &nal, &len)) {
        int t = len ? nal[0] & 0x1F : 0;
        if (t == 7 && !sps && len >= 4) {
            sps = nal;
            sl = len;
        } else if (t == 8 && !pps) {
            pps = nal;
            pl = len;
        }
    }
    if (!sps || !pps || sl > 0xFFFF || pl > 0xFFFF)
        return -1;
    uint8_t *b = malloc(11 + sl + pl);
    if (!b)
        return -1;
    b[0] = 1;
    b[1] = sps[1];
    b[2] = sps[2];
    b[3] = sps[3];
    b[4] = 0xFF;
    b[5] = 0xE1;
    b[6] = (uint8_t)(sl >> 8);
    b[7] = (uint8_t)sl;
    memcpy(b + 8, sps, sl);
    b[8 + sl] = 1;
    b[9 + sl] = (uint8_t)(pl >> 8);
    b[10 + sl] = (uint8_t)pl;
    memcpy(b + 11 + sl, pps, pl);
    *out = b;
    *outn = (int)(11 + sl + pl);
    return 0;
}

/* Annex B to 4-byte length prefixes in s->scratch, without access unit delimiters and filler. */
static int to_length_prefixed(cym_source *s, const uint8_t *d, size_t n, size_t *outn)
{
    size_t need = n + n / 4 + 8;
    if (need > s->scratch_cap) {
        uint8_t *p = realloc(s->scratch, need);
        if (!p)
            return -1;
        s->scratch = p;
        s->scratch_cap = need;
    }
    const uint8_t *nal;
    size_t len, pos = 0, o = 0;
    while (next_nal(d, n, &pos, &nal, &len)) {
        int t = len ? nal[0] & 0x1F : 0;
        if (!len || t == 9 || t == 12)
            continue;
        s->scratch[o] = (uint8_t)(len >> 24);
        s->scratch[o + 1] = (uint8_t)(len >> 16);
        s->scratch[o + 2] = (uint8_t)(len >> 8);
        s->scratch[o + 3] = (uint8_t)len;
        memcpy(s->scratch + o + 4, nal, len);
        o += 4 + len;
    }
    *outn = o;
    return 0;
}

static uint8_t *dup_bytes(const uint8_t *d, int n)
{
    uint8_t *p = n > 0 ? malloc((size_t)n) : NULL;
    if (p)
        memcpy(p, d, (size_t)n);
    return p;
}

static int video_info(cym_source *s, const AVStream *st, char *err, size_t errlen)
{
    const AVCodecParameters *p = st->codecpar;
    cym_stream_info *in = &s->info[CYM_SOURCE_VIDEO];
    in->present = 1;
    snprintf(in->codec, sizeof in->codec, "%s", avcodec_get_name(p->codec_id));
    in->width = p->width;
    in->height = p->height;
    in->bit_rate = p->bit_rate;
    AVRational fr = st->r_frame_rate; /* nominal; the average drifts with edits and rounding */
    if (fr.num <= 0 || fr.den <= 0 || av_q2d(fr) > 240)
        fr = st->avg_frame_rate;
    if (fr.num > 0 && fr.den > 0)
        av_reduce(&in->fps_num, &in->fps_den, fr.num, fr.den, INT_MAX);
    const uint8_t *xd = p->extradata;
    int n = p->extradata_size;
    int annexb_xd = n >= 3 && xd[0] == 0 && xd[1] == 0;
    if (p->codec_id == AV_CODEC_ID_H264 && !(n > 0 && xd[0] == 1)) {
        if (build_avcc(xd, (size_t)n, &s->xd[0], &in->extradata_size) < 0) {
            snprintf(err, errlen, "no H.264 SPS and PPS in the stream header");
            return -1;
        }
        s->annexb = 1;
        in->length_prefixed = 1;
    } else if (n > 0) {
        if (!(s->xd[0] = dup_bytes(xd, n))) {
            snprintf(err, errlen, "out of memory");
            return -1;
        }
        in->extradata_size = n;
        in->length_prefixed =
            (p->codec_id == AV_CODEC_ID_H264 || p->codec_id == AV_CODEC_ID_HEVC) && !annexb_xd;
    }
    in->extradata = s->xd[0];
    return 0;
}

static void audio_info(cym_source *s, const AVStream *st)
{
    const AVCodecParameters *p = st->codecpar;
    cym_stream_info *in = &s->info[CYM_SOURCE_AUDIO];
    in->present = 1;
    snprintf(in->codec, sizeof in->codec, "%s", avcodec_get_name(p->codec_id));
    in->sample_rate = p->sample_rate;
    in->channels = p->ch_layout.nb_channels;
    in->bit_rate = p->bit_rate;
    if ((s->xd[1] = dup_bytes(p->extradata, p->extradata_size)))
        in->extradata_size = p->extradata_size;
    in->extradata = s->xd[1];
}

static pthread_once_t network_once = PTHREAD_ONCE_INIT;

static void network_init(void)
{
    avformat_network_init();
}

cym_source *cym_source_open(const char *url, const char *options, int timeout_ms, char *err,
                            size_t errlen)
{
    pthread_once(&network_once, network_init);
    cym_source *s = calloc(1, sizeof *s);
    if (!s) {
        snprintf(err, errlen, "out of memory");
        return NULL;
    }
    s->idx[0] = s->idx[1] = -1;
    pthread_mutex_init(&s->mu, NULL);
    AVDictionary *opts = NULL;
    if (options && *options && av_dict_parse_string(&opts, options, "=", "\n", 0) < 0) {
        snprintf(err, errlen, "options are not key=value lines");
        goto fail;
    }
    if (!(s->fmt = avformat_alloc_context())) {
        snprintf(err, errlen, "out of memory");
        goto fail;
    }
    s->fmt->interrupt_callback.callback = interrupted;
    s->fmt->interrupt_callback.opaque = s;
    if (timeout_ms >= 0)
        s->deadline_ns = now_ns() + (int64_t)timeout_ms * 1000000;
    int r = avformat_open_input(&s->fmt, url, NULL, &opts);
    if (r < 0) {
        averr(err, errlen, url, r);
        goto fail;
    }
    const AVDictionaryEntry *e = av_dict_get(opts, "", NULL, AV_DICT_IGNORE_SUFFIX);
    if (e) {
        snprintf(err, errlen, "unknown option %s", e->key);
        goto fail;
    }
    if ((r = avformat_find_stream_info(s->fmt, NULL)) < 0) {
        averr(err, errlen, "probe", r);
        goto fail;
    }
    s->deadline_ns = 0;
    int v = av_find_best_stream(s->fmt, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    int a = av_find_best_stream(s->fmt, AVMEDIA_TYPE_AUDIO, -1, v >= 0 ? v : -1, NULL, 0);
    s->idx[CYM_SOURCE_VIDEO] = v >= 0 ? v : -1;
    s->idx[CYM_SOURCE_AUDIO] = a >= 0 ? a : -1;
    if (v < 0 && a < 0) {
        snprintf(err, errlen, "no audio or video stream");
        goto fail;
    }
    for (unsigned i = 0; i < s->fmt->nb_streams; i++)
        if ((int)i != v && (int)i != a)
            s->fmt->streams[i]->discard = AVDISCARD_ALL;
    if (v >= 0 && video_info(s, s->fmt->streams[v], err, errlen) < 0)
        goto fail;
    if (a >= 0)
        audio_info(s, s->fmt->streams[a]);
    av_dict_free(&opts);
    return s;
fail:
    av_dict_free(&opts);
    cym_source_close(s);
    return NULL;
}

void cym_source_info(const cym_source *s, int stream, cym_stream_info *out)
{
    if (stream == CYM_SOURCE_VIDEO || stream == CYM_SOURCE_AUDIO)
        *out = s->info[stream];
    else
        memset(out, 0, sizeof *out);
}

static void release_buf(cymedia_buf *b)
{
    free(b->priv);
}

static void deliver(cym_source *s, int k, const uint8_t *d, size_t n, uint64_t group,
                    uint64_t object, int64_t us, int key)
{
    if (!s->sink.on_frame)
        return;
    srcbuf *b = malloc(sizeof *b + n);
    if (!b)
        return;
    memcpy(b->data, d, n);
    b->buf.data = b->data;
    b->buf.size = n;
    b->buf.release = release_buf;
    b->buf.priv = b;
    cymedia_meta m = {.group = group, .object = object, .pts_us = us,
                      .flags = key ? CYMEDIA_KEY | CYMEDIA_GROUP_START : 0, .branch = (uint32_t)k};
    s->sink.on_frame(s->sink.ctx, &m, &b->buf);
}

static void event(cym_source *s, int code, const char *msg)
{
    if (s->sink.on_event)
        s->sink.on_event(s->sink.ctx, UINT32_MAX, code, s->group, msg);
}

static void pace_to(cym_source *s, int64_t us)
{
    int64_t due = s->t0_ns + us * 1000;
    for (;;) {
        int64_t left = due - now_ns();
        if (left <= 0 || __atomic_load_n(&s->closing, __ATOMIC_ACQUIRE))
            return;
        struct timespec ts = {0, (long)(left < 50000000 ? left : 50000000)};
        nanosleep(&ts, NULL);
    }
}

static void handle(cym_source *s, const AVPacket *pkt)
{
    int k = pkt->stream_index == s->idx[CYM_SOURCE_VIDEO]   ? CYM_SOURCE_VIDEO
            : pkt->stream_index == s->idx[CYM_SOURCE_AUDIO] ? CYM_SOURCE_AUDIO
                                                            : -1;
    int64_t ts = pkt->pts != AV_NOPTS_VALUE ? pkt->pts : pkt->dts;
    if (k < 0 || pkt->size <= 0 || ts == AV_NOPTS_VALUE)
        return;
    int64_t us = av_rescale_q(ts, s->fmt->streams[pkt->stream_index]->time_base, AV_TIME_BASE_Q);
    if (!s->t0_ns) {
        s->origin_us = us;
        s->t0_ns = now_ns();
    }
    us -= s->origin_us;
    int key = !!(pkt->flags & AV_PKT_FLAG_KEY);
    if (k == CYM_SOURCE_VIDEO) {
        if (!s->have_group && !key) {
            SSTAT(s, before_key, 1);
            return;
        }
        if (key) {
            s->group += s->have_group;
            s->have_group = 1;
            s->object = 0;
        } else {
            s->object++;
        }
    }
    const uint8_t *d = pkt->data;
    size_t n = (size_t)pkt->size;
    if (k == CYM_SOURCE_VIDEO && s->annexb) {
        if (to_length_prefixed(s, d, n, &n) < 0 || !n)
            return;
        d = s->scratch;
    }
    if (s->pace)
        pace_to(s, us);
    pthread_mutex_lock(&s->mu);
    s->st.bytes += n;
    if (k == CYM_SOURCE_VIDEO)
        s->st.video_packets++;
    else
        s->st.audio_packets++;
    pthread_mutex_unlock(&s->mu);
    if (k == CYM_SOURCE_AUDIO) {
        deliver(s, k, d, n, s->audio_n++, 0, us, 1);
        return;
    }
    if (s->graph_ok) {
        int r = cym_graph_push(s->graph, d, n, s->group, s->object, us, key, 0);
        if (r < 0)
            s->graph_ok = 0; /* flushing or failed */
        else if (r)
            SSTAT(s, graph_dropped, 1);
    }
    if (s->passthrough)
        deliver(s, k, d, n, s->group, s->object, us, key);
}

static void *reader(void *arg)
{
    cym_source *s = arg;
    AVPacket *pkt = av_packet_alloc();
    if (!pkt) {
        event(s, CYMEDIA_EV_ERROR, "out of memory");
        return NULL;
    }
    while (!__atomic_load_n(&s->closing, __ATOMIC_ACQUIRE)) {
        int r = av_read_frame(s->fmt, pkt);
        if (r == AVERROR(EAGAIN)) {
            struct timespec ts = {0, 1000000};
            nanosleep(&ts, NULL);
            continue;
        }
        if (r < 0) {
            if (__atomic_load_n(&s->closing, __ATOMIC_ACQUIRE))
                break;
            if (r == AVERROR_EOF) {
                event(s, CYMEDIA_EV_END, "end of input");
            } else {
                char msg[160];
                averr(msg, sizeof msg, "read", r);
                event(s, CYMEDIA_EV_ERROR, msg);
            }
            break;
        }
        handle(s, pkt);
        av_packet_unref(pkt);
    }
    av_packet_free(&pkt);
    return NULL;
}

int cym_source_start(cym_source *s, cym_graph *graph, cymedia_sink sink, int passthrough, int pace)
{
    if (s->started || (sink.abi && (sink.abi != CYMEDIA_SINK_ABI || sink.size < sizeof sink)))
        return -1;
    s->graph = graph;
    s->graph_ok = graph != NULL;
    s->sink = sink;
    s->passthrough = passthrough;
    s->pace = pace;
    if (pthread_create(&s->thread, NULL, reader, s) != 0)
        return -1;
    s->started = 1;
    return 0;
}

void cym_source_get_stats(cym_source *s, cym_source_stats *out)
{
    pthread_mutex_lock(&s->mu);
    *out = s->st;
    pthread_mutex_unlock(&s->mu);
}

void cym_source_close(cym_source *s)
{
    if (!s)
        return;
    __atomic_store_n(&s->closing, 1, __ATOMIC_RELEASE);
    if (s->started)
        pthread_join(s->thread, NULL);
    avformat_close_input(&s->fmt);
    free(s->xd[0]);
    free(s->xd[1]);
    free(s->scratch);
    pthread_mutex_destroy(&s->mu);
    free(s);
}

int cym_have_protocol(const char *name)
{
    void *it = NULL;
    const char *p;
    while ((p = avio_enum_protocols(&it, 0)))
        if (!strcmp(p, name))
            return 1;
    return 0;
}

int cym_have_format(const char *name)
{
    return av_find_input_format(name) != NULL;
}
