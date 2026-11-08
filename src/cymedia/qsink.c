#define _POSIX_C_SOURCE 200809L
#include "qsink.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

struct cym_qsink {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    cym_qitem *head, *tail;
    int fds[2];
    int signaled;
};

static void enqueue(cym_qsink *q, cym_qitem *it)
{
    pthread_mutex_lock(&q->mu);
    if (q->tail)
        q->tail->next = it;
    else
        q->head = it;
    q->tail = it;
    if (!q->signaled) {
        q->signaled = 1;
        ssize_t w = write(q->fds[1], "x", 1);
        (void)w;
    }
    pthread_cond_signal(&q->cv);
    pthread_mutex_unlock(&q->mu);
}

static void on_frame(void *ctx, const cymedia_meta *meta, cymedia_buf *buf)
{
    cym_qitem *it = calloc(1, sizeof *it);
    if (!it) {
        buf->release(buf);
        return;
    }
    it->meta = *meta;
    it->buf = buf;
    it->data = buf->data;
    it->size = buf->size;
    enqueue(ctx, it);
}

static void on_event(void *ctx, uint32_t branch, int32_t code, uint64_t group, const char *msg)
{
    cym_qitem *it = calloc(1, sizeof *it);
    if (!it)
        return;
    it->is_event = 1;
    it->branch = branch;
    it->code = code;
    it->group = group;
    strncpy(it->msg, msg ? msg : "", sizeof it->msg - 1);
    enqueue(ctx, it);
}

cym_qsink *cym_qsink_create(void)
{
    cym_qsink *q = calloc(1, sizeof *q);
    if (!q)
        return NULL;
    if (pipe(q->fds) != 0) {
        free(q);
        return NULL;
    }
    for (int i = 0; i < 2; i++) {
        fcntl(q->fds[i], F_SETFL, fcntl(q->fds[i], F_GETFL) | O_NONBLOCK);
        fcntl(q->fds[i], F_SETFD, FD_CLOEXEC);
    }
    pthread_mutex_init(&q->mu, NULL);
    pthread_cond_init(&q->cv, NULL);
    return q;
}

void cym_qitem_free(cym_qitem *it)
{
    while (it) {
        cym_qitem *next = it->next;
        if (it->buf)
            it->buf->release(it->buf);
        free(it);
        it = next;
    }
}

void cym_qsink_destroy(cym_qsink *q)
{
    if (!q)
        return;
    cym_qitem_free(q->head);
    close(q->fds[0]);
    close(q->fds[1]);
    pthread_mutex_destroy(&q->mu);
    pthread_cond_destroy(&q->cv);
    free(q);
}

cymedia_sink cym_qsink_sink(cym_qsink *q)
{
    cymedia_sink s = {CYMEDIA_SINK_ABI, sizeof(cymedia_sink), q, on_frame, on_event};
    return s;
}

int cym_qsink_fd(cym_qsink *q)
{
    return q->fds[0];
}

cym_qitem *cym_qsink_take(cym_qsink *q, int timeout_ms)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    int64_t deadline_ns = (int64_t)tv.tv_sec * 1000000000 + (int64_t)tv.tv_usec * 1000 + (int64_t)timeout_ms * 1000000;
    struct timespec ts = {(time_t)(deadline_ns / 1000000000), (long)(deadline_ns % 1000000000)};
    pthread_mutex_lock(&q->mu);
    int r = 0;
    while (!q->head && timeout_ms != 0 && r == 0) {
        if (timeout_ms < 0)
            pthread_cond_wait(&q->cv, &q->mu);
        else
            r = pthread_cond_timedwait(&q->cv, &q->mu, &ts);
    }
    cym_qitem *it = q->head;
    q->head = q->tail = NULL;
    if (q->signaled) {
        char drain[64];
        while (read(q->fds[0], drain, sizeof drain) > 0)
            ;
        q->signaled = 0;
    }
    pthread_mutex_unlock(&q->mu);
    return it;
}

typedef struct counting {
    cymedia_sink sink;
    pthread_mutex_t mu;
    uint64_t frames;
} counting;

static void count_frame(void *ctx, const cymedia_meta *meta, cymedia_buf *buf)
{
    (void)meta;
    counting *c = ctx;
    pthread_mutex_lock(&c->mu);
    c->frames++;
    pthread_mutex_unlock(&c->mu);
    buf->release(buf);
}

cymedia_sink *cym_counting_sink_new(void)
{
    counting *c = calloc(1, sizeof *c);
    if (!c)
        return NULL;
    pthread_mutex_init(&c->mu, NULL);
    c->sink = (cymedia_sink){CYMEDIA_SINK_ABI, sizeof(cymedia_sink), c, count_frame, NULL};
    return &c->sink;
}

uint64_t cym_counting_sink_frames(const cymedia_sink *s)
{
    counting *c = s->ctx;
    pthread_mutex_lock(&c->mu);
    uint64_t n = c->frames;
    pthread_mutex_unlock(&c->mu);
    return n;
}

void cym_counting_sink_free(cymedia_sink *s)
{
    if (!s)
        return;
    counting *c = s->ctx;
    pthread_mutex_destroy(&c->mu);
    free(c);
}
