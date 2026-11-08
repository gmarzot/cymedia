#define _POSIX_C_SOURCE 200809L
#include "engine.h"

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/dict.h>
#include <libavutil/hwcontext.h>

#define META_CAP 64
#define DELAY_CAP 8192

typedef struct meta_int {
    cymedia_meta pub;
    int64_t t_push_ns;
} meta_int;

/* Pending metadata matched to frames by pts; entries at or before a taken pts are retired. */
typedef struct meta_list {
    meta_int m[META_CAP];
    int n;
} meta_list;

typedef struct item {
    AVPacket *pkt;
    AVFrame *frame;
    meta_int meta;
    int eof;
} item;

typedef struct queue {
    item **v;
    int cap, head, len, closed;
    pthread_mutex_t mu;
    pthread_cond_t not_empty, not_full;
} queue;

typedef struct branch {
    struct cym_graph *g;
    int index;
    char *filter, *encoder, *options;
    int strip_filler;

    /* decode thread */
    AVFilterGraph *fg;
    AVFilterContext *src, *sink;
    int in_w, in_h, in_fmt;
    void *in_hwfc;
    meta_list fmeta;
    int skipping;

    queue q;
    pthread_t thread;
    int started;

    /* branch thread */
    AVCodecContext *enc;
    int enc_w, enc_h, enc_fmt;
    meta_list emeta;

    pthread_mutex_t st_mu;
    cym_branch_stats st;
    int64_t *delays;
    uint64_t ndelays;
} branch;

struct cym_graph {
    cymedia_sink sink;
    int fps_num, fps_den, realtime;

    AVCodecContext *dec;
    AVBufferRef *hwdev;
    enum AVPixelFormat hw_fmt;
    AVFrame *frame;
    meta_list dmeta;
    queue inq;
    pthread_t thread;
    int started;

    int nb;
    branch *b;

    /* producer */
    int have_last, resync, flushing;
    uint64_t last_group;

    pthread_mutex_t mu;
    pthread_cond_t done;
    int ended;
    cym_graph_stats st;
};

static int64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void meta_put(meta_list *l, const meta_int *m)
{
    if (l->n == META_CAP) {
        memmove(&l->m[0], &l->m[1], (META_CAP - 1) * sizeof(meta_int));
        l->n--;
    }
    l->m[l->n++] = *m;
}

/* Without an exact match the newest earlier entry stands in, minus GROUP_START. */
static int meta_take(meta_list *l, int64_t pts, meta_int *out)
{
    int exact = -1, best = -1;
    for (int i = 0; i < l->n; i++) {
        int64_t p = l->m[i].pub.pts_us;
        if (p == pts) {
            exact = i;
            break;
        }
        if (p < pts && (best < 0 || p > l->m[best].pub.pts_us))
            best = i;
    }
    if (exact >= 0) {
        *out = l->m[exact];
    } else if (best >= 0) {
        *out = l->m[best];
        out->pub.pts_us = pts;
        out->pub.flags &= ~CYMEDIA_GROUP_START;
    } else {
        memset(out, 0, sizeof *out);
        out->pub.pts_us = pts;
    }
    int j = 0;
    for (int i = 0; i < l->n; i++)
        if (l->m[i].pub.pts_us > pts)
            l->m[j++] = l->m[i];
    l->n = j;
    return exact >= 0 || best >= 0;
}

static void free_item(item *it)
{
    if (!it)
        return;
    av_packet_free(&it->pkt);
    av_frame_free(&it->frame);
    free(it);
}

static int q_init(queue *q, int cap)
{
    q->v = calloc((size_t)cap, sizeof(item *));
    if (!q->v)
        return -1;
    q->cap = cap;
    pthread_mutex_init(&q->mu, NULL);
    pthread_cond_init(&q->not_empty, NULL);
    pthread_cond_init(&q->not_full, NULL);
    return 0;
}

