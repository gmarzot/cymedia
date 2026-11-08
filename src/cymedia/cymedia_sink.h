/* cymedia output interface: stable C ABI between a graph and whatever consumes its frames. */
#ifndef CYMEDIA_SINK_H
#define CYMEDIA_SINK_H

#include <stddef.h>
#include <stdint.h>

#define CYMEDIA_SINK_ABI 1
#define CYMEDIA_SINK_CAPSULE "cymedia.sink"

enum {
    CYMEDIA_KEY = 1u << 0,          /* the frame is a keyframe */
    CYMEDIA_GROUP_START = 1u << 1,  /* first frame of a source group */
};

enum {
    CYMEDIA_EV_ERROR = 1,           /* the branch or graph hit an error; msg says what */
    CYMEDIA_EV_GROUP_ABANDONED = 2, /* the branch fell behind and dropped the rest of `group` */
    CYMEDIA_EV_INPUT_DROPPED = 3,   /* input was full; input resumes at the next group start */
    CYMEDIA_EV_END = 4,             /* the branch emitted its last frame after a flush */
};

typedef struct cymedia_meta {
    uint64_t group;   /* copied from the source */
    uint64_t object;  /* copied from the source */
    int64_t pts_us;   /* copied from the source */
    uint32_t flags;   /* CYMEDIA_KEY | CYMEDIA_GROUP_START */
    uint32_t branch;  /* index of the branch that produced the frame */
} cymedia_meta;

/* One encoded frame. The sink owns it and calls release exactly once, from any thread. */
typedef struct cymedia_buf {
    const uint8_t *data;
    size_t size;
    void (*release)(struct cymedia_buf *buf);
    void *priv;
} cymedia_buf;

/* Callbacks run on graph threads, concurrently across branches; they must not block for long. */
typedef struct cymedia_sink {
    uint32_t abi;   /* CYMEDIA_SINK_ABI */
    uint32_t size;  /* sizeof(cymedia_sink) */
    void *ctx;
    void (*on_frame)(void *ctx, const cymedia_meta *meta, cymedia_buf *buf);
    void (*on_event)(void *ctx, uint32_t branch, int32_t code, uint64_t group, const char *msg);
} cymedia_sink;

#endif
