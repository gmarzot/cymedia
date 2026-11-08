/* Sinks provided by cymedia: a queue that Python drains, and a counter for tests. */
#ifndef CYMEDIA_QSINK_H
#define CYMEDIA_QSINK_H

#include "cymedia_sink.h"

typedef struct cym_qitem {
    struct cym_qitem *next;
    int is_event;
    cymedia_meta meta;
    const uint8_t *data;
    size_t size;
    cymedia_buf *buf;
    int32_t code;
    uint32_t branch;
    uint64_t group;
    char msg[256];
} cym_qitem;

typedef struct cym_qsink cym_qsink;

cym_qsink *cym_qsink_create(void);
void cym_qsink_destroy(cym_qsink *q);
cymedia_sink cym_qsink_sink(cym_qsink *q);
/* Readable while items are queued. */
int cym_qsink_fd(cym_qsink *q);
/* Takes every queued item, waiting up to timeout_ms (negative: forever) for the first. */
cym_qitem *cym_qsink_take(cym_qsink *q, int timeout_ms);
void cym_qitem_free(cym_qitem *it);

cymedia_sink *cym_counting_sink_new(void);
uint64_t cym_counting_sink_frames(const cymedia_sink *s);
void cym_counting_sink_free(cymedia_sink *s);

#endif