/* 0 queued, 1 full, -1 closed. */
static int q_push(queue *q, item *it, int block)
{
    pthread_mutex_lock(&q->mu);
    while (block && !q->closed && q->len == q->cap)
        pthread_cond_wait(&q->not_full, &q->mu);
    int r = q->closed ? -1 : q->len == q->cap ? 1 : 0;
    if (r == 0) {
        q->v[(q->head + q->len) % q->cap] = it;
        q->len++;
        pthread_cond_signal(&q->not_empty);
    }
    pthread_mutex_unlock(&q->mu);
    return r;
}

/* NULL once the queue is closed; items left behind are freed by q_free. */
static item *q_pop(queue *q)
{
    item *it = NULL;
    pthread_mutex_lock(&q->mu);
    while (!q->len && !q->closed)
        pthread_cond_wait(&q->not_empty, &q->mu);
    if (!q->closed) {
        it = q->v[q->head];
        q->head = (q->head + 1) % q->cap;
        q->len--;
        pthread_cond_signal(&q->not_full);
    }
    pthread_mutex_unlock(&q->mu);
    return it;
}

static void q_close(queue *q)
{
    if (!q->v)
        return;
    pthread_mutex_lock(&q->mu);
    q->closed = 1;
    pthread_cond_broadcast(&q->not_empty);
    pthread_cond_broadcast(&q->not_full);
    pthread_mutex_unlock(&q->mu);
}

static void q_free(queue *q)
{
    if (!q->v)
        return;
    for (int i = 0; i < q->len; i++)
        free_item(q->v[(q->head + i) % q->cap]);
    free(q->v);
    q->v = NULL;
    pthread_mutex_destroy(&q->mu);
    pthread_cond_destroy(&q->not_empty);
    pthread_cond_destroy(&q->not_full);
}

