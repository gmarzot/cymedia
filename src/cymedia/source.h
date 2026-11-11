/* cymedia source: reads an input with libavformat on its own thread, numbers video groups at key
   frames, feeds a graph and hands packets to a sink. */
#ifndef CYMEDIA_SOURCE_H
#define CYMEDIA_SOURCE_H

#include "cymedia_sink.h"
#include "engine.h"

/* cymedia_meta.branch of a source's packets; source events use branch UINT32_MAX. */
enum { CYM_SOURCE_VIDEO = 0, CYM_SOURCE_AUDIO = 1 };

typedef struct cym_source cym_source;

typedef struct cym_stream_info {
    int present;
    char codec[32];             /* decoder name, e.g. "h264", "aac" */
    const uint8_t *extradata;   /* H.264: avcC; otherwise as demuxed; NULL when none */
    int extradata_size;
    int length_prefixed;        /* video NAL units carry 4-byte lengths, not start codes */
    int width, height;
    int fps_num, fps_den;       /* 0 when unknown */
    int sample_rate, channels;
    int64_t bit_rate;           /* 0 when unknown */
} cym_stream_info;

typedef struct cym_source_stats {
    uint64_t video_packets, audio_packets, before_key, graph_dropped, bytes;
} cym_source_stats;

/* Opens and probes url. options are libavformat "key=value" pairs separated by '\n'; timeout_ms
   bounds the open (negative: no bound). NULL on failure, with err set. */
cym_source *cym_source_open(const char *url, const char *options, int timeout_ms, char *err,
                            size_t errlen);

void cym_source_info(const cym_source *s, int stream, cym_stream_info *out);

/* Starts the reader. graph (NULL for none) gets every video packet from the first key frame; sink
   gets audio packets and, with passthrough, video packets. pace holds each packet until its
   timestamp. Returns 0, or -1 when already started or the sink is invalid. */
int cym_source_start(cym_source *s, cym_graph *graph, cymedia_sink sink, int passthrough, int pace);

void cym_source_get_stats(cym_source *s, cym_source_stats *out);

/* Stops the reader, interrupting a blocked read, and frees the source. */
void cym_source_close(cym_source *s);

/* 1 when the linked libavformat reads this protocol, e.g. "srt" or "rtmp". */
int cym_have_protocol(const char *name);

/* 1 when the linked libavformat has this input format, e.g. "rtsp" or "hls". */
int cym_have_format(const char *name);

#endif
