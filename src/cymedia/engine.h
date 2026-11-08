/* cymedia graph engine: decode once, filter per branch, encode per branch on its own thread. */
#ifndef CYMEDIA_ENGINE_H
#define CYMEDIA_ENGINE_H

#include "cymedia_sink.h"

typedef struct cym_graph cym_graph;

typedef struct cym_branch_cfg {
    const char *filter;   /* libavfilter description, e.g. "scale_cuda=w=1280:h=720" */
    const char *encoder;  /* encoder name, e.g. "h264_nvenc" */
    const char *options;  /* encoder options, "key=value" pairs separated by '\n' */
    int strip_filler;     /* drop H.264 filler-data NAL units from the output */
} cym_branch_cfg;

typedef struct cym_graph_cfg {
    const char *decoder;      /* decoder name, e.g. "h264" */
    const char *hwaccel;      /* hardware device type, e.g. "cuda"; NULL decodes in software */
    const char *device;       /* device to open, NULL for the default */
    const uint8_t *extradata; /* codec configuration (avcC); NULL for Annex B with in-band headers */
    int extradata_size;
    int fps_num, fps_den;     /* nominal frame rate, passed to encoders */
    int extra_hw_frames;      /* extra decoder surfaces */
    int input_depth;          /* packets queued ahead of the decoder */
    int queue_depth;          /* frames queued ahead of each encoder */
    int realtime;             /* 1: drop on full queues; 0: block */
    int nbranches;
    const cym_branch_cfg *branches;
    cymedia_sink sink;
} cym_graph_cfg;

typedef struct cym_branch_stats {
    uint64_t frames_in, frames_out, frames_dropped, groups_abandoned, errors;
} cym_branch_stats;

typedef struct cym_graph_stats {
    uint64_t packets_in, packets_dropped, frames_decoded, decode_errors;
} cym_graph_stats;

cym_graph *cym_graph_create(const cym_graph_cfg *cfg, char *err, size_t errlen);

/* Copies data. Call from one thread. Returns 0 queued, 1 dropped, -1 closed or failed. */
int cym_graph_push(cym_graph *g, const uint8_t *data, size_t size, uint64_t group,
                   uint64_t object, int64_t pts_us, int key, int block);

/* Ends input and waits up to timeout_ms (negative: forever) for every branch to finish.
 * Returns 0 when done, 1 on timeout. */
int cym_graph_flush(cym_graph *g, int timeout_ms);

void cym_graph_destroy(cym_graph *g);

int cym_graph_nbranches(const cym_graph *g);
void cym_graph_get_stats(cym_graph *g, cym_graph_stats *out);
void cym_branch_get_stats(cym_graph *g, int branch, cym_branch_stats *out);

/* Copies up to cap of the most recent push-to-sink delays (ns); returns the number copied. */
int cym_branch_delays(cym_graph *g, int branch, int64_t *out, int cap);

int cym_have_encoder(const char *name);
int cym_have_decoder(const char *name);
int cym_have_filter(const char *name);
int cym_hw_available(const char *type, const char *device);

#endif