static void ev(cym_graph *g, int branch_idx, int code, uint64_t group, const char *fmt, ...)
{
    if (!g->sink.on_event)
        return;
    char msg[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    g->sink.on_event(g->sink.ctx, (uint32_t)branch_idx, code, group, msg);
}

static const char *averr(int r, char *buf, size_t len)
{
    av_strerror(r, buf, len);
    return buf;
}

#define BSTAT(b, field, delta)                  \
    do {                                        \
        pthread_mutex_lock(&(b)->st_mu);        \
        (b)->st.field += (delta);               \
        pthread_mutex_unlock(&(b)->st_mu);      \
    } while (0)

#define GSTAT(g, field, delta)                  \
    do {                                        \
        pthread_mutex_lock(&(g)->mu);           \
        (g)->st.field += (delta);               \
        pthread_mutex_unlock(&(g)->mu);         \
    } while (0)

/* ---- output ---- */

static void pkt_release(cymedia_buf *buf)
{
    AVPacket *p = buf->priv;
    av_packet_free(&p);
    free(buf);
}

static size_t next_start_code(const uint8_t *d, size_t n, size_t from)
{
    for (size_t k = from; k + 2 < n; k++)
        if (d[k] == 0 && d[k + 1] == 0 && d[k + 2] == 1)
            return k;
    return n;
}

/* Removes H.264 filler-data NAL units (type 12) from an Annex B packet. */
static void strip_filler(AVPacket *p)
{
    size_t n = (size_t)p->size, k = next_start_code(p->data, n, 0);
    if (k == n || av_packet_make_writable(p) < 0)
        return;
    uint8_t *d = p->data;
    size_t unit = (k > 0 && d[k - 1] == 0) ? k - 1 : k, w = unit;
    while (k < n) {
        size_t nk = next_start_code(d, n, k + 3);
        size_t end = (nk < n && d[nk - 1] == 0) ? nk - 1 : nk;
        if (k + 3 >= n || (d[k + 3] & 0x1f) != 12) {
            memmove(d + w, d + unit, end - unit);
            w += end - unit;
        }
        unit = end;
        k = nk;
    }
    if (w < n) {
        p->size = (int)w;
        memset(d + w, 0, AV_INPUT_BUFFER_PADDING_SIZE);
    }
}

static void emit(cym_graph *g, branch *b, AVPacket *p)
{
    meta_int m;
    meta_take(&b->emeta, p->pts, &m);
    m.pub.branch = (uint32_t)b->index;
    m.pub.flags = (m.pub.flags & CYMEDIA_GROUP_START) | ((p->flags & AV_PKT_FLAG_KEY) ? CYMEDIA_KEY : 0);
    if (b->strip_filler)
        strip_filler(p);
    cymedia_buf *buf = malloc(sizeof *buf);
    if (!buf) {
        av_packet_free(&p);
        BSTAT(b, errors, 1);
        return;
    }
    buf->data = p->data;
    buf->size = (size_t)p->size;
    buf->release = pkt_release;
    buf->priv = p;
    int64_t delay = m.t_push_ns ? now_ns() - m.t_push_ns : 0;
    pthread_mutex_lock(&b->st_mu);
    b->delays[b->ndelays++ % DELAY_CAP] = delay;
    b->st.frames_out++;
    pthread_mutex_unlock(&b->st_mu);
    g->sink.on_frame(g->sink.ctx, &m.pub, buf);
}

/* ---- branch thread: encode ---- */

static void receive_packets(cym_graph *g, branch *b)
{
    char eb[128];
    for (;;) {
        AVPacket *p = av_packet_alloc();
        int r = p ? avcodec_receive_packet(b->enc, p) : AVERROR(ENOMEM);
        if (r < 0) {
            av_packet_free(&p);
            if (r != AVERROR(EAGAIN) && r != AVERROR_EOF) {
                BSTAT(b, errors, 1);
                ev(g, b->index, CYMEDIA_EV_ERROR, 0, "encode: %s", averr(r, eb, sizeof eb));
            }
            return;
        }
        emit(g, b, p);
    }
}

static void close_encoder(cym_graph *g, branch *b)
{
    if (!b->enc)
        return;
    if (avcodec_send_frame(b->enc, NULL) >= 0)
        receive_packets(g, b);
    avcodec_free_context(&b->enc);
}

static int open_encoder(cym_graph *g, branch *b, const AVFrame *f)
{
    char eb[128];
    close_encoder(g, b);
    const AVCodec *c = avcodec_find_encoder_by_name(b->encoder);
    AVCodecContext *e = c ? avcodec_alloc_context3(c) : NULL;
    if (!e)
        return AVERROR(ENOMEM);
    e->width = f->width;
    e->height = f->height;
    e->pix_fmt = f->format;
    if (f->hw_frames_ctx)
        e->hw_frames_ctx = av_buffer_ref(f->hw_frames_ctx);
    e->sample_aspect_ratio = f->sample_aspect_ratio;
    e->color_range = f->color_range;
    e->color_primaries = f->color_primaries;
    e->color_trc = f->color_trc;
    e->colorspace = f->colorspace;
    e->chroma_sample_location = f->chroma_location;
    e->time_base = (AVRational){1, 1000000};
    e->framerate = (AVRational){g->fps_num, g->fps_den};

    AVDictionary *opts = NULL;
    if (b->options && *b->options)
        av_dict_parse_string(&opts, b->options, "=", "\n", 0);
    int r = avcodec_open2(e, c, &opts);
    const AVDictionaryEntry *t = NULL;
    while ((t = av_dict_get(opts, "", t, AV_DICT_IGNORE_SUFFIX)))
        ev(g, b->index, CYMEDIA_EV_ERROR, 0, "%s: unknown option %s", b->encoder, t->key);
    av_dict_free(&opts);
    if (r < 0) {
        ev(g, b->index, CYMEDIA_EV_ERROR, 0, "open %s: %s", b->encoder, averr(r, eb, sizeof eb));
        avcodec_free_context(&e);
        return r;
    }
    b->enc = e;
    b->enc_w = f->width;
    b->enc_h = f->height;
    b->enc_fmt = f->format;
    return 0;
}

static void encode(cym_graph *g, branch *b, AVFrame *f, const meta_int *m)
{
    char eb[128];
    if (!f) {
        close_encoder(g, b);
        return;
    }
    if (!b->enc || f->width != b->enc_w || f->height != b->enc_h || f->format != b->enc_fmt) {
        if (open_encoder(g, b, f) < 0) {
            BSTAT(b, errors, 1);
            return;
        }
    }
    f->pict_type = (m->pub.flags & CYMEDIA_GROUP_START) ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
    meta_put(&b->emeta, m);
    int r = avcodec_send_frame(b->enc, f);
    if (r < 0) {
        BSTAT(b, errors, 1);
        ev(g, b->index, CYMEDIA_EV_ERROR, m->pub.group, "encode: %s", averr(r, eb, sizeof eb));
    }
    receive_packets(g, b);
}

static void *branch_main(void *arg)
{
    branch *b = arg;
    cym_graph *g = b->g;
    item *it;
    while ((it = q_pop(&b->q))) {
        int eof = it->eof;
        encode(g, b, eof ? NULL : it->frame, &it->meta);
        free_item(it);
        if (eof) {
            ev(g, b->index, CYMEDIA_EV_END, 0, "end");
            pthread_mutex_lock(&g->mu);
            g->ended++;
            pthread_cond_broadcast(&g->done);
            pthread_mutex_unlock(&g->mu);
            break;
        }
    }
    return NULL;
}

/* ---- decode thread: decode and filter ---- */

static int build_filter(cym_graph *g, branch *b, const AVFrame *in)
{
    char args[256], eb[128];
    int r;
    avfilter_graph_free(&b->fg);
    b->fg = avfilter_graph_alloc();
    if (!b->fg)
        return AVERROR(ENOMEM);
    b->fg->nb_threads = 1;
    AVRational sar = in->sample_aspect_ratio.num ? in->sample_aspect_ratio : (AVRational){1, 1};
    snprintf(args, sizeof args, "video_size=%dx%d:pix_fmt=%d:time_base=1/1000000:pixel_aspect=%d/%d",
             in->width, in->height, in->format, sar.num, sar.den);
    r = avfilter_graph_create_filter(&b->src, avfilter_get_by_name("buffer"), "in", args, NULL, b->fg);
    if (r >= 0 && in->hw_frames_ctx) {
        AVBufferSrcParameters *par = av_buffersrc_parameters_alloc();
        if (!par)
            r = AVERROR(ENOMEM);
        else {
            par->hw_frames_ctx = in->hw_frames_ctx;
            r = av_buffersrc_parameters_set(b->src, par);
            av_free(par);
        }
    }
    if (r >= 0)
        r = avfilter_graph_create_filter(&b->sink, avfilter_get_by_name("buffersink"), "out", NULL, NULL, b->fg);
    if (r >= 0) {
        AVFilterInOut *outs = avfilter_inout_alloc(), *ins = avfilter_inout_alloc();
        if (!outs || !ins) {
            r = AVERROR(ENOMEM);
        } else {
            outs->name = av_strdup("in");
            outs->filter_ctx = b->src;
            ins->name = av_strdup("out");
            ins->filter_ctx = b->sink;
            r = avfilter_graph_parse_ptr(b->fg, *b->filter ? b->filter : "null", &ins, &outs, NULL);
        }
        avfilter_inout_free(&ins);
        avfilter_inout_free(&outs);
    }
    if (r >= 0 && g->hwdev)
        for (unsigned i = 0; i < b->fg->nb_filters; i++)
            if (!b->fg->filters[i]->hw_device_ctx)
                b->fg->filters[i]->hw_device_ctx = av_buffer_ref(g->hwdev);
    if (r >= 0)
        r = avfilter_graph_config(b->fg, NULL);
    if (r < 0) {
        ev(g, b->index, CYMEDIA_EV_ERROR, 0, "filter '%s': %s", b->filter, averr(r, eb, sizeof eb));
        avfilter_graph_free(&b->fg);
        return r;
    }
    b->in_w = in->width;
    b->in_h = in->height;
    b->in_fmt = in->format;
    b->in_hwfc = in->hw_frames_ctx ? in->hw_frames_ctx->data : NULL;
    b->fmeta.n = 0;
    return 0;
}

/* A branch that cannot keep up drops the rest of the group and resumes at the next group start. */
static void pull_filter(cym_graph *g, branch *b)
{
    for (;;) {
        item *it = calloc(1, sizeof *it);
        if (!it || !(it->frame = av_frame_alloc())) {
            free_item(it);
            return;
        }
        if (av_buffersink_get_frame(b->sink, it->frame) < 0) {
            free_item(it);
            return;
        }
        meta_take(&b->fmeta, it->frame->pts, &it->meta);
        int start = it->meta.pub.flags & CYMEDIA_GROUP_START;
        uint64_t group = it->meta.pub.group;
        if (b->skipping && !start) {
            free_item(it);
            BSTAT(b, frames_dropped, 1);
            continue;
        }
        b->skipping = 0;
        int r = q_push(&b->q, it, !g->realtime);
        if (r != 0) {
            free_item(it);
            if (r < 0)
                return;
            b->skipping = 1;
            pthread_mutex_lock(&b->st_mu);
            b->st.frames_dropped++;
            b->st.groups_abandoned++;
            pthread_mutex_unlock(&b->st_mu);
            ev(g, b->index, CYMEDIA_EV_GROUP_ABANDONED, group, "encoder queue full");
            continue;
        }
        BSTAT(b, frames_in, 1);
    }
}

static void filter_frame(cym_graph *g, branch *b, AVFrame *in, const meta_int *m)
{
    char eb[128];
    void *hwfc = in->hw_frames_ctx ? in->hw_frames_ctx->data : NULL;
    if (!b->fg || in->width != b->in_w || in->height != b->in_h || in->format != b->in_fmt || hwfc != b->in_hwfc) {
        if (build_filter(g, b, in) < 0) {
            BSTAT(b, errors, 1);
            return;
        }
    }
    meta_put(&b->fmeta, m);
    int r = av_buffersrc_add_frame_flags(b->src, in, AV_BUFFERSRC_FLAG_KEEP_REF);
    if (r < 0) {
        BSTAT(b, errors, 1);
        ev(g, b->index, CYMEDIA_EV_ERROR, m->pub.group, "filter: %s", averr(r, eb, sizeof eb));
        return;
    }
    pull_filter(g, b);
}

static void decode(cym_graph *g, AVPacket *pkt)
{
    char eb[128];
    int r = avcodec_send_packet(g->dec, pkt);
    if (r < 0 && r != AVERROR_EOF) {
        GSTAT(g, decode_errors, 1);
        ev(g, -1, CYMEDIA_EV_ERROR, 0, "decode: %s", averr(r, eb, sizeof eb));
    }
    for (;;) {
        r = avcodec_receive_frame(g->dec, g->frame);
        if (r == AVERROR(EAGAIN) || r == AVERROR_EOF)
            return;
        if (r < 0) {
            GSTAT(g, decode_errors, 1);
            ev(g, -1, CYMEDIA_EV_ERROR, 0, "decode: %s", averr(r, eb, sizeof eb));
            return;
        }
        meta_int m;
        meta_take(&g->dmeta, g->frame->pts, &m);
        GSTAT(g, frames_decoded, 1);
        for (int i = 0; i < g->nb; i++)
            filter_frame(g, &g->b[i], g->frame, &m);
        av_frame_unref(g->frame);
    }
}

static void *decode_main(void *arg)
{
    cym_graph *g = arg;
    item *it;
    int eof = 0;
    while ((it = q_pop(&g->inq))) {
        eof = it->eof;
        if (!eof)
            meta_put(&g->dmeta, &it->meta);
        decode(g, eof ? NULL : it->pkt);
        free_item(it);
        if (eof)
            break;
    }
    if (!eof)
        return NULL;
    for (int i = 0; i < g->nb; i++) {
        branch *b = &g->b[i];
        if (b->fg && av_buffersrc_add_frame(b->src, NULL) >= 0)
            pull_filter(g, b);
        item *e = calloc(1, sizeof *e);
        if (e) {
            e->eof = 1;
            if (q_push(&b->q, e, 1) != 0)
                free_item(e);
        }
    }
    return NULL;
}

/* ---- public ---- */

static enum AVPixelFormat get_format(AVCodecContext *ctx, const enum AVPixelFormat *fmts)
{
    cym_graph *g = ctx->opaque;
    for (const enum AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; p++)
        if (*p == g->hw_fmt)
            return *p;
    return AV_PIX_FMT_NONE;
}

static int open_decoder(cym_graph *g, const cym_graph_cfg *cfg, char *err, size_t errlen)
{
    char eb[128];
    const AVCodec *c = avcodec_find_decoder_by_name(cfg->decoder ? cfg->decoder : "h264");
    if (!c) {
        snprintf(err, errlen, "unknown decoder %s", cfg->decoder);
        return -1;
    }
    g->dec = avcodec_alloc_context3(c);
    if (!g->dec)
        return -1;
    g->dec->pkt_timebase = (AVRational){1, 1000000};
    g->dec->thread_count = 1;
    if (cfg->extradata && cfg->extradata_size > 0) {
        g->dec->extradata = av_mallocz((size_t)cfg->extradata_size + AV_INPUT_BUFFER_PADDING_SIZE);
        if (!g->dec->extradata)
            return -1;
        memcpy(g->dec->extradata, cfg->extradata, (size_t)cfg->extradata_size);
        g->dec->extradata_size = cfg->extradata_size;
    }
    if (cfg->hwaccel && *cfg->hwaccel) {
        enum AVHWDeviceType type = av_hwdevice_find_type_by_name(cfg->hwaccel);
        if (type == AV_HWDEVICE_TYPE_NONE) {
            snprintf(err, errlen, "unknown hwaccel %s", cfg->hwaccel);
            return -1;
        }
        g->hw_fmt = AV_PIX_FMT_NONE;
        for (int i = 0;; i++) {
            const AVCodecHWConfig *hc = avcodec_get_hw_config(c, i);
            if (!hc)
                break;
            if ((hc->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX) && hc->device_type == type) {
                g->hw_fmt = hc->pix_fmt;
                break;
            }
        }
        if (g->hw_fmt == AV_PIX_FMT_NONE) {
            snprintf(err, errlen, "decoder %s has no %s support", c->name, cfg->hwaccel);
            return -1;
        }
        int r = av_hwdevice_ctx_create(&g->hwdev, type, cfg->device, NULL, 0);
        if (r < 0) {
            snprintf(err, errlen, "open %s device: %s", cfg->hwaccel, averr(r, eb, sizeof eb));
            return -1;
        }
        g->dec->hw_device_ctx = av_buffer_ref(g->hwdev);
        g->dec->get_format = get_format;
        g->dec->opaque = g;
        g->dec->extra_hw_frames = cfg->extra_hw_frames;
    }
    int r = avcodec_open2(g->dec, c, NULL);
    if (r < 0) {
        snprintf(err, errlen, "open decoder %s: %s", c->name, averr(r, eb, sizeof eb));
        return -1;
    }
    return 0;
}

static char *dupstr(const char *s)
{
    return strdup(s ? s : "");
}

cym_graph *cym_graph_create(const cym_graph_cfg *cfg, char *err, size_t errlen)
{
    snprintf(err, errlen, "out of memory");
    if (cfg->sink.abi != CYMEDIA_SINK_ABI || cfg->sink.size < sizeof(cymedia_sink) || !cfg->sink.on_frame) {
        snprintf(err, errlen, "sink ABI mismatch");
        return NULL;
    }
    if (cfg->nbranches < 1) {
        snprintf(err, errlen, "a graph needs at least one branch");
        return NULL;
    }
    for (int i = 0; i < cfg->nbranches; i++)
        if (!avcodec_find_encoder_by_name(cfg->branches[i].encoder ? cfg->branches[i].encoder : "")) {
            snprintf(err, errlen, "unknown encoder %s", cfg->branches[i].encoder);
            return NULL;
        }
    cym_graph *g = calloc(1, sizeof *g);
    if (!g)
        return NULL;
    pthread_mutex_init(&g->mu, NULL);
    pthread_cond_init(&g->done, NULL);
    g->sink = cfg->sink;
    g->fps_num = cfg->fps_num > 0 ? cfg->fps_num : 60;
    g->fps_den = cfg->fps_den > 0 ? cfg->fps_den : 1;
    g->realtime = cfg->realtime;
    g->nb = cfg->nbranches;
    g->b = calloc((size_t)g->nb, sizeof(branch));
    if (!g->b) {
        g->nb = 0;
        goto fail;
    }
    if (q_init(&g->inq, cfg->input_depth > 0 ? cfg->input_depth : 240) < 0)
        goto fail;
    for (int i = 0; i < g->nb; i++) {
        branch *b = &g->b[i];
        b->g = g;
        b->index = i;
        pthread_mutex_init(&b->st_mu, NULL);
        b->filter = dupstr(cfg->branches[i].filter);
        b->encoder = dupstr(cfg->branches[i].encoder);
        b->options = dupstr(cfg->branches[i].options);
        b->strip_filler = cfg->branches[i].strip_filler;
        b->delays = calloc(DELAY_CAP, sizeof(int64_t));
        if (!b->filter || !b->encoder || !b->options || !b->delays ||
            q_init(&b->q, cfg->queue_depth > 0 ? cfg->queue_depth : 4) < 0)
            goto fail;
    }
    if (open_decoder(g, cfg, err, errlen) < 0 || !(g->frame = av_frame_alloc()))
        goto fail;
    for (int i = 0; i < g->nb; i++) {
        if (pthread_create(&g->b[i].thread, NULL, branch_main, &g->b[i]) != 0)
            goto fail;
        g->b[i].started = 1;
    }
    if (pthread_create(&g->thread, NULL, decode_main, g) != 0)
        goto fail;
    g->started = 1;
    return g;
fail:
    cym_graph_destroy(g);
    return NULL;
}

int cym_graph_push(cym_graph *g, const uint8_t *data, size_t size, uint64_t group,
                   uint64_t object, int64_t pts_us, int key, int block)
{
    if (g->flushing)
        return -1;
    int start = key && (!g->have_last || group != g->last_group);
    g->have_last = 1;
    g->last_group = group;
    if (g->resync && !start) {
        GSTAT(g, packets_dropped, 1);
        return 1;
    }
    item *it = calloc(1, sizeof *it);
    if (!it || !(it->pkt = av_packet_alloc()) || av_new_packet(it->pkt, (int)size) < 0) {
        free_item(it);
        return -1;
    }
    memcpy(it->pkt->data, data, size);
    it->pkt->pts = pts_us;
    it->pkt->dts = AV_NOPTS_VALUE;
    if (key)
        it->pkt->flags |= AV_PKT_FLAG_KEY;
    it->meta.pub.group = group;
    it->meta.pub.object = object;
    it->meta.pub.pts_us = pts_us;
    it->meta.pub.flags = (key ? CYMEDIA_KEY : 0) | (start ? CYMEDIA_GROUP_START : 0);
    it->meta.t_push_ns = now_ns();
    int r = q_push(&g->inq, it, block || !g->realtime);
    if (r != 0) {
        free_item(it);
        if (r < 0)
            return -1;
        g->resync = 1;
        GSTAT(g, packets_dropped, 1);
        ev(g, -1, CYMEDIA_EV_INPUT_DROPPED, group, "input queue full");
        return 1;
    }
    g->resync = 0;
    GSTAT(g, packets_in, 1);
    return 0;
}

int cym_graph_flush(cym_graph *g, int timeout_ms)
{
    if (!g->flushing) {
        g->flushing = 1;
        item *it = calloc(1, sizeof *it);
        if (!it)
            return 1;
        it->eof = 1;
        if (q_push(&g->inq, it, 1) != 0) {
            free_item(it);
            return 1;
        }
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    int64_t deadline_ns = (int64_t)tv.tv_sec * 1000000000 + (int64_t)tv.tv_usec * 1000 + (int64_t)timeout_ms * 1000000;
    struct timespec ts = {(time_t)(deadline_ns / 1000000000), (long)(deadline_ns % 1000000000)};
    int r = 0;
    pthread_mutex_lock(&g->mu);
    while (g->ended < g->nb && r == 0) {
        if (timeout_ms < 0)
            pthread_cond_wait(&g->done, &g->mu);
        else
            r = pthread_cond_timedwait(&g->done, &g->mu, &ts);
    }
    int done = g->ended >= g->nb;
    pthread_mutex_unlock(&g->mu);
    return done ? 0 : 1;
}

void cym_graph_destroy(cym_graph *g)
{
    if (!g)
        return;
    q_close(&g->inq);
    for (int i = 0; i < g->nb; i++)
        q_close(&g->b[i].q);
    if (g->started)
        pthread_join(g->thread, NULL);
    for (int i = 0; i < g->nb; i++) {
        branch *b = &g->b[i];
        if (b->started)
            pthread_join(b->thread, NULL);
        q_free(&b->q);
        avfilter_graph_free(&b->fg);
        avcodec_free_context(&b->enc);
        free(b->filter);
        free(b->encoder);
        free(b->options);
        free(b->delays);
        pthread_mutex_destroy(&b->st_mu);
    }
    q_free(&g->inq);
    free(g->b);
    av_frame_free(&g->frame);
    avcodec_free_context(&g->dec);
    av_buffer_unref(&g->hwdev);
    pthread_mutex_destroy(&g->mu);
    pthread_cond_destroy(&g->done);
    free(g);
}

int cym_graph_nbranches(const cym_graph *g)
{
    return g->nb;
}

void cym_graph_get_stats(cym_graph *g, cym_graph_stats *out)
{
    pthread_mutex_lock(&g->mu);
    *out = g->st;
    pthread_mutex_unlock(&g->mu);
}

void cym_branch_get_stats(cym_graph *g, int i, cym_branch_stats *out)
{
    memset(out, 0, sizeof *out);
    if (i < 0 || i >= g->nb)
        return;
    pthread_mutex_lock(&g->b[i].st_mu);
    *out = g->b[i].st;
    pthread_mutex_unlock(&g->b[i].st_mu);
}

int cym_branch_delays(cym_graph *g, int i, int64_t *out, int cap)
{
    if (i < 0 || i >= g->nb || cap <= 0)
        return 0;
    branch *b = &g->b[i];
    pthread_mutex_lock(&b->st_mu);
    uint64_t n = b->ndelays < DELAY_CAP ? b->ndelays : DELAY_CAP;
    if ((uint64_t)cap < n)
        n = (uint64_t)cap;
    for (uint64_t k = 0; k < n; k++)
        out[k] = b->delays[(b->ndelays - n + k) % DELAY_CAP];
    pthread_mutex_unlock(&b->st_mu);
    return (int)n;
}

int cym_have_encoder(const char *name)
{
    return avcodec_find_encoder_by_name(name) != NULL;
}

int cym_have_decoder(const char *name)
{
    return avcodec_find_decoder_by_name(name) != NULL;
}

int cym_have_filter(const char *name)
{
    return avfilter_get_by_name(name) != NULL;
}

int cym_hw_available(const char *type, const char *device)
{
    enum AVHWDeviceType t = av_hwdevice_find_type_by_name(type);
    AVBufferRef *dev = NULL;
    if (t == AV_HWDEVICE_TYPE_NONE || av_hwdevice_ctx_create(&dev, t, device, NULL, 0) < 0)
        return 0;
    av_buffer_unref(&dev);
    return 1;
}
